# Module Override Priority

1. `/vpodrepo/20XX-labs/XXXX/Startup/{module}.py` (highest)
2. `/vpodrepo/20XX-labs/XXXX/{module}.py`
3. `/home/holuser/{labtype}/Startup/{module}.py` (external team override repo)
4. `/home/holuser/hol/{labtype}/Startup/{module}.py` (in-repo labtype override)
5. `/home/holuser/hol/Startup/{module}.py` (lowest)

## Supported Lab Types

Lab Types are case sensitive

| Type | Description |
| ------ | ------------- |
| HOL | Hands-on Labs |
| Discovery | Discovery Labs |
| VXP | VCF Experience Program |
| ATE | Advanced Technical Enablement (Livefire) |
| EDU | Education/Training |

### Startup Sequences

All lab types use the same comprehensive startup sequence (skipping modules that don't apply):

**HOL / Discovery / VXP / ATE / EDU**:

```bash
prelim → ESXi → VCF → VVF → vSphere → pings → services → Kubernetes → urls → VCFfinal → final → odyssey
```

> **Note:** Individual modules detect if their components are configured. If not present in `config.ini`, the module runs but skips unconfigured checks.
