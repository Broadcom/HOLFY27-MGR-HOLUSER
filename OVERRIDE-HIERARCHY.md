# Override Hierarchy

This repository uses a **5-tier file resolution system** that allows lab teams to customize startup scripts, shutdown scripts, configuration files, firewall rules, and proxy settings — without ever modifying core code.

## How It Works

When the startup system needs a file (a Python module, an `.ini` config, a firewall script, etc.), it searches **five locations in priority order**. The first match wins:

```
Priority 1 (highest)  /vpodrepo/20XX-labs/XXYY/{subfolder}/{file}     Lab-specific override
Priority 2            /vpodrepo/20XX-labs/XXYY/{file}                  Lab root override
Priority 3            /home/holuser/{LABTYPE}/{subfolder}/{file}        External team repo
Priority 4            /home/holuser/hol/{LABTYPE}/{subfolder}/{file}    In-repo labtype override
Priority 5 (lowest)   /home/holuser/hol/{subfolder}/{file}             Core default
```

- **Priorities 1-2**: Lab-specific. These come from the lab's own git repository (vpodrepo), cloned to `/vpodrepo/` at boot.
- **Priority 3**: Team-wide. External team repos cloned to `/home/holuser/{LABTYPE}/` (e.g., `/home/holuser/ATE/`).
- **Priority 4**: Team-wide. Labtype directories inside this repo (e.g., `ATE/`, `DISCOVERY/`, `HOL/`).
- **Priority 5**: Core defaults. The `Startup/`, `Shutdown/`, `Tools/`, `holodeck/`, `holorouter/`, and `console/` directories at the repo root.

## Overridable Subdirectories

Each lab type directory mirrors the core directory structure:

| Subdirectory | Purpose | Core Location |
|---|---|---|
| `Startup/` | Python startup modules (`prelim.py`, `VCF.py`, etc.) | `Startup/` |
| `Shutdown/` | Python shutdown modules | `Shutdown/` |
| `Tools/` | Utility scripts | `Tools/` |
| `holodeck/` | `.ini` configuration templates | `holodeck/` |
| `holorouter/` | Firewall (`iptablescfg.sh`) and proxy configs | `holorouter/` |
| `console/` | Console VM customization scripts | `console/` |

## Startup Module Sequence

All lab types share the same default startup sequence (defined in `Tools/labtypes.py`):

```
prelim → ESXi → VCF → VVF → vSphere → pings → services → Kubernetes → urls → VCFfinal → final → odyssey
```

Each module in the sequence is resolved through the override hierarchy independently. This means you can override just `VCFfinal.py` for ATE labs while keeping every other module at the core default.

## Worked Examples

### Example 1: Override a Startup Module for ATE Labs

**Goal**: All ATE labs need a custom `vSphere.py` that skips DRS configuration.

1. Copy the core module:

```bash
cp Startup/vSphere.py ATE/Startup/vSphere.py
```

2. Edit `ATE/Startup/vSphere.py` — make your changes in the `CUSTOM` section.

3. Commit to this repo. Every ATE lab will now use the custom `vSphere.py`, while HOL, VXP, Discovery, and EDU labs continue using the core version.

### Example 2: Lab-Specific Config Override via vpodrepo

**Goal**: Lab HOL-2705 needs custom URL checks and a different vCenter list.

1. In the lab's vpodrepo (`/vpodrepo/2027-labs/2705/`), create a `config.ini`:

```ini
[VPOD]
vPod_SKU = HOL-2705
labtype = HOL

[RESOURCES]
vCenters = vc-mgmt-a.site-a.vcf.lab:linux:administrator@vsphere.local
URLS = https://vc-mgmt-a.site-a.vcf.lab/ui,VMware vSphere
```

2. This `config.ini` takes priority over any `holodeck/*.ini` file because vpodrepo (Priority 1-2) outranks everything.

### Example 3: Custom Firewall Rules for VXP Labs

**Goal**: VXP labs need different iptables rules than HOL labs.

1. Create `VXP/holorouter/iptablescfg.sh` with the custom rules.

2. The startup system will find `VXP/holorouter/iptablescfg.sh` at Priority 4, ahead of the core `holorouter/iptablescfg.sh` at Priority 5.

### Example 4: Lab-Specific Startup Module via vpodrepo

**Goal**: Lab HOL-2710 needs a completely custom `final.py` that configures an extra demo environment.

1. In the vpodrepo, create `Startup/final.py` (or place it at the repo root as `final.py`):

```
/vpodrepo/2027-labs/2710/
  Startup/
    final.py          ← Priority 1: overrides all other final.py files
  config.ini
```

2. Only lab HOL-2710 uses this custom `final.py`. All other labs remain unaffected.

### Example 5: Holodeck INI Override for a Lab Type

**Goal**: All Discovery labs should use a simplified default configuration.

1. Create `DISCOVERY/holodeck/defaultconfig.ini` with the simplified settings.

2. When a Discovery lab boots without a vpodrepo, `labstartup.sh` finds this at Priority 4 before falling back to `holodeck/defaultconfig.ini` at Priority 5.

## Implementation Details

- **Python modules**: Resolved by `LabTypeLoader.get_module_path()` in `Tools/labtypes.py`. Loaded dynamically via `importlib`.
- **INI config files**: Resolved by `use_local_holodeck_ini()` in `labstartup.sh` (bash) and `LabTypeLoader.get_override_path()` in `Tools/labtypes.py` (Python).
- **Router files** (firewall, proxy): Pushed to the router VM via NFS by `lsfunctions.push_router_files()` and `push_vpodrepo_router_files()`.

## Quick Reference: Where to Put Your Files

| What You Want | Where to Put It |
|---|---|
| Override for **one specific lab** | vpodrepo: `/vpodrepo/20XX-labs/XXYY/` |
| Override for **all labs of a type** (in this repo) | `{LABTYPE}/` directory (e.g., `ATE/Startup/`) |
| Override for **all labs of a type** (external repo) | `/home/holuser/{LABTYPE}/` on the manager VM |
| Change the **core default** for everyone | Root-level directories (`Startup/`, `holodeck/`, etc.) |
