"""
Registry of syncable ERS resource types.

Order in RESOURCES matters: it's the dependency-safe push order (parents
before children), e.g. a NetworkDeviceGroup must exist before a
NetworkDevice that references it.

`strip_fields` are node-local / non-portable fields that must be removed
before comparing two objects (they'll always differ between independently
built nodes and mean nothing about actual config drift) and before
re-POSTing an object to another node (the target assigns its own id/link).
"""
from __future__ import annotations
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ResourceDef:
    key: str                 # friendly name, used in config.yaml sync_resources
    ers_path: str             # URL segment - under /ers/config/ for ERS, under /api/v1/ for openapi
    strip_fields: tuple = ("id", "link", "lastUpdate")
    # some ERS objects nest their real body under a top-level wrapper key,
    # e.g. {"NetworkDeviceGroup": {...}} - set if so, else None
    wrapper_key: str | None = None
    # "ers" (default) or "openapi" - controls which client methods/base path are used
    api_type: str = "ers"


RESOURCES: dict[str, ResourceDef] = {
    "network_device_groups": ResourceDef(
        key="network_device_groups",
        ers_path="networkdevicegroup",
        wrapper_key="NetworkDeviceGroup",
    ),
    "identity_groups": ResourceDef(
        key="identity_groups",
        ers_path="identitygroup",
        wrapper_key="IdentityGroup",
    ),
    "endpoint_groups": ResourceDef(
        key="endpoint_groups",
        ers_path="endpointgroup",
        wrapper_key="EndPointGroup",
    ),
    "downloadable_acls": ResourceDef(
        key="downloadable_acls",
        ers_path="downloadableacl",
        wrapper_key="DownloadableAcl",
    ),
    "authorization_profiles": ResourceDef(
        key="authorization_profiles",
        ers_path="authorizationprofile",
        wrapper_key="AuthorizationProfile",
    ),
    "network_devices": ResourceDef(
        key="network_devices",
        ers_path="networkdevice",
        wrapper_key="NetworkDevice",
    ),
}

# --- POLICY SETS (Open API, confirmed working on 3.1+ including 3.4) ---
#
# These are NOT ERS objects on any release - only Open API. Confirmed
# endpoints (verified against Cisco's own OpenAPI walkthrough docs):
#   GET/POST /api/v1/policy/network-access/policy-set
#   GET/POST /api/v1/policy/device-admin/policy-set
# The list response returns full policy-set bodies inline (name, rank,
# state, top-level condition, service name) - no separate per-ID detail
# fetch needed, unlike ERS.
#
# These are deliberately kept OUT of the default `sync_resources` list -
# they're opt-in via `advanced_resources` in config.yaml. See README
# "Policy Sets" section before enabling: this covers only the top-level
# policy set container (order/condition/state), NOT the authentication
# and authorization RULES nested inside each set, which live under
# further sub-paths (.../policy-set/{id}/authorization,
# .../policy-set/{id}/authentication) and are intentionally not
# auto-pushed by this tool - getting rule order or exception handling
# wrong via a blind automated push is the highest blast-radius mistake
# you can make on a live RADIUS/TACACS engine.

ADVANCED_RESOURCES: dict[str, ResourceDef] = {
    "network_access_policy_sets": ResourceDef(
        key="network_access_policy_sets",
        ers_path="policy/network-access/policy-set",
        api_type="openapi",
        wrapper_key=None,
        strip_fields=("id", "link", "hitCounts"),
    ),
    "device_admin_policy_sets": ResourceDef(
        key="device_admin_policy_sets",
        ers_path="policy/device-admin/policy-set",
        api_type="openapi",
        wrapper_key=None,
        strip_fields=("id", "link", "hitCounts"),
    ),
    # "identity_groups" above is the GROUP container (Employee, Guest, etc).
    # "internal_users" is the actual account: name, enabled/disabled,
    # identity group membership, custom attributes.
    #
    # ERS never returns "password" on GET (write-only, security) but
    # requires it on POST create. Effect: diff/pull work fine; a
    # --apply CREATE of a brand-new user fails with a missing-field
    # error from ISE itself rather than silently setting a blank/guessed
    # password (safe failure, not a bug to work around); an UPDATE never
    # touches password since it's stripped from every push regardless.
    "internal_users": ResourceDef(
        key="internal_users",
        ers_path="internaluser",
        wrapper_key="InternalUser",
        strip_fields=("id", "link", "lastUpdate", "password"),
    ),
}

# Hard-blocked, not just "absent" - these are never syncable through this
# tool no matter what a future config.yaml or code edit says. Active
# Directory join state is node-local (machine account, domain trust,
# site/DC affinity) and pushing one node's AD config onto another can
# break its domain join outright. If AD ever needs to be scripted, that
# belongs in a separate, deliberately manual tool - not this diff/--apply
# pipeline.
BLOCKED_RESOURCES: set[str] = {
    "activedirectory",
    "active_directory",
}
