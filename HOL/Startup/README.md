# HOL Startup Overrides

Place HOL-specific startup module overrides here. These apply to **all HOL labs** across all SKUs.

For lab-specific overrides (a single SKU), place them in your vpodrepo instead.

See [OVERRIDE-HIERARCHY.md](../../OVERRIDE-HIERARCHY.md) for full documentation on the 5-tier override system.

## How to Override a Module

1. Copy the core module you want to customize:

```bash
cp ../../Startup/VCFfinal.py ./VCFfinal.py
```

2. Edit your copy. Place custom code in the `CUSTOM` section at the bottom of the module:

```python
##=========================================================================
## CUSTOM code section - place your custom code here
##=========================================================================

# Your HOL-specific customizations here
```

3. Commit to this repo. The override takes effect on next boot for all HOL labs.

## Available Modules

| Module | Description |
|---|---|
| `prelim.py` | DNS checks, README copy, proxy setup |
| `ESXi.py` | ESXi host SSH verification, maintenance mode |
| `VCF.py` | VCF component boot (NSX, vCenter, edges) |
| `VVF.py` | VVF-specific startup |
| `vSphere.py` | Cluster config, DRS, autostart |
| `pings.py` | Network connectivity checks |
| `services.py` | Windows/Linux service verification |
| `Kubernetes.py` | K8s cluster health checks |
| `urls.py` | URL availability verification |
| `VCFfinal.py` | VCF Automation, Tanzu, Supervisor |
| `final.py` | Cleanup, ready signal, labcheck cron |
| `odyssey.py` | Odyssey client installation |
