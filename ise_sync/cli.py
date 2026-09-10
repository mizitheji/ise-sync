#!/usr/bin/env python3
"""
ise_sync - sync ERS-managed config objects between two independent ISE
nodes/clusters with no shared deployment (no ISE replication involved).

Everything here is HTTPS calls to the ERS admin API - same class of
traffic as the GUI. No repository/config-restore CLI path is touched,
so there is no node reboot at any point, in either direction.

Usage:
    python -m ise_sync.cli diff  --config config.yaml --from dc1-pan --to dc2-standalone
    python -m ise_sync.cli sync  --config config.yaml --from dc1-pan --to dc2-standalone            # dry-run (default)
    python -m ise_sync.cli sync  --config config.yaml --from dc1-pan --to dc2-standalone --apply    # actually push creates/updates
    python -m ise_sync.cli pull  --config config.yaml --node dc1-pan --out snapshots/dc1-pan.json   # export only, for git-tracked audit trail
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

import yaml

from .client import ISEClient, ISEAPIError
from .resources import RESOURCES, ADVANCED_RESOURCES, BLOCKED_RESOURCES
from .diff import build_plan, summarize_plan, print_diff_header, print_sync_summary, _unwrap  # noqa: reuse internal helper
from .colours import bold, cyan, dim, green, yellow

ALL_RESOURCES = {**RESOURCES, **ADVANCED_RESOURCES}

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("ise_sync")


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def make_client(cfg: dict, node_name: str) -> ISEClient:
    node_cfg = cfg["nodes"][node_name]
    return ISEClient(
        name=node_name,
        base_url=node_cfg["base_url"],
        env_prefix=node_cfg["env_prefix"],
        verify_ssl=node_cfg.get("verify_ssl", False),
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

    return resources


def fetch_all(client: ISEClient, resources: list[str]) -> dict[str, list[dict]]:
    out = {}
    label = bold(cyan(f"[{client.name}]"))
    print(f"  {label} fetching {len(resources)} resource type(s) ...", flush=True)
    for res_key in resources:
        rdef = ALL_RESOURCES[res_key]
        if rdef.api_type == "openapi":
            out[res_key] = client.openapi_list(rdef.ers_path)
        else:
            out[res_key] = client.ers_get_all_detail(rdef.ers_path)
        count = len(out[res_key])
        print(f"    {dim('·')} {res_key}: {count} object{'s' if count != 1 else ''}", flush=True)
    return out


def cmd_pull(args):
    cfg = load_config(args.config)
    client = make_client(cfg, args.node)
    resources = resolve_resource_list(cfg, args.include_advanced)
    data = fetch_all(client, resources)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(data, indent=2, sort_keys=True))
    logger.info("wrote snapshot to %s", out_path)


def cmd_diff(args):
    cfg = load_config(args.config)
    src_client = make_client(cfg, args.src)
    tgt_client = make_client(cfg, args.dst)
    resources = resolve_resource_list(cfg, args.include_advanced)

    src_data = fetch_all(src_client, resources)
    tgt_data = fetch_all(tgt_client, resources)

    exclude = cfg.get("exclude_name_patterns", [])
    plans = []
    print_diff_header(args.src, args.dst)
    for res_key in resources:
        rdef = ALL_RESOURCES[res_key]
        plan = build_plan(res_key, rdef, src_data[res_key], tgt_data[res_key], exclude)
        plans.append(plan)
        print(summarize_plan(plan))
        print()
    print_sync_summary(plans, applied=False)


def cmd_sync(args):
    cfg = load_config(args.config)
    src_client = make_client(cfg, args.src)
    tgt_client = make_client(cfg, args.dst)
    resources = resolve_resource_list(cfg, args.include_advanced)

    if args.include_advanced and args.apply:
        logger.warning(
            "Advanced resources (policy sets) included with --apply: this only "
            "pushes top-level policy set containers (order/condition/state), "
            "NOT the authentication/authorization rules inside them. Review "
            "the diff output carefully before trusting this on a live engine."
        )

    src_data = fetch_all(src_client, resources)
    tgt_data = fetch_all(tgt_client, resources)

    exclude = cfg.get("exclude_name_patterns", [])
    plans = []

    print_diff_header(args.src, args.dst)
    for res_key in resources:
        rdef = ALL_RESOURCES[res_key]
        plan = build_plan(res_key, rdef, src_data[res_key], tgt_data[res_key], exclude)
        plans.append(plan)
        print(summarize_plan(plan))
        print()

        if not plan.to_create and not plan.to_update:
            continue

        if not args.apply:
            continue  # dry-run: report only

        for obj in plan.to_create:
            body = _unwrap(obj, rdef)
            body = {k: v for k, v in body.items() if k not in rdef.strip_fields}
            payload = {rdef.wrapper_key: body} if rdef.wrapper_key else body
            try:
                if rdef.api_type == "openapi":
                    tgt_client.openapi_create(rdef.ers_path, payload)
                else:
                    tgt_client.ers_create(rdef.ers_path, payload)
                print(green(f"    ✔  created {body.get('name')}"))
            except ISEAPIError as e:
                logger.error("[%s] FAILED create %s: %s", args.dst, body.get("name"), e)

        for src_obj, tgt_obj, delta in plan.to_update:
            src_body = _unwrap(src_obj, rdef)
            tgt_body = _unwrap(tgt_obj, rdef)
            merged = {k: v for k, v in src_body.items() if k not in rdef.strip_fields}
            merged["id"] = tgt_body["id"]  # keep target's own ID for the PUT
            payload = {rdef.wrapper_key: merged} if rdef.wrapper_key else merged
            try:
                if rdef.api_type == "openapi":
                    tgt_client.openapi_update(rdef.ers_path, tgt_body["id"], payload)
                else:
                    tgt_client.ers_update(rdef.ers_path, tgt_body["id"], payload)
                print(yellow(f"    ✔  updated {src_body.get('name')}"))
            except ISEAPIError as e:
                logger.error("[%s] FAILED update %s: %s", args.dst, src_body.get("name"), e)

    print_sync_summary(plans, applied=args.apply)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p_pull = sub.add_parser("pull", help="Export one node's config objects to a JSON file (audit/git snapshot)")
    p_pull.add_argument("--node", required=True)
    p_pull.add_argument("--out", required=True)
    p_pull.add_argument("--include-advanced", action="store_true",
                         help="Also pull policy sets (config.yaml: advanced_resources). Top-level container only, not nested rules.")
    p_pull.set_defaults(func=cmd_pull)

    p_diff = sub.add_parser("diff", help="Show differences between two nodes, no changes made")
    p_diff.add_argument("--from", dest="src", required=True)
    p_diff.add_argument("--to", dest="dst", required=True)
    p_diff.add_argument("--include-advanced", action="store_true",
                         help="Also diff policy sets (config.yaml: advanced_resources). Top-level container only, not nested rules.")
    p_diff.set_defaults(func=cmd_diff)

    p_sync = sub.add_parser("sync", help="Diff and (optionally) push creates/updates from source to target")
    p_sync.add_argument("--from", dest="src", required=True)
    p_sync.add_argument("--to", dest="dst", required=True)
    p_sync.add_argument("--apply", action="store_true", help="Actually push changes. Omit for dry-run.")
    p_sync.add_argument("--include-advanced", action="store_true",
                         help="Also sync policy sets (config.yaml: advanced_resources). Top-level container only, not nested rules - read README first.")
    p_sync.set_defaults(func=cmd_sync)

    args = parser.parse_args()
    try:
        args.func(args)
    except EnvironmentError as e:
        logger.error(str(e))
        sys.exit(1)
    except ISEAPIError as e:
        logger.error(str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
