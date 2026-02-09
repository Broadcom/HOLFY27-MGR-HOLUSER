# Commented-out options in the config.ini

These are still referenced by code in VCFshutdown.py but are not required:

| Config Key | VCFshutdown.py Task | Why Commented | How Code Handles It |
| ---------- | ------------------- | ------------- | ------------------- |
| vcf_ops_networks_vms | Task 8 (Phase 8) | Under review | Code uses hardcoded defaults (opsnet-a, opsnet-01a, opsnetcollector-01a) and only reads config if the option exists |
| vcf_ops_collector_vms | Task 9 (Phase 9) | Under review | Defaults: opscollector-01a, opsproxy-01a |
| vcf_ops_logs_vms | Task 10 (Phase 10) | Under review | Defaults: opslogs-01a, ops-01a, ops-a |
| vcf_identity_broker_vms | Task 11 (Phase 11) | Under review | Defaults to empty list (not all deployments have it) |
| vcf_ops_fleet_vms | Task 12 (Phase 12) | Under review | Defaults: opslcm-01a, opslcm-a |
| vcf_ops_vms | Task 13 (Phase 13) | Under review | Defaults: o11n-02a, o11n-01a |
| sddc_manager_vms | Task 16 (Phase 16) | Under review | Default: sddcmanager-a |

## Key findings

Every commented-out config option has a corresponding hardcoded default in VCFshutdown.py. The code pattern is: check if the config option exists → if yes, use it; if no, fall back to the hardcoded default VM names. This means shutdown is currently functioning correctly using the hardcoded defaults because the config options are commented out. Uncommenting them would allow operators to override the default VM names without changing code, but the current behavior is already correct for standard VCF 9.0 deployments.