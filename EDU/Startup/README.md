# EDU Startup Overrides

Place EDU (Education/Training) startup module overrides here. These apply to **all EDU labs**.

For lab-specific overrides (a single SKU), place them in your vpodrepo instead.

See [OVERRIDE-HIERARCHY.md](../../OVERRIDE-HIERARCHY.md) for full documentation on the 5-tier override system.

## How to Override a Module

1. Copy the core module you want to customize:

```bash
cp ../../Startup/VCFfinal.py ./VCFfinal.py
```

2. Edit your copy. Place custom code in the `CUSTOM` section at the bottom of the module.

3. Commit to this repo. The override takes effect on next boot for all EDU labs.

## EDU-Specific Notes

- EDU labs use both firewall and proxy filtering (same as HOL)
- Consider overriding `final.py` to adjust `labcheckinterval` for training sessions
- Training environments may benefit from a custom `prelim.py` with additional pre-flight validation
