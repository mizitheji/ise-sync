# 🔄 ise-sync

Sync ERS-managed config objects between any number of Cisco ISE nodes that have **no shared deployment relationship**. Everything here is HTTPS calls to the ERS admin API — the same class of traffic the GUI uses. It never touches the `repository` / config-restore CLI path, so **no node ever reboots** 🚫🔁, in either sync direction.

Works with ISE **3.x** (ERS resources) and **3.1+** (policy sets via Open API).

---

## 🚀 Quick start

```bash
git clone https://github.com/mizitheji/ise-sync.git
cd ise-sync
pip install -r requirements.txt
cp config.example.yaml config.yaml   # edit with your node details
```

Enable ERS on each node: **Administration › Settings › ERS Settings › Enable ERS for Read/Write**. Use a dedicated ERS-enabled admin account, not your personal login. 🔐

---

## 🖥️ Adding your nodes

Open `config.yaml` and add one entry per ISE node under `nodes:`. You can define **as many nodes as you like** — any two can be compared or synced:

```yaml
nodes:

  hq-pan:
    base_url: "https://ise-hq-pan.example.com"
    env_prefix: "ISE_HQ"       # reads ISE_HQ_USER / ISE_HQ_PASS
    verify_ssl: false

  dr-standalone:
    base_url: "https://ise-dr.example.com"
    env_prefix: "ISE_DR"       # reads ISE_DR_USER / ISE_DR_PASS
    verify_ssl: false

  lab:
    base_url: "https://ise-lab.example.com"
    env_prefix: "ISE_LAB"      # reads ISE_LAB_USER / ISE_LAB_PASS
    verify_ssl: false

  # ➕ add more nodes here — no limit
```

The `env_prefix` field controls which environment variables are read for credentials. A node with `env_prefix: "ISE_SITEB"` reads `ISE_SITEB_USER` and `ISE_SITEB_PASS`.

---

## 🔑 Credentials

Credentials are **never stored in config.yaml** — they come from environment variables. Export them before running:

```bash
export ISE_HQ_USER=ise-api-account
export ISE_HQ_PASS='your-password'
export ISE_DR_USER=ise-api-account
export ISE_DR_PASS='your-password'
```

**💡 Tip:** save these in a local `.env` file (which is gitignored) and `source .env` before running.

| Node in config | `env_prefix` | Variables read |
|---|---|---|
| `hq-pan` | `ISE_HQ` | `ISE_HQ_USER` / `ISE_HQ_PASS` |
| `dr-standalone` | `ISE_DR` | `ISE_DR_USER` / `ISE_DR_PASS` |
| `lab` | `ISE_LAB` | `ISE_LAB_USER` / `ISE_LAB_PASS` |

---

## 🛠️ Usage

### 🔍 diff — read-only, never writes anything

```bash
# compare any two nodes by their name in config.yaml
python -m ise_sync.cli diff --from hq-pan --to dr-standalone
python -m ise_sync.cli diff --from dr-standalone --to hq-pan
python -m ise_sync.cli diff --from hq-pan --to lab
```

### 🔁 sync — dry run by default

```bash
# shows the same plan as diff, writes nothing
python -m ise_sync.cli sync --from hq-pan --to dr-standalone

# actually push creates and updates ✅
python -m ise_sync.cli sync --from hq-pan --to dr-standalone --apply
```

### 📦 pull — snapshot a node to JSON

```bash
# export one node's full config to a JSON file (useful for git-tracked audit trail)
python -m ise_sync.cli pull --node hq-pan --out snapshots/hq-pan.json
git add snapshots/ && git commit -m "ISE snapshot $(date -I)"
```

### 🧩 Policy sets (opt-in)

