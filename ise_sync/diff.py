from __future__ import annotations
import copy
import re
from dataclasses import dataclass, field

from deepdiff import DeepDiff

from .resources import ResourceDef
from .colours import bold, cyan, dim, green, magenta, red, yellow


@dataclass
class SyncPlan:
    resource: str
    to_create: list[dict] = field(default_factory=list)   # exists in source, not target
    to_update: list[tuple[dict, dict, dict]] = field(default_factory=list)  # (source_obj, target_obj, diff)
    unchanged: list[str] = field(default_factory=list)     # names
    extra_in_target: list[dict] = field(default_factory=list)  # exists in target, not source (never auto-deleted)


def _unwrap(obj: dict, rdef: ResourceDef) -> dict:
    return obj.get(rdef.wrapper_key, obj) if rdef.wrapper_key else obj


def _normalize(obj: dict, rdef: ResourceDef) -> dict:
    """Strip node-local fields (id/link/timestamps) so two independently
    built objects with the same logical config compare as equal."""
    body = copy.deepcopy(_unwrap(obj, rdef))
    for f in rdef.strip_fields:
        body.pop(f, None)
    return body


def _excluded(name: str, patterns: list[str]) -> bool:
    return any(re.match(p, name) for p in patterns)


def _obj_name(obj: dict) -> str:
    return obj.get("name") or list(obj.values())[0].get("name", "?")


def build_plan(resource: str, rdef: ResourceDef, source_objs: list[dict],
               target_objs: list[dict], exclude_patterns: list[str]) -> SyncPlan:
    plan = SyncPlan(resource=resource)

    src_by_name = {
        _unwrap(o, rdef)["name"]: o for o in source_objs
        if not _excluded(_unwrap(o, rdef)["name"], exclude_patterns)
    }
    tgt_by_name = {
        _unwrap(o, rdef)["name"]: o for o in target_objs
        if not _excluded(_unwrap(o, rdef)["name"], exclude_patterns)
    }

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


def summarize_plan(plan: SyncPlan) -> str:
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
        fields = len(delta)
        lines.append(yellow(f"    ~  {name}") + dim(f"  ({fields} field group{'s' if fields != 1 else ''} differ)"))

    if n_extra:
        lines.append(magenta(f"    ?  {n_extra} extra in target") + dim("  (not touched — review manually)"))
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


def print_sync_summary(plans: list[SyncPlan], applied: bool) -> None:
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
        if total_extra:  parts.append(magenta(f"? {total_extra} extra in target"))
        print(bold("  DRY RUN  ") + "  ".join(parts))
        print(dim("  Re-run with --apply to push changes."))
    else:
        parts = []
        if total_create: parts.append(green(f"✚ {total_create} created"))
        if total_update: parts.append(yellow(f"~ {total_update} updated"))
        if total_extra:  parts.append(magenta(f"? {total_extra} extra in target (skipped)"))
        print(bold("  ✔  Applied  ") + "  ".join(parts))
    print()
