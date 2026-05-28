# Discovery Startup Overrides

Place Discovery-specific startup module overrides here. These apply to **all Discovery labs**.

For lab-specific overrides, place them in your vpodrepo instead.

See [OVERRIDE-HIERARCHY.md](../../OVERRIDE-HIERARCHY.md) for full documentation on the 5-tier override system.

## How to Override a Module

1. Copy the core module you want to customize:

```bash
cp ../../Startup/VCFfinal.py ./VCFfinal.py
```

2. Edit your copy. Place custom code in the `CUSTOM` section at the bottom of the module.

3. Commit to this repo. The override takes effect on next boot for all Discovery labs.

## Discovery-Specific Notes

- Discovery labs run with **no firewall and no proxy filtering** (`firewall: False`, `proxy_filter: False`)
- Uses `named` repo pattern: SKU format is `Discovery-Name` (not year-based like `HOL-XXYY`)
- `prelim.py` override (v3.11-DISCOVERY1) actively:
  - Re-pushes `holorouter/nofirewall.sh` → `/tmp/holorouter/iptablescfg.sh` (belt-and-suspenders vs. labstartup.sh)
  - Logs `DISCOVERY: proxy not required — PROXY_URL="" NO_PROXY=""`
  - Calls `lsf.clear_vscode_proxy()` to clear any stale `http.proxy` / `http.noProxy` from VS Code settings.json on the console
- `VCFfinal.py` override (v6.3.12-DISCOVERY1) actively clears all 4 proxy targets written by `confighol-9.1.py`:
  - **Target 1** vCenter(s) OS: `/etc/environment`, `/etc/sysconfig/proxy`, VAMI REST no-proxy cleared — via `supervisor_stabilizer.py --clear-proxy` Phase 0
  - **Target 2** Supervisor CP node OS: `/etc/environment`, containerd drop-in cleared — via `supervisor_stabilizer.py --clear-proxy` Phase 2
  - **Target 3** Supervisor API: `cluster_proxy_config` PATCHed empty via `lsf.clear_supervisor_api_proxy()`
  - **Target 4** VSP node(s) OS: `/etc/sysconfig/proxy`, `/etc/environment`, containerd + kubelet drop-ins cleared — via `lsf.clear_vsp_node_proxy()` per node
- **Hosts NOT configured with proxy by confighol** (no clearing needed): ESXi, NSX Manager/Edges, VCF Operations, SDDC Manager, VCF Automation