Add `--include-advanced` to include top-level policy set containers (name, rank, condition, state). Read the [Policy Sets](#-policy-sets) section before using `--apply` with this flag. ⚠️

```bash
python -m ise_sync.cli diff --from hq-pan --to dr-standalone --include-advanced
python -m ise_sync.cli sync --from hq-pan --to dr-standalone --include-advanced --apply
```

### 📋 Example diff output

```text
╔════════════════════════════════════════════════════════╗
║                hq-pan  →  dr-standalone                ║
╚════════════════════════════════════════════════════════╝

────────────────────────────────────────────────────
  Network Device Groups
    ✚ create 1   ~ update 1   · 1 unchanged
    ✚  Device Type#All Device Types#Firewall
    ~  Device Type#All Device Types#Switch  (1 field group differ)
    ?  1 extra in target  (not touched — review manually)
       •  Location#All Locations#Legacy-DC

────────────────────────────────────────────────────
  Authorization Profiles
    ✚ create 1   ~ update 0   · 1 unchanged
    ✚  Contractors_Quarantine

────────────────────────────────────────────────────
  Network Devices
    ✚ create 0   ~ update 0   · 1 unchanged
    ✔  in sync

────────────────────────────────────────────────────
  DRY RUN  ✚ 2 to create  ~ 1 to update  ? 1 extra in target
  Re-run with --apply to push changes.
```

---

## 📚 What gets synced

Objects are matched **by name** (not internal UUID) — two independently-built ISE nodes never share UUIDs for the same logical object. `sync_resources` in `config.yaml` controls which ERS object types are in scope, in dependency-safe order:

| Resource | ERS type | Notes |
|---|---|---|
| Network Device Groups | `networkdevicegroup` | parent of Network Devices |
| Identity Groups | `identitygroup` | |
| Endpoint Groups | `endpointgroup` | |
| Downloadable ACLs | `downloadableacl` | |
| Authorization Profiles | `authorizationprofile` | |
| Network Devices | `networkdevice` | |

`internaluser` (Internal Users) is opt-in via `--include-advanced`, not part of the default table above — see Safety notes. 🔒

**🔒 Passwords are never synced.** After syncing an `internal_users` create, set the password on the target node manually via the ISE GUI.

**🗑️ Deletes are never automatic.** Objects in the target that have no matching source object appear as "extra in target" in the report for manual review. Silently deleting config on a live RADIUS/TACACS engine is exactly the kind of blast radius this tool avoids.

**🚫 Active Directory is hard-blocked.** `activedirectory` is rejected in code (`resources.py → BLOCKED_RESOURCES`) regardless of `config.yaml` — domain join state (machine account, site/DC affinity, trust) is node-local, and pushing one node's AD config onto another can break its domain join outright.

Objects matching `exclude_name_patterns` (regex) are skipped in both source and target:

```yaml
exclude_name_patterns:
  - "^DR-LOCAL-.*"    # skip anything named DR-LOCAL-<anything>
  - "^LAB-.*"         # skip all lab-prefixed objects
```

---

## 🧩 Policy sets

Confirmed working on ISE **3.1+** via Open API (not ERS — policy sets have never been ERS objects):

```
GET/POST /api/v1/policy/network-access/policy-set
GET/POST /api/v1/policy/device-admin/policy-set
```

Enable Open API separately: **Administration › System › Settings › API Settings › API Service Settings**.

**✅ What `--include-advanced` covers:** the policy set *container* — name, rank (evaluation order), top-level condition, service name, enabled/disabled state.

**⛔ What it deliberately does NOT cover:** the authentication and authorization *rules* nested inside each set. Auto-pushing rule changes is where this tool stops: rule order and exception handling are the highest blast-radius part of ISE to get wrong via a blind automated push.

**➡️ Safe order:** sync the default resources first (NDGs, identity groups, authz profiles, ACLs, network devices) — policy sets reference those by name, so getting the flat objects in sync first avoids dangling references on the target.

---

## ⚠️ Safety notes

- `sync` is read+diff by default — `--apply` is the only thing that writes. Nothing pushes without that explicit flag. 🛡️
- Run `diff` in **both directions** before your first `--apply`. With two independently-managed nodes it's easy to have drifted in both directions rather than cleanly one way. 🔄
- `internal_users` and policy sets are opt-in via `--include-advanced` — not touched by a plain `diff`/`sync`. 🧩
- Coloured output is on by default. Set `NO_COLOR=1` to disable it when piping to a log file. 🎨
- `deepdiff` is used for field-level comparison so you see *why* something is flagged as changed, not just that it is. 🔎

---

## 🗂️ Project layout

```
ise-sync/
├── config.example.yaml   ← template — copy to config.yaml and edit
├── config.yaml           ← your local config (gitignored)
├── requirements.txt
└── ise_sync/
    ├── cli.py            ← entry point (diff / sync / pull commands)
    ├── client.py         ← ISE ERS + Open API HTTP client
    ├── diff.py           ← diff engine + output formatting
    ├── resources.py      ← registry of syncable resource types
    └── colours.py        ← ANSI colour helpers
```

---

## ✅ Requirements

- Python 3.9+ 🐍
- ISE 3.x with ERS enabled
- ISE 3.1+ for policy set support (`--include-advanced`)

```
requests>=2.31.0
PyYAML>=6.0
deepdiff>=6.7.0
```
