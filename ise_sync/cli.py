#!/usr/bin/env python3
"""
ise_sync - sync ERS-managed config objects between two independent ISE
nodes/clusters with no shared deployment (no ISE replication involved).

Everything here is HTTPS calls to the ERS admin API - same class of
traffic as the GUI. No repository/config-restore CLI path is touched,
so there is no node reboot at any point, in either direction.

Usage:
    python -m ise_sync.cli diff  --from dc1-pan --to dc2-standalone
    python -m ise_sync.cli diff  --from dc1-pan --to dc2-standalone --exit-code   # rc=2 if drift (CI)
    python -m ise_sync.cli sync  --from dc1-pan --to dc2-standalone                    # dry-run (default)
    python -m ise_sync.cli sync  --from dc1-pan --to dc2-standalone --apply            # push creates/updates
    python -m ise_sync.cli sync  --from dc1-pan --to dc2-standalone --apply --delete   # ...and delete extras
    python -m ise_sync.cli pull  --node dc1-pan --out snapshots/dc1-pan.json           # export only

Exit codes: 0 ok | 1 error or failed writes | 2 drift found (diff --exit-code)
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from .client import DEFAULT_WORKERS, ISEAPIError, ISEClient
from .colours import bold, cyan, dim, green, magenta, red, yellow
from .diff import (SyncPlan, _obj_name, _unwrap, build_plan, print_diff_header,
                   print_sync_summary, summarize_plan)
from .resources import (ADVANCED_RESOURCES, BLOCKED_RESOURCES, RESOURCES,
                        ResourceDef, is_system_policy_set)

ALL_RESOURCES = {**RESOURCES, **ADVANCED_RESOURCES}

logger = logging.getLogger("ise_sync")

EXIT_ERROR = 1
EXIT_DRIFT = 2


# --------------------------------------------------------------------------
# config / env
# --------------------------------------------------------------------------

def load_dotenv(path: str | Path = ".env") -> None:
    """Populate os.environ from .env if not already set."""
    env_file = Path(path)
    if not env_file.is_file():
        return
    with env_file.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'\"")
                if k and k not in os.environ:
                    os.environ[k] = v


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    for key in ("nodes", "sync_resources"):
        if not cfg.get(key):
            raise SystemExit(f"config error: '{key}' is missing or empty in {path}")
    return cfg


def make_client(cfg: dict, node_name: str, workers: int | None = None) -> ISEClient:
    if node_name not in cfg["nodes"]:
        raise SystemExit(
            f"unknown node '{node_name}' - defined nodes: {', '.join(cfg['nodes'])}"
        )
    node_cfg = cfg["nodes"][node_name]
    # verify_ssl may be true/false or a CA-bundle path; `ca_bundle` is an alias.
    verify = node_cfg.get("ca_bundle") or node_cfg.get("verify_ssl", True)
    return ISEClient(
        name=node_name,
        base_url=node_cfg["base_url"],
        env_prefix=node_cfg["env_prefix"],
        verify_ssl=verify,
        max_workers=workers or cfg.get("max_workers", DEFAULT_WORKERS),
    )


def resolve_resource_list(cfg: dict, include_advanced: bool) -> list[str]:
    resources = list(cfg["sync_resources"])
    if include_advanced:
        resources += list(cfg.get("advanced_resources", []))

    blocked_found = [r for r in resources if r in BLOCKED_RESOURCES]
    if blocked_found:
        logger.warning(
            "Ignoring blocked resource(s) in config.yaml: %s - Active Directory "
            "config is never synced by this tool (node-local domain join state).",
            ", ".join(blocked_found),
        )
        resources = [r for r in resources if r not in BLOCKED_RESOURCES]

    unknown = [r for r in resources if r not in ALL_RESOURCES]
    if unknown:
        raise SystemExit(
            f"config error: unknown resource(s) {', '.join(unknown)} - "
            f"valid: {', '.join(ALL_RESOURCES)}"
        )
    return resources


# --------------------------------------------------------------------------
# internal-user identity-group translation (UUIDs differ per node)
# --------------------------------------------------------------------------

def _translate_internal_users_groups(client: ISEClient, users: list[dict]) -> None:
    """Translate identityGroups UUIDs in internal user bodies to group names in-place."""
    try:
        ig_map = client.get_identity_group_id_to_name()
    except Exception as e:
        logger.warning("[%s] Failed to resolve identity groups for internal users: %s", client.name, e)
        return
    rdef = ALL_RESOURCES["internal_users"]
    for obj in users:
        body = _unwrap(obj, rdef)
        raw = body.get("identityGroups", "")
        if raw and isinstance(raw, str):
            gids = [g.strip() for g in raw.split(",") if g.strip()]
            body["identityGroups"] = ",".join(sorted(ig_map.get(g, g) for g in gids))


def _resolve_target_identity_groups(raw_groups: str | list[str], tgt_client: ISEClient,
                                    target_name: str) -> str:
    """Convert group names to target node's group UUIDs."""
    target_ig_map = tgt_client.get_identity_group_name_to_id()
    if isinstance(raw_groups, str):
        gnames = [g.strip() for g in raw_groups.split(",") if g.strip()]
    else:
        gnames = list(raw_groups)
    missing = [g for g in gnames if g not in target_ig_map]
    if missing:
        raise ISEAPIError(
            "POST/PUT", "/ers/config/internaluser", 400,
            f"Target node '{target_name}' is missing identity group(s): {', '.join(missing)}. "
            f"Sync identity_groups first.",
        )
    return ",".join(target_ig_map[g] for g in gnames)


