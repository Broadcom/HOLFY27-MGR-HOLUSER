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
- Consider overriding `prelim.py` to skip proxy-related checks
