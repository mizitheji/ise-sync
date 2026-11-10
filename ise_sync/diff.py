from __future__ import annotations
import copy
import re
from dataclasses import dataclass, field
from functools import lru_cache

from deepdiff import DeepDiff

from .resources import ResourceDef, is_system_policy_set
from .colours import bold, cyan, dim, green, magenta, red, yellow


@dataclass
class SyncPlan:
    resource: str
    to_create: list[dict] = field(default_factory=list)   # exists in source, not target
    to_update: list[tuple[dict, dict, dict]] = field(default_factory=list)  # (source_obj, target_obj, diff)
    unchanged: list[str] = field(default_factory=list)     # names
    extra_in_target: list[dict] = field(default_factory=list)  # exists in target, not source

    @property
    def has_drift(self) -> bool:
        return bool(self.to_create or self.to_update or self.extra_in_target)


def _unwrap(obj: dict, rdef: ResourceDef) -> dict:
    return obj.get(rdef.wrapper_key, obj) if rdef.wrapper_key else obj


def _normalize(obj: dict, rdef: ResourceDef) -> dict:
    """Strip node-local fields (id/link/timestamps) so two independently
    built objects with the same logical config compare as equal."""
    body = copy.deepcopy(_unwrap(obj, rdef))
    for f in rdef.strip_fields:
        body.pop(f, None)
    # Built-in Default policy sets in ISE are always pinned to the bottom;
    # their rank is automatically derived by ISE from the count of custom sets.
    if is_system_policy_set(rdef, body):
        body.pop("rank", None)
    if rdef.key == "internal_users" and "identityGroups" in body:
        val = body["identityGroups"]
        if isinstance(val, str):
            body["identityGroups"] = sorted([g.strip() for g in val.split(",") if g.strip()])
    return body


@lru_cache(maxsize=None)
def _compile(patterns: tuple[str, ...]) -> tuple[re.Pattern, ...]:
    return tuple(re.compile(p) for p in patterns)


def _excluded(name: str, patterns) -> bool:
    return any(rx.match(name) for rx in _compile(tuple(patterns)))


def changed_fields(delta) -> list[str]:
    """Top-level field names that differ (e.g. ['description', 'NetworkDeviceGroupList'])."""
    keys = getattr(delta, "affected_root_keys", None)
    return sorted(str(k) for k in keys) if keys else []


def _obj_name(obj: dict) -> str:
    return obj.get("name") or list(obj.values())[0].get("name", "?")


def build_plan(resource: str, rdef: ResourceDef, source_objs: list[dict],
               target_objs: list[dict], exclude_patterns: list[str]) -> SyncPlan:
    plan = SyncPlan(resource=resource)

    def _index(objs: list[dict]) -> dict[str, dict]:
        out = {}
        for o in objs:
            name = _unwrap(o, rdef)["name"]
            if not _excluded(name, exclude_patterns):
                out[name] = o
        return out

    src_by_name = _index(source_objs)
    tgt_by_name = _index(target_objs)

    for name, src_obj in src_by_name.items():
        if name not in tgt_by_name:
            plan.to_create.append(src_obj)
            continue

        tgt_obj = tgt_by_name[name]
        src_norm = _normalize(src_obj, rdef)
        tgt_norm = _normalize(tgt_obj, rdef)
        delta = DeepDiff(tgt_norm, src_norm, ignore_order=True)
        if delta:
            plan.to_update.append((src_obj, tgt_obj, delta))
        else:
            plan.unchanged.append(name)

    for name, tgt_obj in tgt_by_name.items():
        if name not in src_by_name:
            plan.extra_in_target.append(tgt_obj)

    return plan