# --------------------------------------------------------------------------
# fetch
# --------------------------------------------------------------------------

def fetch_all(client: ISEClient, resources: list[str]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    label = bold(cyan(f"[{client.name}]"))
    lines = [f"  {label} fetching {len(resources)} resource type(s) ..."]
    for res_key in resources:
        rdef = ALL_RESOURCES[res_key]
        if rdef.api_type == "openapi":
            out[res_key] = client.openapi_list(rdef.ers_path)
        else:
            out[res_key] = client.ers_get_all_detail(rdef.ers_path)
        if res_key == "internal_users":
            _translate_internal_users_groups(client, out[res_key])
        n = len(out[res_key])
        lines.append(f"    {dim('·')} {res_key}: {n} object{'s' if n != 1 else ''}")
    print("\n".join(lines), flush=True)  # one block per node so parallel output doesn't interleave
    return out


def fetch_pair(src: ISEClient, dst: ISEClient, resources: list[str]):
    """Fetch both nodes at the same time - they're independent servers."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_src = pool.submit(fetch_all, src, resources)
        f_dst = pool.submit(fetch_all, dst, resources)
        return f_src.result(), f_dst.result()


def build_plans(cfg: dict, resources: list[str], src_data: dict, tgt_data: dict) -> list[SyncPlan]:
    exclude = cfg.get("exclude_name_patterns", [])
    return [
        build_plan(k, ALL_RESOURCES[k], src_data[k], tgt_data[k], exclude)
        for k in resources
    ]


# --------------------------------------------------------------------------
# commands: pull / diff
# --------------------------------------------------------------------------

def cmd_pull(args) -> int:
    cfg = load_config(args.config)
    client = make_client(cfg, args.node, args.workers)
    resources = resolve_resource_list(cfg, args.include_advanced)
    data = fetch_all(client, resources)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    logger.info("wrote snapshot to %s", out_path)
    return 0


def cmd_diff(args) -> int:
    cfg = load_config(args.config)
    src_client = make_client(cfg, args.src, args.workers)
    tgt_client = make_client(cfg, args.dst, args.workers)
    resources = resolve_resource_list(cfg, args.include_advanced)

    src_data, tgt_data = fetch_pair(src_client, tgt_client, resources)
    plans = build_plans(cfg, resources, src_data, tgt_data)

    print_diff_header(args.src, args.dst)
    for plan in plans:
        print(summarize_plan(plan))
        print()
    print_sync_summary(plans, applied=False)

    if args.exit_code and any(p.has_drift for p in plans):
        return EXIT_DRIFT
    return 0


# --------------------------------------------------------------------------
# commands: sync
# --------------------------------------------------------------------------

def _push(client: ISEClient, rdef: ResourceDef, payload: dict, obj_id: str | None = None) -> None:
    """Create (obj_id None) or update one object via the right API family."""
    if rdef.api_type == "openapi":
        if obj_id is None:
            client.openapi_create(rdef.ers_path, payload)
        else:
            client.openapi_update(rdef.ers_path, obj_id, payload)
    else:
        if obj_id is None:
            client.ers_create(rdef.ers_path, payload)
        else:
            client.ers_update(rdef.ers_path, obj_id, payload)


def apply_creates_and_updates(plans: list[SyncPlan], tgt_client: ISEClient, dst: str) -> int:
    """Push creates/updates in dependency order. Returns number of failures.
    One bad object never aborts the rest of the run."""
    failures = 0
    for plan in plans:
        rdef = ALL_RESOURCES[plan.resource]
        res_key = plan.resource

        for obj in plan.to_create:
            body = {k: v for k, v in _unwrap(obj, rdef).items() if k not in rdef.strip_fields}
            try:
                if res_key == "internal_users" and "identityGroups" in body:
                    body["identityGroups"] = _resolve_target_identity_groups(
                        body["identityGroups"], tgt_client, dst)
                _push(tgt_client, rdef, {rdef.wrapper_key: body} if rdef.wrapper_key else body)
                print(green(f"    ✔  created {body.get('name')}"))
            except ISEAPIError as e:
                failures += 1
                logger.error("[%s] FAILED create %s: %s", dst, body.get("name"), e)

        for src_obj, tgt_obj, _delta in plan.to_update:
            src_body = _unwrap(src_obj, rdef)
            tgt_body = _unwrap(tgt_obj, rdef)
            name = src_body.get("name")
            if is_system_policy_set(rdef, src_body):
                logger.info("[%s] Skipping update for system '%s' policy set (managed automatically by ISE)", dst, name)
                continue
            if res_key == "internal_users" and name in tgt_client.get_admin_user_names():
                logger.info("[%s] Skipping update for admin user '%s' (admin accounts are managed via Admin Access, not internaluser API)", dst, name)
                continue
            merged = {k: v for k, v in src_body.items() if k not in rdef.strip_fields}
            merged["id"] = tgt_body["id"]  # keep target's own ID for the PUT
            try:
                if res_key == "internal_users" and "identityGroups" in merged:
                    merged["identityGroups"] = _resolve_target_identity_groups(
                        merged["identityGroups"], tgt_client, dst)
                _push(tgt_client, rdef, {rdef.wrapper_key: merged} if rdef.wrapper_key else merged,
                      obj_id=tgt_body["id"])
                print(yellow(f"    ✔  updated {name}"))
            except ISEAPIError as e:
                failures += 1
                logger.error("[%s] FAILED update %s: %s", dst, name, e)

        if res_key == "identity_groups" and plan.to_create:
            tgt_client.invalidate_identity_groups()  # internal_users later must see new groups
    return failures


def collect_deletions(plans: list[SyncPlan], tgt_client: ISEClient, dst: str):
    """Extra-in-target objects eligible for deletion, children before parents."""
    items = []
    for plan in reversed(plans):
        rdef = ALL_RESOURCES[plan.resource]
        for obj in plan.extra_in_target:
            body = _unwrap(obj, rdef)
            name = body.get("name") or _obj_name(obj)
            if is_system_policy_set(rdef, body):
                logger.info("[%s] Skipping delete for system '%s' policy set (protected fallback)", dst, name)
                continue
            if plan.resource == "internal_users" and name in tgt_client.get_admin_user_names():
                logger.warning("[%s] Skipping delete for admin user '%s' (protected account)", dst, name)
                continue
            items.append((plan.resource, rdef, name, body.get("id")))
    return items


def confirm_deletions(items, dst: str, assume_yes: bool) -> bool:
    print()
    print(dim("─" * 52))
    print(bold(red(f"  🗑  DELETION VERIFICATION: {len(items)} extra object(s) on '{dst}'")))
    print(dim("  The following objects exist on target but NOT on source and are marked for deletion:"))
    by_res: dict[str, list[tuple[str, str | None]]] = {}
    for rk, _, n, oid in items:
        by_res.setdefault(rk, []).append((n, oid))
    for rk, group in by_res.items():
        print(f"    • {bold(cyan(rk.replace('_', ' ').title()))} ({len(group)}):")
        for n, oid in group:
            print(f"       - {red(n)}{dim(f'  (id: {oid})' if oid else '')}")
    print()
    print(bold(yellow(f"  ⚠️  WARNING: Deletion is permanent and cannot be undone on '{dst}'.")))

    if assume_yes:
        print(bold("  --yes specified: auto-confirming deletion."))
        return True
    try:
        reply = input(bold(f"  Are you sure you want to permanently delete these {len(items)} object(s)? [y/N]: "))
        return reply.strip().lower() in ("y", "yes")
    except (KeyboardInterrupt, EOFError):
        print()
        return False


def execute_deletions(items, tgt_client: ISEClient, dst: str) -> tuple[int, int]:
    """Returns (deleted, failed)."""
    print(dim("  Deleting in reverse dependency order..."))
    deleted = failed = 0
    for rk, rdef, name, obj_id in items:
        if not obj_id:
            logger.error("[%s] Cannot delete %s '%s': missing object ID", dst, rk, name)
            failed += 1
            continue
        try:
            if rdef.api_type == "openapi":
                tgt_client.openapi_delete(rdef.ers_path, obj_id)
            else:
                tgt_client.ers_delete(rdef.ers_path, obj_id)
            print(red(f"    ✔  deleted {name} ({rk.replace('_', ' ')})"))
            deleted += 1
        except ISEAPIError as e:
            failed += 1
            logger.error("[%s] FAILED delete %s '%s': %s", dst, rk, name, e)
    return deleted, failed


def cmd_sync(args) -> int:
    cfg = load_config(args.config)
    src_client = make_client(cfg, args.src, args.workers)
    tgt_client = make_client(cfg, args.dst, args.workers)
    resources = resolve_resource_list(cfg, args.include_advanced)

    if args.include_advanced and args.apply:
        logger.warning(
            "Advanced resources (policy sets) included with --apply: this only "
            "pushes top-level policy set containers (order/condition/state), "
            "NOT the authentication/authorization rules inside them. Review "
            "the diff output carefully before trusting this on a live engine."
        )

    src_data, tgt_data = fetch_pair(src_client, tgt_client, resources)
    plans = build_plans(cfg, resources, src_data, tgt_data)

    print_diff_header(args.src, args.dst)
    for plan in plans:
        print(summarize_plan(plan, delete_enabled=args.delete))
        print()

    if not args.apply:
        print_sync_summary(plans, applied=False, delete_enabled=args.delete)
        return 0

    failures = apply_creates_and_updates(plans, tgt_client, args.dst)

    total_deleted = 0
    deletion_aborted = False
    if args.delete:
        items = collect_deletions(plans, tgt_client, args.dst)
        if items:
            if confirm_deletions(items, args.dst, args.yes):
                total_deleted, del_failed = execute_deletions(items, tgt_client, args.dst)
                failures += del_failed
            else:
                print(yellow("  ✖ Deletion cancelled by user. Extra objects were not touched."))
                deletion_aborted = True

    print_sync_summary(plans, applied=True, delete_enabled=args.delete,
                       total_deleted=total_deleted, deletion_aborted=deletion_aborted)
    if failures:
        print(red(f"  ✖ {failures} operation(s) failed - see errors above. Exiting non-zero."))
        print()
        return EXIT_ERROR
    return 0


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--workers", type=int, default=None,
                        help=f"Concurrent detail requests per node (default: config 'max_workers' or {DEFAULT_WORKERS}). "
                             "Use 1 to disable concurrency if ERS rate-limiting complains.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    adv_help = "Also {} policy sets and internal users (config.yaml: advanced_resources). Policy sets: top-level container only, not nested rules."

    p_pull = sub.add_parser("pull", help="Export one node's config objects to a JSON file (audit/git snapshot)")
    p_pull.add_argument("--node", required=True)
    p_pull.add_argument("--out", required=True)
    p_pull.add_argument("--include-advanced", action="store_true", help=adv_help.format("pull"))
    p_pull.set_defaults(func=cmd_pull)

    p_diff = sub.add_parser("diff", help="Show differences between two nodes, no changes made")
    p_diff.add_argument("--from", dest="src", required=True)
    p_diff.add_argument("--to", dest="dst", required=True)
    p_diff.add_argument("--include-advanced", action="store_true", help=adv_help.format("diff"))
    p_diff.add_argument("--exit-code", action="store_true",
                        help=f"Exit {EXIT_DRIFT} if any drift is found (for CI/cron drift checks).")
    p_diff.set_defaults(func=cmd_diff)

    p_sync = sub.add_parser("sync", help="Diff and (optionally) push creates/updates/deletes from source to target")
    p_sync.add_argument("--from", dest="src", required=True)
    p_sync.add_argument("--to", dest="dst", required=True)
    p_sync.add_argument("--apply", action="store_true", help="Actually push changes. Omit for dry-run.")
    p_sync.add_argument("--delete", action="store_true",
                        help="Delete extra objects on target that do not exist on source (prompts for confirmation).")
    p_sync.add_argument("-y", "--yes", action="store_true",
                        help="Auto-confirm deletion prompt when --delete is used.")
    p_sync.add_argument("--include-advanced", action="store_true", help=adv_help.format("sync"))
    p_sync.set_defaults(func=cmd_sync)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv()
    try:
        sys.exit(args.func(args) or 0)
    except (EnvironmentError, ISEAPIError) as e:
        logger.error(str(e))
        sys.exit(EXIT_ERROR)
    except KeyboardInterrupt:
        print()
        sys.exit(130)


if __name__ == "__main__":
    main()
