# VXP Startup Overrides

Place VXP (VCF Experience Program) startup module overrides here. These apply to **all VXP labs**.

For lab-specific overrides (a single SKU), place them in your vpodrepo instead.

See [OVERRIDE-HIERARCHY.md](../../OVERRIDE-HIERARCHY.md) for full documentation on the 5-tier override system.

## How to Override a Module

1. Copy the core module you want to customize:

```bash
cp ../../Startup/VCFfinal.py ./VCFfinal.py
```

2. Edit your copy. Place custom code in the `CUSTOM` section at the bottom of the module.

3. Commit to this repo. The override takes effect on next boot for all VXP labs.

## VXP-Specific Notes

- VXP labs use both firewall and proxy filtering (same as HOL)
- VXP has a custom holodeck config: `VXP/holodeck/VCF9-VKS-D.ini`
- Demo environments may need additional URL checks in `urls.py` for demo-specific endpoints