def summarize_plan(plan: SyncPlan, delete_enabled: bool = False) -> str:
    """Return a coloured, human-friendly summary block for one resource type."""
    n_create  = len(plan.to_create)
    n_update  = len(plan.to_update)
    n_same    = len(plan.unchanged)
    n_extra   = len(plan.extra_in_target)
    has_delta = n_create or n_update or n_extra

    # ── header ──────────────────────────────────────────────────────────────
    label = plan.resource.replace("_", " ").title()
    header = bold(cyan(f"  {label}"))
    bar    = dim("─" * 52)

    lines = [bar, header]

    # ── counts row ──────────────────────────────────────────────────────────
    def _badge(n: int, colour_fn, symbol: str) -> str:
        text = f"{symbol} {n}"
        return colour_fn(text) if n else dim(text)

    counts = (
        f"    {_badge(n_create, green,   '✚ create')}   "
        f"{_badge(n_update, yellow,  '~ update')}   "
        f"{dim(f'· {n_same} unchanged')}"
    )
    lines.append(counts)

    # ── detail lines ────────────────────────────────────────────────────────
    for obj in plan.to_create:
        lines.append(green(f"    ✚  {_obj_name(obj)}"))

    for src, _tgt, delta in plan.to_update:
        name = _obj_name(src)
        fields = changed_fields(delta)
        what = ", ".join(fields) if fields else f"{len(delta)} difference(s)"
        lines.append(yellow(f"    ~  {name}") + dim(f"  (changed: {what})"))

    if n_extra:
        hint = "  (will prompt to delete with --apply)" if delete_enabled else "  (not touched — review manually)"
        lines.append(magenta(f"    ?  {n_extra} extra in target") + dim(hint))
        for obj in plan.extra_in_target:
            lines.append(magenta(f"       •  {_obj_name(obj)}"))

    # ── no-change shortcut ───────────────────────────────────────────────────
    if not has_delta:
        lines.append(dim("    ✔  in sync"))

    return "\n".join(lines)


def print_diff_header(src: str, dst: str) -> None:
    width = 56
    title = f" {src}  →  {dst} "
    pad   = max(0, width - len(title))
    left  = pad // 2
    right = pad - left
    print()
    print(bold(cyan("╔" + "═" * width + "╗")))
    print(bold(cyan("║")) + bold(" " * left + title + " " * right) + bold(cyan("║")))
    print(bold(cyan("╚" + "═" * width + "╝")))
    print()


def print_sync_summary(
    plans: list[SyncPlan],
    applied: bool,
    delete_enabled: bool = False,
    total_deleted: int = 0,
    deletion_aborted: bool = False,
) -> None:
    """Print a one-line summary after all resources have been processed."""
    total_create = sum(len(p.to_create) for p in plans)
    total_update = sum(len(p.to_update) for p in plans)
    total_extra  = sum(len(p.extra_in_target) for p in plans)

    print()
    print(dim("─" * 52))
    if not (total_create or total_update or total_extra):
        print(bold("  ✔  Everything in sync — nothing to do."))
    elif not applied:
        parts = []
        if total_create: parts.append(green(f"✚ {total_create} to create"))
        if total_update: parts.append(yellow(f"~ {total_update} to update"))
        if total_extra:
            if delete_enabled:
                parts.append(red(f"🗑 {total_extra} to delete"))
            else:
                parts.append(magenta(f"? {total_extra} extra in target"))
        print(bold("  DRY RUN  ") + "  ".join(parts))
        if delete_enabled:
            print(dim("  Re-run with --apply --delete to push changes and delete extra objects."))
        else:
            print(dim("  Re-run with --apply to push changes (use --delete to also remove extra in target)."))
    else:
        parts = []
        if total_create: parts.append(green(f"✚ {total_create} created"))
        if total_update: parts.append(yellow(f"~ {total_update} updated"))
        if delete_enabled:
            if total_deleted:
                parts.append(red(f"🗑 {total_deleted} deleted"))
            remaining = total_extra - total_deleted
            if remaining > 0:
                if deletion_aborted:
                    parts.append(magenta(f"? {remaining} extra in target (skipped by user)"))
                else:
                    parts.append(magenta(f"? {remaining} extra in target (protected/skipped)"))
        else:
            if total_extra:
                parts.append(magenta(f"? {total_extra} extra in target (skipped)"))
        print(bold("  ✔  Applied  ") + "  ".join(parts))
    print()
