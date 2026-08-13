#!/usr/bin/env bash
# =============================================================================
# remediate-lab.sh   (v3.0.1-draft - 2026-08-03, Ben Sier + HOL Core Team)
#
# ONE combined, idempotent, one-shot remediation for a whole HOL VCF pod. Merges
# hol-remediate.sh (VCFA node + VSP control-plane static-manifest fixes; Families
# A/B/C) and vsp-remediate.sh (VSP fleet-cluster node-sizing / footprint /
# leader-election-cascade fixes) into a single script.
#
#   NO ARGS  = FULL, IDEMPOTENT REMEDIATION: every fix from both scripts, in a
#              safe order, each step performed ONLY if discovery shows it is
#              actually needed (drifted / unhealthy). Preflight makes the
#              otherwise-"unsafe" steps safe, so no extra flag is required for
#              the default run.
#   FLAGS    = individual actions retained for targeted one-off testing (see
#              --help). The default (no args) runs the complete remediation.
#
# ACCESS MODEL (all hops proxied; nothing runs on a node being power-cycled):
#     local --sshpass--> manager/jump host (holuser@MGR_HOST:5480)   [govc lives here]
#           --sshpass--> cluster node (vmware-system-user@<ip>, sudo -> root kubectl)
#   Two clusters are touched, each via its own control-plane node's admin.conf:
#     * VSP fleet cluster  -- via its CP node   (default 10.1.1.142)
#     * VCFA (auto-a) node -- its own single CP  (default 10.1.1.70)
#   VSP-fleet-only actions (node resize, right-size, HA reduce, safe-to-evict,
#   disable-capi-le, autoscaler pin, consolidate) run ONLY against the VSP
#   cluster. Families A/B/C run per-node on BOTH nodes, each against its own
#   apiserver. All creds/IPs are env-overridable (see CONFIG); creds differ
#   per pod, so preflight verifies connectivity and fails with a clear message
#   rather than hammering auth (one password attempt per credential, no keys).
#
# THE INCIDENT THIS DRAFT EXISTS TO NOT REPEAT (CP-readiness, see #3 in the
# task / VSP-DEEPDIVE-HANDOFF §8): a prior CP power-cycle polled ONLY the
# kube-vip VIP (10.1.1.142), which is DOWN while the CP reboots -> false
# "CP down" -> the cluster was left PAUSED. This script instead: waits for
# guest boot via govc; polls the CP's REAL node IP against the LOCAL apiserver
# https://127.0.0.1:6443 (independent of the VIP); verifies identity
# (hostname==CP + etcd/apiserver static pods) and Ready at the NEW size;
# retries through connection errors for the full window; and concludes "CP
# down" ONLY if the real node IP is unreachable for the entire window. Unpause
# is owned by an EXIT trap that ALWAYS attempts unpause (webhook-resilient,
# ~8x) unless the CP is verifiably down.
#
# Full mechanism/caveat write-ups live in the two toolkit READMEs and
# VSP-DEEPDIVE-HANDOFF.md -- NOT dumped by --help (which is intentionally terse).
#
# Exit: 0 ok, 1 error, 2 cannot reach manager/node, 3 guard/identity refused.
# THIS IS A REVIEW DRAFT -- do NOT run against a live lab until the lead signs off.
# =============================================================================
set -euo pipefail

# ── CONFIG (override any of these via env for a different pod) ────────────────
# No default passwords are baked in on purpose -- MGR_PW and NODE_PW are REQUIRED env vars
# (validated below, right after arg parsing). Every other value here has a safe, non-secret
# default; credentials never do.
MGR_HOST="${MGR_HOST:-10.138.150.5}"; MGR_PORT="${MGR_PORT:-5480}"
MGR_USER="${MGR_USER:-holuser}";      MGR_PW="${MGR_PW:-}"
NODE_USER="${NODE_USER:-vmware-system-user}"
NODE_PW="${NODE_PW:-}"
VSP_CP_IP="${VSP_CP_IP:-10.1.1.142}"
# AUTOA_IP defaults to the kube-vip SERVICE VIP (.70), not a real node IP. resolve_autoa_ip()
# (called once after arg parsing, below) replaces this with the real node IP when one answers --
# see that function for why. AUTOA_IP_EXPLICIT tracks whether the caller (env var or --autoa) set
# this themselves, so auto-discovery never overrides an explicit choice.
AUTOA_IP_EXPLICIT=0; [ -n "${AUTOA_IP:-}" ] && AUTOA_IP_EXPLICIT=1
AUTOA_IP="${AUTOA_IP:-10.1.1.70}"
# AUTOA_VIP is ALWAYS the kube-vip service VIP specifically -- deliberately kept separate from
# AUTOA_IP, which resolve_autoa_ip() below overwrites with a real node IP for the management SSH
# hop. INCIDENT (2026-08-12, HOL-2711 10.138.150.5): verify_vcfa_ready()'s login-page check used to
# hardcode 10.1.1.70 and got "fixed" to use $AUTOA_IP instead (matching the management hop) -- but
# the login page is served via the VIP specifically (10.1.1.70), NOT the node's own static IP
# (10.1.1.71 on this lab): curl -v confirmed "Connection refused" on :443 via the node's own IP
# while kube-vip held the VIP just fine. The management hop and the "is the actual service up"
# check are different concerns that happen to often be interchangeable on a single-node appliance
# (both go to the same VM) -- except for the ONE address (443/VIP) kube-vip specifically owns.
AUTOA_VIP="${AUTOA_VIP:-10.1.1.70}"
# Companion VCFA storm-mitigation script (footprint + vksm probe relax + kube-vip
# validity guard [apply]; disable-le / logging as opt-in). Runs ON the auto-a node.
VCFA_MIT="${VCFA_MIT:-$(cd "$(dirname "$0")/.." 2>/dev/null && pwd)/vcfa-storm-mitigation.sh}"  # toolkit/vcfa-storm-mitigation.sh; falls back to the embedded copy
GOVC="${GOVC:-/home/holuser/govc}";   GOVC_ENV="${GOVC_ENV:-/home/holuser/.govc-vsp01a.env}"
VM_FOLDER="${VM_FOLDER:-/dc-mgmt-a/vm/vcf-management-services}"
# AMD Zen4/5 (EPYC 9004/9005) esxcli entropySources workaround target (2=RDRAND, avoids the slow
# RDSEED default). See do_entropy_fix() below for the full mechanism/citation.
ENTROPY_TARGET="${ENTROPY_TARGET:-2}"
# Cluster name/ns are DISCOVERED at preflight; these are only fallbacks.
CLS_NAME="${CLUSTER:-vsp-01a}"; CLS_NS="${NS:-vmsp-platform}"

# Family (B) lease tuning -- leaseDuration > renewDeadline > retryPeriod, renew
# giving margin over the observed 24.65s worst-case guest stall (RCA §0.3).
LEASE_DURATION="60s";  RENEW_DEADLINE="40s";  RETRY_PERIOD="6s"
VIP_LEASE_DURATION="60"; VIP_RENEW_DEADLINE="40"; VIP_RETRY_PERIOD="6"
ETCD_CPU_REQUEST="2500m"
# Family (C) targets (patch a ReleaseTemplate, never the HelmRelease directly).
KYVERNO_RESYNC_TARGET="1h"
EG_MEM_LIMIT="8Gi"; EG_MEM_REQUEST="1536Mi"

# CP-readiness poll window (seconds). Comfortably longer than the ~9-min
# post-reboot reconnection storm documented in VSP-DEEPDIVE-HANDOFF §8.
CP_READY_WINDOW="${CP_READY_WINDOW:-900}"
# Auto-consolidation target: reduce the worker MachineDeployment down to this many
# nodes (self-selecting drainable nodes, cordon-first so VCF lifecycle churn can't
# reset the autoscaler's unneeded-timer), then disable the autoscaler to pin it.
CONSOLIDATE_TARGET="${CONSOLIDATE_TARGET:-4}"

# ── runtime state ─────────────────────────────────────────────────────────────
DO_VSP=1; DO_VCFA=1; ACTION="full"; ARG=""; FORCE=0
VSP_OK=0; VCFA_OK=0
PAUSED_BY_US=0; CP_VERIFIED_DOWN=0
# discovery outputs (filled by discover_vsp)
CP_NODE=""; CP_REAL_IP=""; CP_CAP_CPU=""
CP_TMPL_CPU=""; CP_TMPL_MEM=""; W_TMPL_CPU=""; W_TMPL_MEM=""
MD=""; AS_RT=""; AS_RT_REPLICAS=""; MD_REPLICAS=""; PAUSED_STATE=""

usage() {
  cat <<'USAGE'
remediate-lab.sh -- one-shot, idempotent HOL VCF pod remediation (VSP fleet + VCFA).

  remediate-lab.sh                 FULL remediation (default). Every fix from both
                                   toolkits, safe order, each step only if drifted.

Scope / overrides:
  --vsp-only              only the VSP fleet cluster + its CP node
  --vcfa-only             only the VCFA (auto-a) node
  --vsp-cp IP             VSP control-plane node IP        (default 10.1.1.142)
  --autoa  IP             VCFA (auto-a) node IP            (default 10.1.1.70)
  --mgr    HOST           jump-host/manager IP             (default 10.138.150.5)
  --force                 override consolidate safety guards
  -h, --help              this help

Read-only:
  --status                combined status: VSP cluster + per-node families A/B/C

VSP fleet cluster (kubectl, non-disruptive):
  --right-size-requests   cut oversized vodap/ops-logs CPU/mem requests
  --reduce-ha             scale leader-election controllers + coredns 2->1
  --safe-to-evict         annotate vodap collectors safe-to-evict
  --disable-capi-le       --leader-elect=false on the 5 clusterctl CAPI/CAPV ctrls
  --disable-autoscaler    pin autoscaler off (RT replicaCount=0)  [alias --pin-autoscaler]
  --enable-autoscaler     re-enable autoscaler                    [alias --unpin-autoscaler]
  --consolidate [NODE]    no NODE = AUTO: self-select drainable workers, cordon+drain
                          +remove down to CONSOLIDATE_TARGET (=${CONSOLIDATE_TARGET:-4}), then pin
                          autoscaler off (this runs in the default no-arg pass too).
                          With NODE = remove that one node.

VSP fleet cluster (power-cycle, pause-guarded, fixed CP-readiness):
  --cp-resize [C[/M]]     resize CP to template default (or C vCPU [/ M MiB]) if drifted
  --worker-resize [C]     resize drifted workers to template default (or C vCPU), 1 at a time
  --pause / --unpause     set/clear Cluster spec.paused (webhook-resilient)

Physical/nested-host layer (govc; config-only, NEVER reboots):
  --entropy-fix           set esxcli entropySources=${ENTROPY_TARGET:-2} (RDRAND) on every nested ESXi
                          host under the vCenter, if not already set -- works around AMD Zen4/5
                          (EPYC 9004/9005) CPUs burning host CPU on slow RDSEED entropy generation
                          ("NRandomHwrng: Out of entropy, refreshing" in vmkernel.log). Sets the
                          Configured value and verifies it took; does NOT reboot any host, so the
                          fix is NOT live (Runtime stays unchanged) until each host is rebooted
                          manually. Runs in the default no-arg pass too (still no reboot there).

Per-node families A/B/C (both nodes unless scoped):
  --keepers               install/refresh Family A drift-keeper timers
  --apply-lease           Family B: KCM/scheduler lease + etcd CPU request
  --revert-lease          revert lease/etcd from last backup
  --etcd-compaction       enable etcd auto-compaction + one-time defrag
  --kube-vip-status       report kube-vip on-disk-file vs Cluster-variable drift
  --kube-vip-apply        Family B: fix kube-vip lease (file-only, no VM replace)
  --kube-vip-cluster-patch  DISRUPTIVE: patch Cluster var (CP VM replace); CONFIRM-gated
  --kcp-patch             print (never apply) the equivalent KCP lease patch
  --kyverno-resync-relax  Family C: relax kyverno background-controller resyncPeriod
  --envoy-gateway-fix     Family C: envoy-gateway mem + leader-election-disable
  --vcfa-stabilize [ACT]  VCFA auto-a CPU-storm mitigation via companion script (runs on auto-a):
                          ACT = apply (default; footprint + vksm probe-relax + kube-vip validity
                          guard + kube-vip VIP-preserve + data-plane Envoy proxy hardening)
                          | harden-gateway (just the data-plane Envoy proxy lever: probe
                          failureThresholds + restore the shutdown-manager /tmp mount -- kills the
                          5-6 min ':443 Unable to connect' outages; rolls each proxy pod once)
                          | harden-uitier (just the UI-tier lever: give the 7 user-facing prelude
                          workloads a CPU/mem request so they leave BestEffort (cpu.weight 1) --
                          halves UI latency (avg 0.25->0.11s, worst 20.6->10.7s); rolls those pods
                          once. PARTIAL: a node-wide transient still causes an occasional ~10s tail)
                          | disable-le | logging (cell restart!) | status | revert
  --remove                uninstall all Family A keepers (leaves B/C objects patched)
USAGE
}


# ── EMBEDDED COMPANION ─────────────────────────────────────────────────────────
# remediate-lab.sh is a SINGLE-FILE one-shot: copy just this script to any jump host
# and it works. It prefers an external ../vcfa-storm-mitigation.sh when present (so the
# companion stays independently editable/runnable on a node), and otherwise materialises
# the copy embedded below. Regenerate the embed after editing the companion with:
#     BenS/sync-embedded-companion.sh
materialise_companion() {
  [ -f "$VCFA_MIT" ] && return 0                    # external copy wins
  VCFA_MIT="$TMP/vcfa-storm-mitigation.sh"
  cat > "$VCFA_MIT" <<'__VCFA_MIT_EMBEDDED__'
#!/usr/bin/env bash
# =============================================================================
# vcfa-storm-mitigation.sh   (v2, 2026-07-30, Ben Sier + HOL Core Team)
# RUNS ON the VCFA / auto-a control-plane node as root (uses admin.conf).
# Invoked standalone, or pushed+run by remediate-lab.sh --vcfa-stabilize.
#
# Durable, idempotent, REVERSIBLE in-guest mitigation for the recurring ~10-min
# CPU storm on the single-node VCF Automation appliance. RCA: in-guest
# concurrency (blocked tasks at ~72% CPU), NOT hypervisor steal (auto-a is
# DRS-pinned away from the VSP nodes to avoid co-stop) and NOT vCenter-slow.
# See 2026-07-30-post-reboot-2701-2711-RCA.md.
#
# ACTIONS:
#   apply       Idempotent + durable. No cell restart. NOTE: the last two levers each roll
#               their pods ONCE (observed zero-downtime; a brief transient 500 was seen):
#                 - CAPI/CAPV controllers (5) 2->1 + --leader-elect=false   [clusterctl -> sticks]
#                 - coredns 2->1                                            [etcd -> survives reboot]
#                 - kyverno CLEANUP resyncPeriod 15m->1h via ReleaseTemplate [operator-rendered]
#                 - vksm control-tier liveness/readiness probe relax (tight 1s -> 10s/8)
#                     [ns prelude; Flux driftDetection is OFF -> direct patch STICKS across
#                      reconcile+reboot; wiped only by a chart upgrade -> also a product ask]
#                 - kube-vip static-manifest lease VALIDITY guard (repairs invalid ordering
#                     renewdeadline>=leaseduration that panic-crashloops the CP-VIP pod)
#                 - SERVICE kube-vip vip_preserve_on_leadership_loss=true + lease 60/40/6 via RT
#                 - DATA-PLANE Envoy proxy hardening via the EnvoyProxy CR (probe failureThresholds
#                     + restore the shutdown-manager /tmp mount) -- kills the 5-6 min ":443 Unable
#                     to connect" outages. Rolls each proxy pod ONCE (was zero-downtime on 2701).
#                 - USER-FACING UI TIER out of BestEffort: the 7 prelude UI/proxy workloads ship
#                     with resources:{} -> QoS BestEffort -> cgroup cpu.weight *1* (vs ~750 for
#                     burstable), so under storm they cannot finish a TLS handshake and the UI
#                     takes 11-24 s. Adds a 200m/64Mi REQUEST (no limits). Rolls those pods ONCE.
#                     PARTIAL: halves avg and worst-case UI latency and removes the pod-side
#                     mechanism, but a node-wide transient still produces an occasional ~10 s tail.
#   harden-gateway  Just the data-plane Envoy proxy lever (+ status). Idempotent.
#   harden-uitier   Just the UI-tier QoS lever (+ status). Idempotent.
#   disable-le  OPT-IN (causes rollouts). Disables leader-election on the replicas==1 vksm
#               control services whose LE is an arg (account/authentication/dataprotection) to
#               break the lease-loss cascade under storm. Guarded on replicas==1. NOTE: the
#               configmap-driven LE services (policy-engine/policy-insights/cluster-*) are left
#               for a validated follow-up (their probe relax already applied). EXPERIMENTAL.
#   logging     OPT-IN, DISRUPTIVE (cell restart). Sets the tenant-manager-logback ConfigMap
#               DEBUG/TRACE->INFO (kills the property-collector debug firehose = ~80% of cell
#               CPU) and restarts the cell to apply. Durable (driftDetection off); wiped by a
#               chart upgrade -> product ask to set it in the helm values.
#   status      Report current state of every lever. (default)
#   revert      Undo apply (+disable-le); restore logging only if a backup exists.
#
# Backups: /root/manifest-bak/  (replicas, probes, kyverno RT, kube-vip manifest, logback CM).
# =============================================================================
set -u
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s"
BAK=/root/manifest-bak; mkdir -p "$BAK" 2>/dev/null || true
NS=vmsp-platform          # CAPI/CAPV + ReleaseTemplates
PNS=prelude               # vksm control tier + VCD cell
CAPI="capi-controller-manager capi-ipam-in-cluster-controller-manager capi-kubeadm-bootstrap-controller-manager capi-kubeadm-control-plane-controller-manager capv-controller-manager"
# vksm control deploys whose probes we relax if tight (timeout<=2):
PROBE_DEPLOYS="policy-engine-server cluster-service-server cluster-object-service-server dataprotection-server policy-insights-server intent-server account-manager-server authentication-server resource-manager-server api-gateway-server"
# vksm control deploys with an --enable-leader-election ARG (safe to flip at replicas==1):
LE_ARG_DEPLOYS="account-manager-server authentication-server dataprotection-server"
KVFILE=/etc/kubernetes/manifests/kube-vip.yaml
LOGBACK_CM=tenant-manager-logback
CELL_STS=tenant-manager
ACTION="${1:-status}"

# Retry a 'kubectl get ... -o name'-style discovery call up to 3x (5s apart) before giving up.
# INCIDENT (2026-08-11, HOL-2711 10.138.150.5): a transient manager->VCFA connectivity blip
# (kube-vip VIP flakiness on lease loss is the suspected cause -- a known failure mode of this
# exact appliance, ironically the same mechanism harden_vip_apply exists to fix) made every
# kubectl call in this companion return empty on TWO consecutive full runs, even though the
# target objects (kube-vip RT, EnvoyProxy CRs, prelude namespace) had existed for two months.
# Every caller below used `... 2>/dev/null | grep ...` with no exit-code check, so a failed API
# call and a genuinely-absent object were indistinguishable -- both silently read as "not found,
# skip" (steps [4]/[5]/[7]/[8]/[9] all reported zero effect). A THIRD run, moments later with zero
# code changes, applied every lever cleanly -- proof this was transient, not a logic bug. This
# helper retries before concluding "not found," and prints an unmistakable WARNING to stderr
# (distinct from the normal "-- skip" messages) when retries are exhausted, so a log reviewer
# knows to re-run rather than concluding the object doesn't exist.
kc_discover() {  # <kubectl get/list args...> -- e.g. kc_discover get releasetemplate -n "$NS" -o name
  local out rc i errf
  errf="$(mktemp)"
  for i in 1 2 3; do
    out="$($KC "$@" 2>"$errf")"; rc=$?
    if [ $rc -eq 0 ]; then rm -f "$errf"; printf '%s\n' "$out"; return 0; fi
    [ "$i" -lt 3 ] && sleep 5
  done
  echo "  WARNING: 'kubectl $*' failed 3x in a row (API unreachable? VIP flaky?) -- treating as" >&2
  echo "  UNKNOWN, not absent. Any '-- skip'/'not found' message right after this is UNRELIABLE --" >&2
  echo "  re-run this action once connectivity is confirmed stable before trusting a skip here." >&2
  echo "  last error: $(tail -1 "$errf" 2>/dev/null)" >&2
  rm -f "$errf"
  return 2
}

kyverno_rt() { kc_discover get releasetemplate -n "$NS" -o name | grep -iE 'kyverno-' | grep -v policies | head -1; }
c0() { $KC -n "$PNS" get deploy "$1" -o jsonpath='{.spec.template.spec.containers[0].name}' 2>/dev/null; }  # first container name

# ---- reusable lever functions --------------------------------------------------
footprint_apply() {
  echo "== [1] CAPI/CAPV controllers 2->1 =="
  for d in $CAPI; do
    cur=$($KC -n "$NS" get deploy "$d" -o jsonpath='{.spec.replicas}' 2>/dev/null)
    [ -z "$cur" ] && { echo "  $d: NOT FOUND -- skip"; continue; }
    if [ "$cur" != 1 ]; then echo "$d $cur" >> "$BAK/vcfa-footprint-replicas.bak"
      $KC -n "$NS" scale deploy "$d" --replicas=1 >/dev/null 2>&1 && echo "  $d: $cur->1" || echo "  $d: scale FAILED"
    else echo "  $d: already 1"; fi
  done
  echo "== [2] coredns 2->1 =="
  cur=$($KC -n kube-system get deploy coredns -o jsonpath='{.spec.replicas}' 2>/dev/null)
  if [ -n "$cur" ] && [ "$cur" != 1 ]; then echo "coredns $cur" >> "$BAK/vcfa-footprint-replicas.bak"
    $KC -n kube-system scale deploy coredns --replicas=1 >/dev/null 2>&1 && echo "  coredns: $cur->1"
  else echo "  coredns: already ${cur:-?}"; fi
  echo "== [3] disable leader-election on CAPI/CAPV (only where spec.replicas==1) =="
  python3 - "$NS" $CAPI <<'PY'
import subprocess,json,sys
KC=["kubectl","--kubeconfig=/etc/kubernetes/admin.conf"]; ns=sys.argv[1]; deps=sys.argv[2:]
for dep in deps:
    r=subprocess.run(KC+["-n",ns,"get","deploy",dep,"-o","json"],capture_output=True,text=True)
    if r.returncode!=0: print("  %s: NOT FOUND"%dep); continue
    d=json.loads(r.stdout)
    if d["spec"].get("replicas")!=1: print("  %s: replicas!=1 -> SKIP (unsafe)"%dep); continue
    done=False
    for ci,c in enumerate(d["spec"]["template"]["spec"]["containers"]):
        for ai,a in enumerate(c.get("args",[])):
            if a in ("--leader-elect","--leader-elect=true"):
                p=[{"op":"replace","path":"/spec/template/spec/containers/%d/args/%d"%(ci,ai),"value":"--leader-elect=false"}]
                pr=subprocess.run(KC+["-n",ns,"patch","deploy",dep,"--type=json","-p",json.dumps(p)],capture_output=True,text=True)
                print("  %s: %s->false [%s]"%(dep,a,"OK" if pr.returncode==0 else pr.stderr.strip())); done=True
            elif a=="--leader-elect=false": print("  %s: already false"%dep); done=True
    if not done: print("  %s: no --leader-elect arg -- left"%dep)
PY
  echo "== [4] kyverno CLEANUP resyncPeriod -> 1h (ReleaseTemplate) =="
  RT=$(kyverno_rt)
  if [ -z "$RT" ]; then echo "  kyverno RT not found -- skip"; else
    $KC get "$RT" -n "$NS" -o yaml > "$BAK/kyverno-rt-$(date +%s).yaml" 2>/dev/null
    c=$($KC get "$RT" -n "$NS" -o jsonpath='{.spec.helm.values.cleanupController.resyncPeriod}' 2>/dev/null)
    if [ "$c" = "1h" ]; then echo "  cleanup already 1h"; else
      $KC patch "$RT" -n "$NS" --type=merge -p '{"spec":{"helm":{"values":{"cleanupController":{"resyncPeriod":"1h"}}}}}' >/dev/null 2>&1 \
        && echo "  cleanup resync -> 1h (was ${c:-15m})" || echo "  RT patch FAILED"
    fi
  fi
}

probe_relax_apply() {
  echo "== [5] prelude probe relax: TOLERANCE (failureThreshold x period) >= ${PROBE_TOL_TARGET:-90}s, timeout >= 10s =="
  # 2026-07-31, second widening. Two earlier versions of this lever under-fired:
  #   v1 gated on timeoutSeconds<=2  -> skipped account-manager-server (timeout already 10 but
  #      fT5 x period5 = 25 s tolerance); it was liveness-killed 241 times into CrashLoopBackOff,
  #      and because the auth tier is what a UI login/refresh needs, that is what users see as
  #      "the UI will not load" while the gateway itself is healthy.
  #   v2 gated on failureThreshold<8 -> still skipped policy-engine-server (fT=8 but period=5 =
  #      only 40 s) which had 98 restarts.
  # The quantity that actually matters is TOLERANCE = failureThreshold x periodSeconds, i.e. how
  # long a pod may be unresponsive before the kubelet kills it. Under these storms the prober
  # stalls for tens of seconds, so anything under ~90 s gets killed. Discovery-driven over every
  # prelude Deployment, RAISE-ONLY (never weakens a more tolerant site), and it SKIPS
  # operator-generated Deployments (ownerReferences present, e.g. Strimzi/Kafka) because patching
  # those fights their operator -- the RCA S1 anti-pattern.
  local TOL="${PROBE_TOL_TARGET:-90}"
  python3 - "$PNS" "$TOL" "$BAK" <<'PY'
import json,subprocess,sys,math,time
ns,TOL,BAK=sys.argv[1],int(sys.argv[2]),sys.argv[3]
KC=["kubectl","--kubeconfig=/etc/kubernetes/admin.conf","--request-timeout=20s","-n",ns]
# Retry the discovery list 3x (5s apart) -- see kc_discover's incident note in this file's header.
# 'stdout or "{}"' on a failed call used to silently look identical to "zero deployments exist";
# now a persistent failure is a loud, visible warning instead of a quiet no-op.
r=None
for attempt in range(3):
    r=subprocess.run(KC+["get","deploy","-o","json"],capture_output=True,text=True)
    if r.returncode==0: break
    if attempt<2: time.sleep(5)
if r.returncode!=0:
    print("  WARNING: 'kubectl get deploy -n %s' failed 3x (API unreachable?) -- skipping this"%ns)
    print("  lever entirely rather than risk misreading the failure as zero deployments. Re-run.")
    print("  last error: %s"%(r.stderr or "").strip()[:200])
    raise SystemExit
d=json.loads(r.stdout or "{}")
changed=skipped=0
for w in d.get("items",[]):
    n=w["metadata"]["name"]
    if w["metadata"].get("ownerReferences"):
        skipped+=1; continue                      # operator-generated -> never fight the operator
    c=w["spec"]["template"]["spec"]["containers"][0]
    lp=c.get("livenessProbe")
    if not lp: continue
    ft=lp.get("failureThreshold") or 3; pe=lp.get("periodSeconds") or 10; to=lp.get("timeoutSeconds") or 1
    need_ft=max(ft, math.ceil(TOL/max(pe,1)))     # raise-only
    need_to=max(to,10)
    if need_ft==ft and need_to==to: continue
    cpatch={"name":c["name"],"livenessProbe":{"failureThreshold":need_ft,"timeoutSeconds":need_to}}
    # only touch readiness if the container actually HAS one -- patching a null readinessProbe is
    # rejected by the API (hit on policy-sync-service-server, which has liveness but no readiness)
    rp=c.get("readinessProbe")
    if rp:
        cpatch["readinessProbe"]={"failureThreshold":max(rp.get("failureThreshold") or 3,3),
                                  "timeoutSeconds":max(rp.get("timeoutSeconds") or 1,10)}
    patch={"spec":{"template":{"spec":{"containers":[cpatch]}}}}
    subprocess.run(KC+["get","deploy",n,"-o","yaml"],stdout=open("%s/probe-%s-%s.yaml"%(BAK,n,"pre"),"w"),stderr=subprocess.DEVNULL)
    r=subprocess.run(KC+["patch","deploy",n,"--type=strategic","-p",json.dumps(patch)],capture_output=True,text=True)
    ok = r.returncode==0
    print("  %-36s tol %3ds->%3ds  fT %2d->%-2d t %2d->%-2d %s"%(n,ft*pe,need_ft*pe,ft,need_ft,to,need_to,"" if ok else "PATCH FAILED"))
    changed+=1 if ok else 0
print("  relaxed %d workload(s); skipped %d operator-generated"%(changed,skipped))
PY
}

kubevip_guard() {
  echo "== [6] kube-vip static-manifest lease validity guard =="
  [ -f "$KVFILE" ] || { echo "  $KVFILE not found -- skip"; return; }
  ld=$(awk '/name: vip_leaseduration/{getline; gsub(/[^0-9]/,""); print; exit}' "$KVFILE")
  rd=$(awk '/name: vip_renewdeadline/{getline; gsub(/[^0-9]/,""); print; exit}' "$KVFILE")
  rp=$(awk '/name: vip_retryperiod/{getline; gsub(/[^0-9]/,""); print; exit}' "$KVFILE")
  echo "  current lease: leaseduration=$ld renewdeadline=$rd retryperiod=$rp"
  if [ -z "$ld" ] || [ -z "$rd" ] || [ -z "$rp" ]; then echo "  could not parse -- skip"; return; fi
  if [ "$ld" -gt "$rd" ] && [ "$rd" -gt "$rp" ]; then echo "  ordering valid -- no change"; return; fi
  echo "  INVALID ordering (need leaseduration>renewdeadline>retryperiod) -> repairing to 60/40/6"
  cp "$KVFILE" "$BAK/kube-vip.yaml.bak.$(date +%s)"
  sed -E -i '/name: vip_leaseduration/{n;s/value: *"?[0-9]+"?/value: "60"/}; /name: vip_renewdeadline/{n;s/value: *"?[0-9]+"?/value: "40"/}; /name: vip_retryperiod/{n;s/value: *"?[0-9]+"?/value: "6"/}' "$KVFILE"
  echo "  repaired -> 60/40/6 (kubelet restarts the static pod)"
}

harden_vip_apply() {
  echo "== [7] SERVICE kube-vip: preserve VIP on lease-loss + relax lease (via ReleaseTemplate = DURABLE) =="
  # ROOT CAUSE of "Unable to connect" during CPU storms: the service kube-vip (svc_enable=true,
  # holds the :443 LB VIPs .69/.70) ships with vip_preserve_on_leadership_loss=false + a tight
  # 15/10/2 lease -> under storm-induced apiserver slowness it loses its per-service LE lease and
  # DELETES the VIP -> UI unreachable until it restarts+reacquires. The CP kube-vip survives because
  # it has preserve=true. Fix: preserve=true + lease 60/40/6 so the VIP is NEVER withdrawn on loss.
  # MUST be set in the kube-vip ReleaseTemplate (.spec.helm.values.env) -- the vmsp-operator render
  # layer -> HelmRelease -> DS. A direct DS/HelmRelease patch is REVERTED (kube-vip HelmRelease has
  # driftDetection=enabled); the RT is the source of truth -> durable across reconcile + reboot
  # (a chart upgrade would wipe it -> product ask). SINGLE-NODE appliance: preserve=true has no
  # split-brain risk. Do NOT use preserve=true on a MULTI-node VCFA (could dual-advertise the VIP).
  local rt cur
  rt=$(kc_discover get releasetemplate -n "$NS" -o name | grep -i kube-vip | head -1)
  [ -z "$rt" ] && { echo "  no kube-vip ReleaseTemplate in $NS -- skip"; return; }
  cur=$($KC -n "$NS" get "$rt" -o jsonpath='{.spec.helm.values.env.vip_preserve_on_leadership_loss}' 2>/dev/null)
  if [ "$cur" = "true" ]; then echo "  $rt already preserve=true -- no change"; return; fi
  $KC -n "$NS" get "$rt" -o yaml > "$BAK/kube-vip-rt-$(date +%s).yaml" 2>/dev/null && echo "  backed up $rt"
  $KC -n "$NS" patch "$rt" --type=merge -p '{"spec":{"helm":{"values":{"env":{"vip_preserve_on_leadership_loss":"true","vip_leaseduration":"60","vip_renewdeadline":"40","vip_retryperiod":"6"}}}}}' >/dev/null 2>&1 \
    && echo "  patched $rt: preserve=true, lease 60/40/6 (vmsp-operator renders -> DS within ~30s)" || echo "  RT patch FAILED"
}

harden_gateway_apply() {
  echo "== [8] DATA-PLANE Envoy proxy: survive kubelet prober stalls + un-break graceful shutdown =="
  # ROOT CAUSE of the multi-minute ":443 Unable to connect" that the kube-vip fix ([7]) does NOT
  # cover. Chain, measured on 2701 (see ENVOY-GATEWAY-VCFA-FINDINGS.md):
  #   1. The node saturates -> the KUBELET's HTTP prober stalls. The proxy itself is fine: envoy
  #      answered :19003/ready in 0.7 ms (state LIVE, workers_started=1, 5 listeners active,
  #      xDS connected, cgroup nr_throttled=0) at the same instant kubelet marked it NotReady.
  #      (Not CPU-specific: the same probe timeouts hit VSP proxies on nodes at 1% CPU.)
  #   2. readiness failureThreshold=1 -> ONE stalled probe drops the endpoint -> the LB service
  #      (loadBalancerClass=envoy, externalTrafficPolicy=Local) has no backend -> TCP refused.
  #   3. liveness failureThreshold=3 x period 10s -> 30 s of stalls KILLS a healthy envoy. Its
  #      preStop (GET :19002/shutdown/ready) then drains ALL listeners -- including :19003 and
  #      :10443 -> hard :443 outage -- and only returns once the shutdown-manager creates
  #      /tmp/shutdown-ready. On this appliance the shutdown-manager container has NO /tmp mount,
  #      so it logs `error creating shutdown ready file ... no such file or directory`, the hook
  #      blocks for its 600 s readiness timeout, capped by terminationGracePeriodSeconds=360
  #      -> ~5-6 MINUTES of drained-listener outage per prober stall. Measured: 6m45s and 5m08s.
  # WHY THE MOUNT IS MISSING: these proxy Deployments are GENERATED by the Envoy Gateway operator
  # from EnvoyProxy CRs (ownerRef GatewayClass) -- never patch the Deployment, that fights the
  # operator (RCA S1, 150+ restarts). The CR's envoyDeployment.patch is the sanctioned layer, and
  # on this appliance a prior `kubectl patch` of that block replaced the containers array,
  # dropping the shutdown-manager's /tmp emptyDir mount (and both containers' resources).
  # The VSP fleet's Helm-rendered CRs still carry the mount -> we restore vendor intent.
  # DURABILITY: these HelmReleases have driftDetection OFF (verified) and the pre-existing non-Helm
  # patch on this path survived ~3 months + reboots -> the CR is the durable layer. A chart upgrade
  # would wipe it -> product ask. We MERGE into the existing containers (never replace) so any
  # image/resources/volumeMounts already there are preserved.
  # NOT touched on purpose: `resources` (restoring the chart's would re-impose a memory LIMIT the
  # pods currently do not have -> OOM risk) and terminationGracePeriodSeconds (vendor default).
  local crs
  crs=$(kc_discover get envoyproxy -n "$NS" -o name | sed 's|.*/||')
  [ -z "$crs" ] && { echo "  no EnvoyProxy CRs in $NS (not an Envoy Gateway platform?) -- skip"; return; }
  for cr in $crs; do
    $KC -n "$NS" get envoyproxy "$cr" -o yaml > "$BAK/envoyproxy-$cr-$(date +%s).yaml" 2>/dev/null
    python3 - "$cr" "$NS" <<'PY'
import json,sys,subprocess
cr,ns=sys.argv[1],sys.argv[2]
KC=["kubectl","--kubeconfig=/etc/kubernetes/admin.conf","-n",ns]
LIVE_FT,READY_FT,MIN_TO=10,3,5   # liveness 10x10s=100s tolerance; readiness 3x5s=15s; probe timeout >=5s
# Evidence for MIN_TO: on 2701 (timeout already 5s) the proxies had ~92 restarts; on 2711
# (timeout still the 1s chart default) the SAME proxies had 340-364 -- a 1s prober timeout trips
# constantly on a saturated node. 5s is VMware's own value on 2701. We only ever RAISE.
# NOTE: fetch the CR here (not piped in) -- this heredoc IS python's stdin.
r=subprocess.run(KC+["get","envoyproxy",cr,"-o","json"],capture_output=True,text=True)
try: d=json.loads(r.stdout)
except Exception: print("  %s: unreadable (%s) -- skip"%(cr,(r.stderr or "").strip()[:80])); raise SystemExit
ed=(d.get("spec",{}).get("provider",{}).get("kubernetes",{}) or {}).get("envoyDeployment")
if ed is None: print("  %s: no envoyDeployment stanza -- skip"%cr); raise SystemExit
patch=ed.setdefault("patch",{"type":"StrategicMerge","value":{}})
val=patch.setdefault("value",{})
spec=val.setdefault("spec",{}).setdefault("template",{}).setdefault("spec",{})
cs=spec.get("containers")
if not cs:
    # No container overrides yet: create minimal entries. Safe -- StrategicMerge merges by name,
    # so the operator's generated image/resources/mounts are kept for anything we don't set.
    cs=[{"name":"envoy"},{"name":"shutdown-manager"}]
    spec["containers"]=cs
changed=[]
for c in cs:
    nm=c.get("name")
    if nm not in ("envoy","shutdown-manager"): continue
    for pk,ft in (("livenessProbe",LIVE_FT),("readinessProbe",READY_FT),("startupProbe",None)):
        p=c.setdefault(pk,{})
        # raise-only: never weaken a site that is already more tolerant than our target
        if ft is not None and (p.get("failureThreshold") or 0) < ft:
            p["failureThreshold"]=ft; changed.append("%s.%s.failureThreshold=%d"%(nm,pk,ft))
        if (p.get("timeoutSeconds") or 0) < MIN_TO:
            p["timeoutSeconds"]=MIN_TO; changed.append("%s.%s.timeoutSeconds=%d"%(nm,pk,MIN_TO))
    if nm=="shutdown-manager":
        vms=c.setdefault("volumeMounts",[])
        if not any(m.get("mountPath")=="/tmp" for m in vms):
            vms.append({"name":"shutdown-manager","mountPath":"/tmp"})
            changed.append("%s.volumeMounts+=/tmp (shutdown-ready file fix)"%nm)
if not changed:
    print("  %s: already at target (liveness fT>=%d, readiness fT>=%d, timeout>=%ds, /tmp mounted) -- no change"%(cr,LIVE_FT,READY_FT,MIN_TO)); raise SystemExit
body={"spec":{"provider":{"kubernetes":{"envoyDeployment":{"patch":patch}}}}}
r=subprocess.run(KC+["patch","envoyproxy",cr,"--type=merge","-p",json.dumps(body)],capture_output=True,text=True)
if r.returncode==0:
    print("  %s: patched -> %s"%(cr,"; ".join(changed)))
    print("    (the Envoy Gateway operator re-renders the Deployment in ~20 s; the proxy pod rolls once)")
else:
    print("  %s: PATCH FAILED: %s"%(cr,(r.stderr or r.stdout).strip()))
PY
  done
}

harden_gateway_status() {
  echo "== data-plane Envoy proxies (probe tolerance + shutdown-manager /tmp mount) =="
  for d in $($KC -n "$NS" get deploy -l app.kubernetes.io/managed-by=envoy-gateway -o name 2>/dev/null | sed 's|.*/||'); do
    $KC -n "$NS" get deploy "$d" -o json 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin); sp=d['spec']['template']['spec']
out=[]
for c in sp['containers']:
    lp=c.get('livenessProbe',{}); rp=c.get('readinessProbe',{})
    tmp='/tmp' if any(m.get('mountPath')=='/tmp' for m in c.get('volumeMounts',[])) else 'NO-/tmp'
    out.append('%s live=fT%s/p%s/t%s ready=fT%s/p%s/t%s %s'%(c['name'],
      lp.get('failureThreshold'),lp.get('periodSeconds'),lp.get('timeoutSeconds'),
      rp.get('failureThreshold'),rp.get('periodSeconds'),rp.get('timeoutSeconds'),tmp))
print('  %s: %s'%(d['metadata']['name'],' | '.join(out)))
" 2>/dev/null
  done
}

uitier_apply() {
  echo "== [9] USER-FACING UI TIER: lift out of BestEffort (halves the UI stalls; see caveat) =="
  # SCOPE, measured on 2701 (190 samples before / 130 after): this removes the PROVEN mechanism but
  # is NOT a complete fix. avg /automation 0.251s -> 0.110s, worst 20.59s -> 10.66s, slow-rate
  # 1.1% -> 0.8% (1-2 events per window, so the rate difference is not significant).
  # What it definitely fixes: the pod's own inability to complete a TLS handshake --
  # direct-to-pod went from 15.76s (tls 11.93) to 0.77s (tls 0.41) during a stall.
  # What REMAINS: a node-wide transient at extreme load -- in the same post-fix stall envoy's own
  # /ready took 1.76s -- i.e. the residual is the node-wide stall defect (see the next-agent
  # prompt), not the UI tier.
  # ROOT CAUSE of "the auto-a UI takes ~15 seconds" during storms. Measured on 2701 AND 2711:
  #   /automation via the VIP took 11-24 s, and the SAME request sent DIRECTLY to the pod --
  #   bypassing envoy and the Service -- was just as slow (tls=11.9 s on 2701, tls=17.1 s on 2711).
  #   So the gateway is NOT involved: the time is the upstream pod failing to complete a TLS
  #   handshake. Envoy was healthy throughout (/ready in 10-39 ms, circuit breakers closed,
  #   upstream_rq_timeout=0), and other routes on the SAME proxy were fast in the same second
  #   (/api/versions 0.13 s).
  # WHY: every UI micro-frontend ships with `resources: {}` -> QoS BestEffort -> it lands in
  #   kubepods-besteffort.slice, whose cgroup v2 cpu.weight is *1*, versus ~710-753 for the
  #   burstable slice. Under a storm a weight-1 cgroup gets almost no CPU, so crypto that needs
  #   ~10 ms of CPU takes 17 s of wall clock. The container was NOT throttled (no limit,
  #   nr_throttled=0) and was still burning ~0.22 cores -- it is weight starvation, not a cap.
  # FIX: give them a modest CPU/memory REQUEST. That alone moves them into the burstable slice
  #   (weight 1 -> ~750 at the slice level) and improves their eviction ranking. We deliberately
  #   set NO LIMITS, so nothing can be throttled or OOM-killed by this change.
  # DURABLE: every prelude HelmRelease has driftDetection unset (OFF) -> a direct patch sticks
  #   across Flux reconciles and reboots (verified pattern -- same as the vksm probe relax).
  #   A chart upgrade would wipe it -> PRODUCT ASK: ship requests on the UI tier.
  local cpu="${UI_CPU_REQUEST:-200m}" mem="${UI_MEM_REQUEST:-64Mi}" n=0
  for d in $(kc_discover get deploy -n "$PNS" -o name | sed 's|.*/||'); do
    # only the user-facing tier: UI micro-frontends + the static/proxy front ends
    case "$d" in *-ui-app|nginx-httpd-app|proxy-service|health-status-app) ;; *) continue;; esac
    qos=$($KC -n "$PNS" get pods -l app="$d" -o jsonpath='{.items[0].status.qosClass}' 2>/dev/null)
    cur=$($KC -n "$PNS" get deploy "$d" -o jsonpath='{.spec.template.spec.containers[0].resources.requests.cpu}' 2>/dev/null)
    if [ -n "$cur" ]; then echo "  $d: already has cpu request=$cur -- left"; continue; fi
    cn=$($KC -n "$PNS" get deploy "$d" -o jsonpath='{.spec.template.spec.containers[0].name}' 2>/dev/null)
    [ -z "$cn" ] && { echo "  $d: no container -- skip"; continue; }
    $KC -n "$PNS" get deploy "$d" -o yaml > "$BAK/uitier-$d-$(date +%s).yaml" 2>/dev/null
    if $KC -n "$PNS" patch deploy "$d" --type=strategic          -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"$cn\",\"resources\":{\"requests\":{\"cpu\":\"$cpu\",\"memory\":\"$mem\"}}}]}}}}" >/dev/null 2>&1; then
      echo "  $d ($cn): BestEffort -> requests cpu=$cpu mem=$mem (no limits) [rolls once]"; n=$((n+1))
    else echo "  $d: patch FAILED"; fi
  done
  echo "  patched $n workload(s). cgroup slice weights now: besteffort=$(cat /sys/fs/cgroup/kubepods.slice/kubepods-besteffort.slice/cpu.weight 2>/dev/null) burstable=$(cat /sys/fs/cgroup/kubepods.slice/kubepods-burstable.slice/cpu.weight 2>/dev/null)"
}

uitier_status() {
  echo "== user-facing UI tier QoS (BestEffort = cpu.weight 1 = 15s stalls under storm) =="
  $KC -n "$PNS" get pods -o json 2>/dev/null | python3 -c "
import json,sys
d=json.load(sys.stdin)
be=[p['metadata']['name'] for p in d['items'] if p['status'].get('qosClass')=='BestEffort' and p['status'].get('phase')=='Running']
print('  BestEffort pods in this namespace: %d'%len(be))
for n in be[:12]: print('    %s'%n)
" 2>/dev/null
}

# ---- dispatch ------------------------------------------------------------------
case "$ACTION" in
apply)
  footprint_apply; probe_relax_apply; kubevip_guard; harden_vip_apply; harden_gateway_apply; uitier_apply
  echo "== apply done. 'status' to verify; 'disable-le'/'logging' for the opt-in levers. =="
  ;;

harden-gateway)
  harden_gateway_apply; echo; harden_gateway_status
  ;;

harden-uitier)
  uitier_apply; echo; uitier_status
  ;;

disable-le)
  echo "== OPT-IN: disable leader-election on replicas==1 vksm control services (arg-based) =="
  python3 - "$PNS" $LE_ARG_DEPLOYS <<'PY'
import subprocess,json,sys
KC=["kubectl","--kubeconfig=/etc/kubernetes/admin.conf"]; ns=sys.argv[1]; deps=sys.argv[2:]
for dep in deps:
    r=subprocess.run(KC+["-n",ns,"get","deploy",dep,"-o","json"],capture_output=True,text=True)
    if r.returncode!=0: print("  %s: NOT FOUND"%dep); continue
    d=json.loads(r.stdout)
    if d["spec"].get("replicas")!=1: print("  %s: replicas!=1 -> SKIP (unsafe)"%dep); continue
    done=False
    for ci,c in enumerate(d["spec"]["template"]["spec"]["containers"]):
        for ai,a in enumerate(c.get("args",[])):
            if a in ("--enable-leader-election","--enable-leader-election=true"):
                p=[{"op":"replace","path":"/spec/template/spec/containers/%d/args/%d"%(ci,ai),"value":"--enable-leader-election=false"}]
                pr=subprocess.run(KC+["-n",ns,"patch","deploy",dep,"--type=json","-p",json.dumps(p)],capture_output=True,text=True)
                print("  %s: %s->false [%s]"%(dep,a,"OK" if pr.returncode==0 else pr.stderr.strip())); done=True
            elif a=="--enable-leader-election=false": print("  %s: already false"%dep); done=True
    if not done: print("  %s: no --enable-leader-election arg (may be configmap-driven) -- left"%dep)
PY
  echo "  NOTE: configmap-driven LE services (policy-engine/policy-insights/cluster-*) left for a validated follow-up."
  ;;

logging)
  echo "== OPT-IN (DISRUPTIVE, cell restart): tenant-manager logging DEBUG/TRACE -> INFO =="
  $KC -n "$PNS" get cm "$LOGBACK_CM" >/dev/null 2>&1 || { echo "  cm $LOGBACK_CM not found -- skip"; exit 0; }
  $KC -n "$PNS" get cm "$LOGBACK_CM" -o yaml > "$BAK/logback-cm.bak.$(date +%s).yaml"
  before=$($KC -n "$PNS" get cm "$LOGBACK_CM" -o jsonpath='{.data.logback\.xml}' 2>/dev/null | grep -c 'level="DEBUG"\|level="TRACE"')
  python3 - "$PNS" "$LOGBACK_CM" <<'PY'
import subprocess,json,sys
KC=["kubectl","--kubeconfig=/etc/kubernetes/admin.conf"]; ns,cm=sys.argv[1],sys.argv[2]
d=json.loads(subprocess.run(KC+["-n",ns,"get","cm",cm,"-o","json"],capture_output=True,text=True).stdout)
lb=d["data"]["logback.xml"].replace('level="DEBUG"','level="INFO"').replace('level="TRACE"','level="INFO"')
p=[{"op":"replace","path":"/data/logback.xml","value":lb}]
pr=subprocess.run(KC+["-n",ns,"patch","cm",cm,"--type=json","-p",json.dumps(p)],capture_output=True,text=True)
print("  cm patch:", "OK" if pr.returncode==0 else pr.stderr.strip())
PY
  after=$($KC -n "$PNS" get cm "$LOGBACK_CM" -o jsonpath='{.data.logback\.xml}' 2>/dev/null | grep -c 'level="DEBUG"\|level="TRACE"')
  echo "  DEBUG/TRACE loggers: $before -> $after (expect ->0)"
  echo "  restarting cell to apply (no scan=true) ..."
  $KC -n "$PNS" rollout restart statefulset "$CELL_STS" >/dev/null 2>&1 || $KC -n "$PNS" delete pod "${CELL_STS}-0" >/dev/null 2>&1
  $KC -n "$PNS" rollout status statefulset "$CELL_STS" --timeout=300s || echo "  (cell still settling -- check manually)"
  ;;

status)
  echo "== CAPI/CAPV (ns $NS) =="
  for d in $CAPI; do r=$($KC -n "$NS" get deploy "$d" -o jsonpath='{.spec.replicas}' 2>/dev/null)
    le=$($KC -n "$NS" get deploy "$d" -o jsonpath='{.spec.template.spec.containers[*].args}' 2>/dev/null | grep -oE -- '--leader-elect[^ "]*' | head -1)
    echo "  $d: replicas=${r:-NA} ${le:-<no-le>}"; done
  echo "  coredns replicas=$($KC -n kube-system get deploy coredns -o jsonpath='{.spec.replicas}' 2>/dev/null)"
  RT=$(kyverno_rt); [ -n "$RT" ] && echo "  kyverno cleanup.resync=$($KC get "$RT" -n "$NS" -o jsonpath='{.spec.helm.values.cleanupController.resyncPeriod}' 2>/dev/null)"
  echo "== vksm probes (ns $PNS) =="
  for d in $PROBE_DEPLOYS; do cn=$(c0 "$d"); [ -z "$cn" ] && continue
    lt=$($KC -n "$PNS" get deploy "$d" -o jsonpath="{.spec.template.spec.containers[?(@.name==\"$cn\")].livenessProbe.timeoutSeconds}" 2>/dev/null)
    le=$($KC -n "$PNS" get deploy "$d" -o jsonpath='{.spec.template.spec.containers[*].args}' 2>/dev/null | grep -oE -- '--enable-leader-election[^ "]*' | head -1)
    echo "  $d: liveTimeout=${lt:-NA} ${le:-}"; done
  echo "== kube-vip lease (on-disk) =="
  if [ -f "$KVFILE" ]; then
    echo "  leaseduration=$(awk '/vip_leaseduration/{getline;gsub(/[^0-9]/,"");print;exit}' "$KVFILE") renewdeadline=$(awk '/vip_renewdeadline/{getline;gsub(/[^0-9]/,"");print;exit}' "$KVFILE") retryperiod=$(awk '/vip_retryperiod/{getline;gsub(/[^0-9]/,"");print;exit}' "$KVFILE")"
  fi
  echo "== cell logging =="
  echo "  DEBUG/TRACE loggers in $LOGBACK_CM: $($KC -n "$PNS" get cm "$LOGBACK_CM" -o jsonpath='{.data.logback\.xml}' 2>/dev/null | grep -c 'level="DEBUG"\|level="TRACE"')"
  echo "== service kube-vip VIP preservation (RT) =="
  KVRT=$($KC -n "$NS" get releasetemplate -o name 2>/dev/null | grep -i kube-vip | head -1)
  [ -n "$KVRT" ] && echo "  preserve_on_leadership_loss=$($KC -n "$NS" get "$KVRT" -o jsonpath='{.spec.helm.values.env.vip_preserve_on_leadership_loss}' 2>/dev/null)"
  harden_gateway_status
  uitier_status
  ;;

revert)
  echo "== revert CAPI/CAPV LE -> true =="
  python3 - "$NS" $CAPI <<'PY'
import subprocess,json,sys
KC=["kubectl","--kubeconfig=/etc/kubernetes/admin.conf"]; ns=sys.argv[1]
for dep in sys.argv[2:]:
    r=subprocess.run(KC+["-n",ns,"get","deploy",dep,"-o","json"],capture_output=True,text=True)
    if r.returncode!=0: continue
    d=json.loads(r.stdout)
    for ci,c in enumerate(d["spec"]["template"]["spec"]["containers"]):
        for ai,a in enumerate(c.get("args",[])):
            if a=="--leader-elect=false":
                p=[{"op":"replace","path":"/spec/template/spec/containers/%d/args/%d"%(ci,ai),"value":"--leader-elect=true"}]
                subprocess.run(KC+["-n",ns,"patch","deploy",dep,"--type=json","-p",json.dumps(p)]); print("  %s: LE->true"%dep)
PY
  echo "== revert replicas -> 2 (CAPI/CAPV + coredns) =="
  for d in $CAPI; do $KC -n "$NS" scale deploy "$d" --replicas=2 >/dev/null 2>&1 && echo "  $d->2"; done
  $KC -n kube-system scale deploy coredns --replicas=2 >/dev/null 2>&1 && echo "  coredns->2"
  echo "== revert kyverno cleanup -> 15m =="
  RT=$(kyverno_rt); [ -n "$RT" ] && $KC patch "$RT" -n "$NS" --type=merge -p '{"spec":{"helm":{"values":{"cleanupController":{"resyncPeriod":"15m"}}}}}' >/dev/null 2>&1 && echo "  kyverno cleanup->15m"
  echo "== revert vksm probes from saved originals =="
  for d in $PROBE_DEPLOYS; do f="$BAK/probe-$d.orig"; [ -f "$f" ] || continue; cn=$(c0 "$d"); read -r ot of < "$f"
    $KC -n "$PNS" patch deploy "$d" --type=strategic -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"$cn\",\"livenessProbe\":{\"timeoutSeconds\":${ot:-1},\"failureThreshold\":${of:-3}},\"readinessProbe\":{\"timeoutSeconds\":${ot:-1},\"failureThreshold\":${of:-3}}}]}}}}" >/dev/null 2>&1 && echo "  $d: probes restored to ${ot:-1}/${of:-3}"; done
  echo "== revert vksm LE (arg) -> true =="
  python3 - "$PNS" $LE_ARG_DEPLOYS <<'PY'
import subprocess,json,sys
KC=["kubectl","--kubeconfig=/etc/kubernetes/admin.conf"]; ns=sys.argv[1]
for dep in sys.argv[2:]:
    r=subprocess.run(KC+["-n",ns,"get","deploy",dep,"-o","json"],capture_output=True,text=True)
    if r.returncode!=0: continue
    d=json.loads(r.stdout)
    for ci,c in enumerate(d["spec"]["template"]["spec"]["containers"]):
        for ai,a in enumerate(c.get("args",[])):
            if a=="--enable-leader-election=false":
                p=[{"op":"replace","path":"/spec/template/spec/containers/%d/args/%d"%(ci,ai),"value":"--enable-leader-election=true"}]
                subprocess.run(KC+["-n",ns,"patch","deploy",dep,"--type=json","-p",json.dumps(p)]); print("  %s: LE->true"%dep)
PY
  echo "== revert data-plane EnvoyProxy CR patches from newest backups =="
  for cr in $($KC -n "$NS" get envoyproxy -o name 2>/dev/null | sed 's|.*/||'); do
    f=$(ls -1t "$BAK"/envoyproxy-"$cr"-*.yaml 2>/dev/null | head -1)
    [ -z "$f" ] && { echo "  $cr: no backup -- left as-is"; continue; }
    python3 - "$f" "$NS" <<'PY'
import sys,json,subprocess,yaml
d=yaml.safe_load(open(sys.argv[1])); ns=sys.argv[2]
p=d['spec']['provider']['kubernetes']['envoyDeployment'].get('patch')
body={"spec":{"provider":{"kubernetes":{"envoyDeployment":{"patch":p}}}}}
KC=["kubectl","--kubeconfig=/etc/kubernetes/admin.conf","-n",ns]
r=subprocess.run(KC+["patch","envoyproxy",d['metadata']['name'],"--type=merge","-p",json.dumps(body)],capture_output=True,text=True)
print("  %s: %s"%(d['metadata']['name'],(r.stdout or r.stderr).strip()))
PY
  done
  echo "  (kube-vip lease + logging: restore from /root/manifest-bak backups manually if needed.)"
  ;;
*) echo "usage: $0 {apply|harden-gateway|harden-uitier|disable-le|logging|status|revert}"; exit 1;;
esac
__VCFA_MIT_EMBEDDED__
  chmod +x "$VCFA_MIT"
  echo "  (using the companion embedded in this script -- no external file needed)"
}
# ── END EMBEDDED COMPANION ─────────────────────────────────────────────────────

# ── ARG PARSING ────────────────────────────────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --vsp-only)  DO_VCFA=0; shift;;
    --vcfa-only) DO_VSP=0;  shift;;
    --vsp-cp) VSP_CP_IP="$2"; shift 2;;
    --autoa)  AUTOA_IP="$2"; AUTOA_IP_EXPLICIT=1; shift 2;;
    --mgr)    MGR_HOST="$2";  shift 2;;
    --force)  FORCE=1; shift;;
    --status) ACTION="status"; shift;;
    --right-size-requests) ACTION="right-size"; shift;;
    --reduce-ha) ACTION="reduce-ha"; shift;;
    --safe-to-evict) ACTION="safe-evict"; shift;;
    --disable-capi-le) ACTION="disable-capi-le"; shift;;
    --disable-autoscaler|--pin-autoscaler) ACTION="pin"; shift;;
    --enable-autoscaler|--unpin-autoscaler) ACTION="unpin"; shift;;
    --consolidate) ACTION="consolidate"; if [ $# -ge 2 ] && [[ "${2:-}" != --* ]]; then ARG="$2"; shift; fi; shift;;
    --cp-resize) ACTION="cp-resize"; [ $# -ge 2 ] && [[ "${2:-}" != --* ]] && { ARG="$2"; shift; }; shift;;
    --worker-resize) ACTION="worker-resize"; [ $# -ge 2 ] && [[ "${2:-}" != --* ]] && { ARG="$2"; shift; }; shift;;
    --pause) ACTION="pause"; shift;;
    --unpause) ACTION="unpause"; shift;;
    --entropy-fix) ACTION="entropy-fix"; shift;;
    --keepers) ACTION="keepers"; shift;;
    --apply-lease) ACTION="apply-lease"; shift;;
    --revert-lease) ACTION="revert-lease"; shift;;
    --static-pod-hygiene) ACTION="static-pod-hygiene"; shift;;
    --kubelet-reload) ACTION="kubelet-reload"; shift;;
    --etcd-compaction) ACTION="etcd-compaction"; shift;;
    --kube-vip-status) ACTION="kube-vip-status"; shift;;
    --kube-vip-apply) ACTION="kube-vip-apply"; shift;;
    --kube-vip-cluster-patch) ACTION="kube-vip-cluster-patch"; shift;;
    --kcp-patch) ACTION="kcp-patch"; shift;;
    --kyverno-resync-relax) ACTION="kyverno-resync-relax"; shift;;
    --envoy-gateway-fix) ACTION="envoy-gateway-fix"; shift;;
    --vcfa-stabilize) ACTION="vcfa-stabilize"; if [ $# -ge 2 ] && [[ "${2:-}" != --* ]]; then ARG="$2"; shift; fi; shift;;
    --remove) ACTION="remove"; shift;;
    -h|--help) usage; exit 0;;
    *) echo "unknown arg: $1 (see --help)"; exit 1;;
  esac
done

command -v sshpass >/dev/null 2>&1 || { echo "ERROR: sshpass not found locally (needed for the SSH hops). apt/brew/tdnf install sshpass."; exit 2; }
command -v base64  >/dev/null 2>&1 || { echo "ERROR: base64 not found locally."; exit 2; }
[ -z "$MGR_PW" ]  && { echo "ERROR: MGR_PW is not set. No default is baked in -- export MGR_PW='...' (manager/console SSH password for this pod) and re-run."; exit 2; }
[ -z "$NODE_PW" ] && { echo "ERROR: NODE_PW is not set. No default is baked in -- export NODE_PW='...' (node SSH password for this pod) and re-run."; exit 2; }

# ── temp workspace + manager password file (keep MGR_PW out of local argv/ps) ──
TMP="$(mktemp -d)"
umask 077
MGR_PW_FILE="$TMP/.mgrpw"; printf '%s' "$MGR_PW" > "$MGR_PW_FILE"; chmod 600 "$MGR_PW_FILE"

# EXIT trap: cleanup + ALWAYS-attempt-unpause safety (unless CP verifiably down).
cleanup() {
  local rc=$?
  if [ "$PAUSED_BY_US" = 1 ]; then
    if [ "$CP_VERIFIED_DOWN" = 1 ]; then
      echo ""
      echo "*** WARNING: Cluster ${CLS_NAME} left PAUSED ON PURPOSE -- the control plane is verifiably down/unhealthy."
      echo "*** Do NOT unpause until the CP is restored. Investigate, then: $0 --unpause"
    else
      echo ""
      echo "EXIT-trap safety: ensuring Cluster ${CLS_NAME} is UNPAUSED (webhook-resilient)..."
      patch_paused false || echo "  WARNING: unpause did not confirm -- run '$0 --unpause' manually."
    fi
  fi
  rm -rf "$TMP" 2>/dev/null || true
  exit "$rc"
}
trap cleanup EXIT

# ── SSH options: force ONE password attempt, no key attempts (avoids the
#    'Too many authentication failures' lockout entirely), no host-key prompts ─
SSHO_BASE="-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=20 -o PubkeyAuthentication=no -o PreferredAuthentications=password -o NumberOfPasswordPrompts=1"
MGR_SSHO="$SSHO_BASE -o ServerAliveInterval=30 -o ServerAliveCountMax=8"
NODE_SSHO="$SSHO_BASE -o ServerAliveInterval=30 -o ServerAliveCountMax=8"

# ── TRANSPORT ──────────────────────────────────────────────────────────────────
# Run a script (from stdin) as holuser on the manager. base64 keeps quoting sane.
mgr_run() {
  local b; b=$(base64 | tr -d '\n')
  sshpass -f "$MGR_PW_FILE" ssh $MGR_SSHO -p "$MGR_PORT" "${MGR_USER}@${MGR_HOST}" "echo $b | base64 -d | bash" 2>&1 \
    | grep -vaE 'Warning:|Permanently added|Welcome to Photon|Last login' || true
}
# Run govc (staged on the manager). Args = the govc command line.
govc() { printf '. %q && %q %s\n' "$GOVC_ENV" "$GOVC" "$*" | mgr_run; }
# govc preflight: true ONLY if the env file + binary exist on the manager AND
# `govc about` authenticates to vCenter. The node-RESIZE steps depend on this;
# if it fails, they are SKIPPED (never paused) -- a missing govc must not be able
# to pause a cluster and then fail (that was the .21 incident). Consolidation and
# every kubectl-based fix do NOT need govc, so they run regardless.
GOVC_OK_CACHE=""
GOVC_STAGE_ATTEMPTED=""
# Nested vCenter creds (NOT physical/outer infra -- these share the pod's standard lab password,
# confirmed on 10.138.150.5: administrator@vsphere.local / the same VMware123!VMware123!-style
# password used everywhere else in the pod). Override via env if a pod ever differs.
GOVC_ADMIN_USER="${GOVC_ADMIN_USER:-administrator@vsphere.local}"
GOVC_ADMIN_PW="${GOVC_ADMIN_PW:-$MGR_PW}"
govc_ok() {
  [ -n "$GOVC_OK_CACHE" ] && { [ "$GOVC_OK_CACHE" = yes ]; return; }
  local probe
  probe="$(printf '{ [ -f %q ] && [ -x %q ]; } && . %q && %q about 2>/dev/null | grep -m1 "^FullName" || echo GOVC_UNAVAILABLE\n' "$GOVC_ENV" "$GOVC" "$GOVC_ENV" "$GOVC" | mgr_run 2>/dev/null)"
  if echo "$probe" | grep -q '^FullName'; then GOVC_OK_CACHE=yes; return 0; fi
  if [ -z "$GOVC_STAGE_ATTEMPTED" ]; then
    GOVC_STAGE_ATTEMPTED=1
    stage_govc
    probe="$(printf '{ [ -f %q ] && [ -x %q ]; } && . %q && %q about 2>/dev/null | grep -m1 "^FullName" || echo GOVC_UNAVAILABLE\n' "$GOVC_ENV" "$GOVC" "$GOVC_ENV" "$GOVC" | mgr_run 2>/dev/null)"
    if echo "$probe" | grep -q '^FullName'; then GOVC_OK_CACHE=yes; return 0; fi
  fi
  GOVC_OK_CACHE=no; return 1
}
# Self-heal a missing/unusable govc: download the binary (no creds needed) and, if the vCenter
# server address is discoverable from the live VSphereCluster CR, write the env file using the
# pod's standard nested-vCenter creds above. Never touches physical/outer vCenter -- if no nested
# vCenter address can be discovered, this cleanly gives up rather than guessing one.
stage_govc() {
  echo "  govc not usable on the manager -- attempting to auto-stage it (binary + nested-vCenter env file)..."
  local bprobe
  bprobe="$(printf '[ -x %q ] && echo BINARY_OK || echo BINARY_MISSING\n' "$GOVC" | mgr_run 2>/dev/null)"
  if ! echo "$bprobe" | grep -q BINARY_OK; then
    echo "    downloading govc from the official govmomi GitHub release..."
    printf 'set -e\ncd /tmp\nurl=$(curl -s https://api.github.com/repos/vmware/govmomi/releases/latest | grep -o "\\"browser_download_url\\": *\\"[^\\"]*govc_Linux_x86_64.tar.gz\\"" | sed -E "s/.*\\"(https:[^\\"]+)\\"/\\1/")\n[ -z "$url" ] && { echo STAGE_FAIL_NO_ASSET; exit 1; }\ncurl -sL "$url" -o govc.tar.gz\ntar -xzf govc.tar.gz govc\nchmod +x govc\nmv -f govc %q\nrm -f govc.tar.gz\necho STAGE_BINARY_OK\n' "$GOVC" | mgr_run 2>&1 | sed 's/^/    /'
  fi
  local eprobe
  eprobe="$(printf '[ -f %q ] && echo ENV_OK || echo ENV_MISSING\n' "$GOVC_ENV" | mgr_run 2>/dev/null)"
  if echo "$eprobe" | grep -q ENV_OK; then return 0; fi
  local vc=""
  if [ "$DO_VSP" = 1 ]; then
    vc="$(kc get vsphereclusters -A -o jsonpath='{.items[0].spec.server}' 2>/dev/null | tr -d '[:space:]')"
  fi
  if [ -z "$vc" ]; then
    echo "    could not discover the nested vCenter server address (no VSphereCluster reachable via kubectl) -- giving up. Set GOVC_ENV to a pre-staged file, or GOVC_URL/GOVC_USERNAME/GOVC_PASSWORD to write one by hand."
    return 1
  fi
  echo "    writing ${GOVC_ENV} for nested vCenter ${vc} (user ${GOVC_ADMIN_USER})..."
  printf 'umask 077\ncat > %q <<GOVCENVEOF\nexport GOVC_URL='"'"'https://%s/sdk'"'"'\nexport GOVC_USERNAME='"'"'%s'"'"'\nexport GOVC_PASSWORD='"'"'%s'"'"'\nexport GOVC_INSECURE=1\nGOVCENVEOF\nchmod 600 %q\necho STAGE_ENV_OK\n' "$GOVC_ENV" "$vc" "$GOVC_ADMIN_USER" "$GOVC_ADMIN_PW" "$GOVC_ENV" | mgr_run 2>&1 | sed 's/^/    /'
}

# Run a script (from stdin) as ROOT on node <ip>, proxied local->manager->node.
# The node password is written to a transient mode-600 file ON THE MANAGER and
# used via `sshpass -f` (never `-p`), so it is not exposed in any long-lived
# argv; sudo -S reads it from stdin on the node. Quote-safe via base64 nesting.
# INCIDENT (2026-08-12, 10.138.150.3, under heavy load): the manager->node SSH hop itself failed
# transiently ("kex_exchange_identification: read: Connection reset by peer"), not any command on
# the node -- and this had NO retry, unlike kc_discover's kubectl-level retries. Two whole steps
# (VSP envoy-gateway-fix, VCFA Family A keepers) were silently dropped; both had happened to
# already complete moments earlier so no harm resulted that time, but nothing guaranteed that
# timing. mgr_run's own `|| true` means it ALWAYS returns rc=0 regardless of what happened inside
# (existing, intentional behavior -- every caller in this script already parses output text rather
# than checking exit codes, e.g. reachable()'s `grep -q ok`), so a transient transport failure can
# only be detected by matching the actual SSH error text, not an exit code. Every step this project
# runs is idempotent by design (checks current state, patches only if needed), so retrying is
# always safe -- worst case a re-run of an already-applied step reports "no change".
node_run() {
  local ip="$1"
  local inner ib attempt out
  inner="$(base64 | tr -d '\n')"   # the root script for the node
  ib="$(printf 'echo %s | base64 -d > /tmp/_rl.$$ && printf "%%s\n" %q | sudo -S -p "" bash /tmp/_rl.$$; rc=$?; rm -f /tmp/_rl.$$; exit $rc' "$inner" "$NODE_PW" | base64 | tr -d '\n')"
  for attempt in 1 2 3; do
    out="$(mgr_run <<MGR
umask 077
NPF="\$(mktemp)"
printf '%s' $(printf '%q' "$NODE_PW") > "\$NPF"
sshpass -f "\$NPF" ssh $NODE_SSHO ${NODE_USER}@${ip} 'echo $ib | base64 -d | bash'
rc=\$?
rm -f "\$NPF"
exit \$rc
MGR
)"
    if [ "$attempt" -lt 3 ] && echo "$out" | grep -qE 'kex_exchange_identification|Connection reset by peer|Connection timed out|ssh_exchange_identification|Connection refused|Operation timed out|Broken pipe'; then
      echo "  (node_run to $ip: transient SSH-transport failure, retry $attempt/3 in 5s...)" >&2
      sleep 5
      continue
    fi
    printf '%s\n' "$out"
    return 0
  done
}

# Deliver a local script file to node <ip> and run it with optional args.
# The file bytes are shipped verbatim inside a quoted heredoc (no re-quoting),
# so battle-tested remote scripts run exactly as authored.
node_run_file() {  # <ip> <localfile> [args...]
  local ip="$1" f="$2"; shift 2
  local args="$*"
  {
    echo "cat > /tmp/_rlf.\$\$ <<'__RLF_EOF__'"
    cat "$f"
    echo "__RLF_EOF__"
    echo "bash /tmp/_rlf.\$\$ $args; rc=\$?; rm -f /tmp/_rlf.\$\$; exit \$rc"
  } | node_run "$ip"
}
# Same, but feed the string "CONFIRM" on the script's stdin (for the one
# interactive, CONFIRM-gated action, so its `read` is satisfied over the hop).
node_run_file_confirm() {  # <ip> <localfile>
  local ip="$1" f="$2"
  {
    echo "cat > /tmp/_rlf.\$\$ <<'__RLF_EOF__'"
    cat "$f"
    echo "__RLF_EOF__"
    echo "echo CONFIRM | bash /tmp/_rlf.\$\$; rc=\$?; rm -f /tmp/_rlf.\$\$; exit \$rc"
  } | node_run "$ip"
}
# Ship a set of local files ($TMP basenames in $TARFILES) to /tmp on the node
# via a tar+base64 blob, then run the install block piped on stdin.
push_files_and_run() {  # <ip>  (reads install block from stdin; uses $TARFILES)
  local ip="$1"
  local b64; b64="$(tar czf - -C "$TMP" $TARFILES 2>/dev/null | base64 | tr -d '\n')"
  {
    echo "base64 -d > /tmp/_rlk.tgz <<'__RLK64__'"
    echo "$b64"
    echo "__RLK64__"
    echo "tar xzf /tmp/_rlk.tgz -C /tmp && rm -f /tmp/_rlk.tgz"
    cat
  } | node_run "$ip"
}
# True if node <ip> is reachable as root through the manager hop. Retries a few
# times (spaced) so a transient SSH/network blip doesn't false-negative a healthy
# node -- e.g. a post-remediation re-verify. One password prompt per attempt,
# 6s apart, so this never approaches the 'Too many authentication failures' lockout.
reachable() {
  local i
  for i in 1 2 3 4 5 6; do
    printf 'echo ok\n' | node_run "$1" 2>/dev/null | grep -q ok && return 0
    [ "$i" -lt 6 ] && sleep 10
  done
  return 1
}

# kubectl on the VSP CP node (root, admin.conf), %q-quoted args, prints output.
kc() {
  local a cmd="kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s"
  for a in "$@"; do cmd+=" $(printf '%q' "$a")"; done
  printf '%s\n' "$cmd" | node_run "$VSP_CP_IP"
}

# AUTOA_IP defaults to the kube-vip SERVICE VIP (see CONFIG), not a real node IP. For a
# genuinely single-node VCFA appliance (the normal HOL topology) there is no failover benefit to
# routing the *management* SSH hop through that VIP -- there's no second node to fail over to --
# and VIP flakiness on lease-loss is a documented failure mode of this exact appliance (see
# harden_vip_apply in the embedded companion). Prefer a real node IP when one answers, matching
# the candidate-probe pattern vcfa-verify-stability.sh already uses for the same reason. One quick
# single attempt per candidate (not the full 6x10s reachable() retry loop -- this is a cheap
# probe, not a health gate); keeps the configured AUTOA_IP (the VIP by default) if none answer,
# e.g. a genuinely multi-node VCFA or a pod with a different IP plan. Never overrides an explicit
# AUTOA_IP env var or --autoa flag (AUTOA_IP_EXPLICIT).
resolve_autoa_ip() {
  [ "$AUTOA_IP_EXPLICIT" = 1 ] && return
  local candidate
  for candidate in 10.1.1.71 10.1.1.72 10.1.1.73 10.1.1.74; do
    if printf 'echo ok\n' | node_run "$candidate" 2>/dev/null | grep -q ok; then
      echo "  (auto-a: found real node $candidate reachable -- using it instead of VIP $AUTOA_IP for the mgmt hop)"
      AUTOA_IP="$candidate"
      return
    fi
  done
  echo "  (auto-a: no real-node candidate answered -- keeping VIP $AUTOA_IP; may be genuinely multi-node or a different IP plan)"
}

# ── PREFLIGHT: connectivity, role discovery, health gate (BOTH clusters) ───────
reach_manager() {
  echo "== Preflight: manager/jump host ${MGR_HOST}:${MGR_PORT} (${MGR_USER}) =="
  echo ok | mgr_run 2>/dev/null | grep -q ok \
    || { echo "ERROR: cannot reach manager ${MGR_HOST}:${MGR_PORT} as ${MGR_USER} -- check MGR_HOST/MGR_PW. (One password attempt was made; no retry, to avoid auth lockout.)"; exit 2; }
  echo "  manager reachable."
  [ "$DO_VCFA" = 1 ] && resolve_autoa_ip
}

# Reusable role+health gate. Verifies the node at <ip> IS a healthy control
# plane: hostname resolves, etcd + kube-apiserver STATIC PODS present, the
# LOCAL apiserver https://127.0.0.1:6443 responds (independent of any VIP),
# and the node reports Ready via that local apiserver. Prints a per-line
# report; returns 0 only if all gates pass. Never assumes; never mutates.
node_preflight() {  # <ip> <role-label-for-messages>
  local ip="$1" label="$2" out etcd api lapi ready attempt
  echo "== Preflight: ${label} node ${ip} =="
  reachable "$ip" || { echo "  UNREACHABLE: cannot SSH ${NODE_USER}@${ip} via the manager (wrong NODE_PW, or manager cannot route to it). SKIPPING all actions on this node."; return 1; }
  # Retry the health probe: right after a defrag/etcd restart the probe can
  # transiently return empty/incomplete over the hop; that must not false-fail a
  # healthy node. Up to 3 attempts, 8s apart. Any healthy reading wins immediately.
  for attempt in 1 2 3 4 5 6; do
  out="$(node_run "$ip" <<'PF' 2>/dev/null || true
H="$(hostname)"
echo "HOST=$H"
[ -f /etc/kubernetes/manifests/etcd.yaml ]           && echo "ETCD_STATIC=yes"      || echo "ETCD_STATIC=no"
[ -f /etc/kubernetes/manifests/kube-apiserver.yaml ] && echo "APISERVER_STATIC=yes" || echo "APISERVER_STATIC=no"
if command -v curl >/dev/null 2>&1; then
  curl -sk --max-time 10 https://127.0.0.1:6443/readyz >/dev/null 2>&1 && echo "LOCAL_APISERVER=up" || echo "LOCAL_APISERVER=down"
else
  KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=https://127.0.0.1:6443 --insecure-skip-tls-verify=true --request-timeout=10s"
  $KC get --raw='/readyz' >/dev/null 2>&1 && echo "LOCAL_APISERVER=up" || echo "LOCAL_APISERVER=down"
fi
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=https://127.0.0.1:6443 --insecure-skip-tls-verify=true --request-timeout=10s"
rd="$($KC get node "$H" -o jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status}{end}' 2>/dev/null)"
echo "NODE_READY=${rd:-unknown}"
echo "NODE_CPU=$($KC get node "$H" -o jsonpath='{.status.capacity.cpu}' 2>/dev/null)"
PF
)"
  etcd="$(echo "$out"  | sed -n 's/^ETCD_STATIC=//p')"
  api="$(echo "$out"   | sed -n 's/^APISERVER_STATIC=//p')"
  lapi="$(echo "$out"  | sed -n 's/^LOCAL_APISERVER=//p')"
  ready="$(echo "$out" | sed -n 's/^NODE_READY=//p')"
  if [ "$etcd" = yes ] && [ "$api" = yes ] && [ "$lapi" = up ] && [ "$ready" = True ]; then
    echo "$out" | sed 's/^/  /'
    echo "  PREFLIGHT OK: healthy control-plane (etcd+apiserver static pods present, local apiserver up, node Ready)."
    return 0
  fi
  if [ "$attempt" -lt 6 ]; then
    echo "  (health probe incomplete: etcd=${etcd:-?} api=${api:-?} localApiserver=${lapi:-?} ready=${ready:-?} -- likely a transient/self-inflicted etcd-defrag or cascade blip; retry ${attempt}/6 in 12s)"
    sleep 12; continue
  fi
  echo "$out" | sed 's/^/  /'
  echo "  PREFLIGHT FAILED: not a healthy control-plane (etcd=${etcd:-empty} apiserver=${api:-empty} localApiserver=${lapi:-empty} ready=${ready:-empty}) after 6 attempts -- SKIPPING mutating actions on this node."
  return 1
  done
}

# INCIDENT (2026-08-11, HOL-2711 10.138.150.5): patching the kube-vip/kyverno/envoy-gateway
# ReleaseTemplates makes vmsp-operator do a full re-list of every ReleaseTemplate, which is
# documented (see the embedded companion's comments) to "commonly trigger ONE instance of the
# platform-wide leader-election restart-cascade... within the next minute or two." Confirmed live
# that this can be bigger and slower than that phrasing suggests: a real, user-visible outage on
# the actual login page ("upstream connect error... connection timeout"), load average spiking to
# ~4x the node's vCPU count, and a kube-vip pod delete/recreate loop -- lasting a few minutes, not
# a few seconds, before self-resolving. node_preflight only checks the control plane (etcd/
# apiserver/node Ready), which can report healthy while this cascade is still mid-flight -- it
# does NOT check whether the thing a user actually hits (the login page) is reachable. This gate
# polls for REAL readiness (the login page actually answering -- what a user actually hits) before
# letting the run claim to be complete, instead of handing back "done" during the cascade.
#
# INCIDENT #2 (2026-08-12, different lab/SKU, 10.138.150.3): this gate originally also required
# zero CrashLoopBackOff pods, same as verify_vsp_ready() did before ITS incident -- and hit the
# exact same false-negative: an unrelated, pre-existing chronic crash loop (ndc-controller-manager,
# 46h old, 84 restarts) kept the gate from ever passing even though the login page was answering
# 200 in ~50ms the entire time. Fixed the same way: gate ONLY on the login page, report crashloop
# count for visibility without blocking on it.
#
# INCIDENT #3 (2026-08-12, same lab, later the same day): the login check was changed to resolve
# against $ip (the management-hop target, a real node IP) instead of the hardcoded VIP -- looked
# like a good fix (no more hardcoded address) but broke the actual check: curl got an immediate
# "Connection refused" on :443 via the node's own IP while kube-vip was holding the VIP just fine
# on .70. The service is only reachable via the VIP; the node's own address is not a valid
# substitute even though it reaches the same VM. Now uses $AUTOA_VIP (a separate global, always
# the VIP) for the login check specifically, while $ip (the management-hop target) is still used
# for the node_run/SSH hop itself -- these are genuinely different addresses for different jobs.
verify_vcfa_ready() {  # <ip>
  local ip="$1" i out crashloop code secs
  echo "== Verifying VCFA is actually ready (login page reachable) -- up to 5 min =="
  for i in $(seq 1 20); do
    out="$(node_run "$ip" <<VREADY 2>/dev/null
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=10s"
echo "CRASHLOOP=\$(\$KC get pods -A --no-headers 2>/dev/null | grep -ci crashloop)"
echo "LOGIN=\$(curl -k -s -o /dev/null -w '%{http_code} %{time_total}' --connect-timeout 8 --max-time 15 --resolve auto-a.site-a.vcf.lab:443:${AUTOA_VIP} https://auto-a.site-a.vcf.lab/login/ 2>/dev/null)"
VREADY
)"
    crashloop="$(echo "$out" | sed -n 's/^CRASHLOOP=//p')"
    read -r code secs <<<"$(echo "$out" | sed -n 's/^LOGIN=//p')"
    if [ "$code" = "200" ]; then
      echo "  READY: login page http=$code (${secs:-?}s). (confirmed on attempt $i/20)"
      if [ "${crashloop:-0}" != "0" ]; then
        echo "  NOTE: ${crashloop} CrashLoopBackOff pod(s) present. Not gating on this -- if it was"
        echo "  already crash-looping before this run, that is a separate, pre-existing issue worth"
        echo "  investigating on its own, not something this remediation caused or fixes."
      fi
      return 0
    fi
    echo "  not ready yet (attempt $i/20): crashloop=${crashloop:-?} login_http=${code:-?} login_time=${secs:-?}s -- waiting 15s"
    sleep 15
  done
  echo "  WARNING: VCFA did not confirm actually-ready within 5 minutes of the post-remediation"
  echo "  cascade. The documented cascade usually settles in 1-3 min; 5 min without recovering is"
  echo "  longer than observed historically. Check manually (load average, CrashLoopBackOff pods,"
  echo "  the login page) before treating this remediation pass as done -- do NOT assume it's fine."
  return 1
}

# INCIDENT (2026-08-11, HOL-2711 10.138.150.5, same session as verify_vcfa_ready's incident):
# minutes after the same RT-patch cascade, `kubectl get nodes`/`get pods` on the VSP CP node
# returned an instant (~2ms) "Forbidden" -- not a timeout, not RBAC misconfiguration (verified
# live: authorization-mode=Node,RBAC correct, kubeadm:cluster-admins ClusterRoleBinding correct
# and matching the client cert's O= field exactly, no RBAC-deny lines in the apiserver log). etcd
# itself was healthy throughout (actively serving reads/writes in its own log). It self-resolved
# within a few minutes with zero intervention -- but a SINGLE successful node_preflight call can
# land in a good window and miss this kind of brief, intermittent flakiness entirely, which is
# exactly what happened here (node_preflight had already reported this node healthy earlier in
# the same run). Require several CONSECUTIVE clean reads, not one, before calling it ready.
verify_vsp_ready() {  # <ip>
  local ip="$1" i out crashloop nodecount ok_streak=0
  echo "== Verifying VSP fleet CP is consistently responsive (not just a single lucky read) -- up to 3 min =="
  for i in $(seq 1 18); do
    out="$(node_run "$ip" <<'VSPREADY' 2>/dev/null
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=10s"
echo "CRASHLOOP=$($KC get pods -A --no-headers 2>/dev/null | grep -ci crashloop)"
echo "NODECOUNT=$($KC get nodes --no-headers 2>/dev/null | grep -c Ready)"
VSPREADY
)"
    crashloop="$(echo "$out" | sed -n 's/^CRASHLOOP=//p')"
    nodecount="$(echo "$out" | sed -n 's/^NODECOUNT=//p')"
    # Gate ONLY on kubectl itself being consistently, correctly responsive -- that is what the
    # incident above was actually about. Do NOT gate on cluster-wide CrashLoopBackOff count: this
    # pod carries an unrelated, pre-existing, hours-long chronic kyverno-background-controller
    # crash loop (87+ restarts and counting, confirmed live) that has nothing to do with this
    # remediation and would make a "zero CrashLoopBackOff anywhere" bar permanently unsatisfiable
    # -- confirmed live: this exact bug made the first version of this gate warn on every one of
    # 18 attempts despite kubectl answering correctly and consistently the whole time. Report the
    # count for visibility only.
    if [ -n "$nodecount" ] && [ "$nodecount" -gt 0 ] 2>/dev/null; then
      ok_streak=$((ok_streak+1))
      echo "  clean read $ok_streak/3 (attempt $i/18): nodes-ready=$nodecount (crashloop pods elsewhere: ${crashloop:-?}, not gated -- see note below)"
      if [ "$ok_streak" -ge 3 ]; then
        echo "  READY: 3 consecutive clean kubectl reads."
        if [ "${crashloop:-0}" != "0" ]; then
          echo "  NOTE: ${crashloop} CrashLoopBackOff pod(s) present. Not gating on this -- if it was"
          echo "  already crash-looping before this run, that is a separate, pre-existing issue worth"
          echo "  investigating on its own, not something this remediation caused or fixes."
        fi
        return 0
      fi
    else
      echo "  not ready (attempt $i/18): kubectl get nodes returned '${nodecount:-empty/error}' -- resetting streak"
      ok_streak=0
    fi
    sleep 10
  done
  echo "  WARNING: VSP fleet CP did not confirm 3 CONSECUTIVE clean reads within 3 minutes. This"
  echo "  can happen after the same RT-patch cascade verify_vcfa_ready guards against -- but"
  echo "  intermittent Forbidden/timeout responses this far out are longer than observed"
  echo "  historically. Check manually before treating this remediation pass as done."
  return 1
}

# Discover VSP cluster objects + template-declared sizes (run on the VSP CP).
# Everything the resize/footprint steps need is discovered, not hardcoded.
discover_vsp() {
  echo "== Discovering VSP cluster objects/templates (via ${VSP_CP_IP}) =="
  local out
  out="$(node_run "$VSP_CP_IP" <<'DISC' 2>/dev/null || true
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s"
cn="$($KC get cluster -A -o json 2>/dev/null | jq -r '.items[]|select(.spec.controlPlaneRef.kind=="KubeadmControlPlane")|"\(.metadata.namespace) \(.metadata.name)"' | head -1)"
ns="$(echo "$cn" | awk '{print $1}')"; name="$(echo "$cn" | awk '{print $2}')"
echo "CLS_NS=$ns"; echo "CLS_NAME=$name"
echo "PAUSED=$($KC get cluster "$name" -n "$ns" -o jsonpath='{.spec.paused}' 2>/dev/null)"
cpn="$($KC get nodes -l node-role.kubernetes.io/control-plane -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
echo "CP_NODE=$cpn"
echo "CP_CAP_CPU=$($KC get node "$cpn" -o jsonpath='{.status.capacity.cpu}' 2>/dev/null)"
echo "CP_REAL_IP=$($KC get node "$cpn" -o jsonpath='{.status.addresses[?(@.type=="InternalIP")].address}' 2>/dev/null)"
kcp="$($KC get kubeadmcontrolplane -n "$ns" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
cpt="$($KC get kubeadmcontrolplane "$kcp" -n "$ns" -o jsonpath='{.spec.machineTemplate.spec.infrastructureRef.name}{.spec.machineTemplate.infrastructureRef.name}' 2>/dev/null)"
echo "CP_TMPL_CPU=$($KC get vspheremachinetemplate "$cpt" -n "$ns" -o jsonpath='{.spec.template.spec.numCPUs}' 2>/dev/null)"
echo "CP_TMPL_MEM=$($KC get vspheremachinetemplate "$cpt" -n "$ns" -o jsonpath='{.spec.template.spec.memoryMiB}' 2>/dev/null)"
md="$($KC get machinedeployment -n "$ns" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null)"
echo "MD=$md"
echo "MD_REPLICAS=$($KC get machinedeployment "$md" -n "$ns" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
wt="$($KC get machinedeployment "$md" -n "$ns" -o jsonpath='{.spec.template.spec.infrastructureRef.name}' 2>/dev/null)"
echo "W_TMPL_CPU=$($KC get vspheremachinetemplate "$wt" -n "$ns" -o jsonpath='{.spec.template.spec.numCPUs}' 2>/dev/null)"
echo "W_TMPL_MEM=$($KC get vspheremachinetemplate "$wt" -n "$ns" -o jsonpath='{.spec.template.spec.memoryMiB}' 2>/dev/null)"
asrt="$($KC get releasetemplate -n "$ns" -o name 2>/dev/null | grep -i cluster-autoscaler | head -1 | cut -d/ -f2)"
echo "AS_RT=$asrt"
[ -n "$asrt" ] && echo "AS_RT_REPLICAS=$($KC get releasetemplate "$asrt" -n "$ns" -o jsonpath='{.spec.helm.values.replicaCount}' 2>/dev/null)"
DISC
)"
  local v
  v="$(echo "$out" | sed -n 's/^CLS_NS=//p')";        [ -n "$v" ] && CLS_NS="$v"
  v="$(echo "$out" | sed -n 's/^CLS_NAME=//p')";      [ -n "$v" ] && CLS_NAME="$v"
  PAUSED_STATE="$(echo "$out" | sed -n 's/^PAUSED=//p')"
  CP_NODE="$(echo "$out"     | sed -n 's/^CP_NODE=//p')"
  CP_CAP_CPU="$(echo "$out"  | sed -n 's/^CP_CAP_CPU=//p')"
  CP_REAL_IP="$(echo "$out"  | sed -n 's/^CP_REAL_IP=//p')"
  CP_TMPL_CPU="$(echo "$out" | sed -n 's/^CP_TMPL_CPU=//p')"
  CP_TMPL_MEM="$(echo "$out" | sed -n 's/^CP_TMPL_MEM=//p')"
  MD="$(echo "$out"          | sed -n 's/^MD=//p')"
  MD_REPLICAS="$(echo "$out" | sed -n 's/^MD_REPLICAS=//p')"
  W_TMPL_CPU="$(echo "$out"  | sed -n 's/^W_TMPL_CPU=//p')"
  W_TMPL_MEM="$(echo "$out"  | sed -n 's/^W_TMPL_MEM=//p')"
  AS_RT="$(echo "$out"       | sed -n 's/^AS_RT=//p')"
  AS_RT_REPLICAS="$(echo "$out" | sed -n 's/^AS_RT_REPLICAS=//p')"
  echo "  Cluster=${CLS_NAME} ns=${CLS_NS} paused=${PAUSED_STATE:-false}"
  echo "  CP node=${CP_NODE} realIP=${CP_REAL_IP} liveCPU=${CP_CAP_CPU}  template=${CP_TMPL_CPU}vCPU/${CP_TMPL_MEM}MiB"
  echo "  MD=${MD} replicas=${MD_REPLICAS}  worker template=${W_TMPL_CPU}vCPU/${W_TMPL_MEM}MiB  autoscaler RT=${AS_RT} replicaCount=${AS_RT_REPLICAS:-unset}"
  if [ -z "$CP_NODE" ] || [ -z "$CP_REAL_IP" ] || [ -z "$CP_TMPL_CPU" ]; then
    echo "  WARNING: incomplete VSP discovery -- resize steps will be skipped (cannot safely target what wasn't discovered)."
    return 1
  fi
  return 0
}

# ── webhook-resilient pause/unpause (retry through capi-webhook timeouts) ──────
patch_paused() {  # <true|false>
  local want="$1" i got
  [ -n "$CLS_NAME" ] || { echo "  (no cluster discovered -- cannot patch paused)"; return 1; }
  for i in 1 2 3 4 5 6 7 8; do
    kc patch cluster "$CLS_NAME" -n "$CLS_NS" --type=merge -p "{\"spec\":{\"paused\":${want}}}" >/dev/null 2>&1 || true
    got="$(kc get cluster "$CLS_NAME" -n "$CLS_NS" -o jsonpath='{.spec.paused}' 2>/dev/null || true)"
    if { [ "$want" = true ] && [ "$got" = true ]; } || { [ "$want" = false ] && { [ "$got" = false ] || [ -z "$got" ]; }; }; then
      echo "  cluster ${CLS_NAME}: paused=${got:-false} (confirmed, attempt $i)"; return 0
    fi
    echo "  paused=${want} patch attempt $i did not take yet (webhook 'context deadline exceeded'?) -- retrying in 5s..."
    sleep 5
  done
  echo "  ERROR: paused=${want} did not confirm after 8 attempts."
  return 1
}
do_pause()   { echo "Pausing cluster ${CLS_NAME} (webhook-resilient)..."; if patch_paused true;  then PAUSED_BY_US=1; return 0; fi; return 1; }
do_unpause() { echo "Unpausing cluster ${CLS_NAME} (webhook-resilient)..."; if patch_paused false; then PAUSED_BY_US=0; return 0; fi; return 1; }

# ── Build Family B/C remote scripts into $TMP (VERBATIM from hol-remediate.sh --
#    only their DELIVERY changes; their internals are byte-for-byte identical). ─
build_remote_scripts() {
cat > "$TMP/remediate-lease.sh" <<REMOTE
#!/bin/bash
set -euo pipefail
MDIR=/etc/kubernetes/manifests
SPBAK=/root/manifest-bak      # backups MUST live OUTSIDE MDIR -- see sweep_static_pod_dir
FAMB_ACTION="\$1"
LEASE_DURATION="${LEASE_DURATION}"
RENEW_DEADLINE="${RENEW_DEADLINE}"
RETRY_PERIOD="${RETRY_PERIOD}"
ETCD_CPU_REQUEST="${ETCD_CPU_REQUEST}"

# ── static-pod directory hygiene ─────────────────────────────────────────────────────────────
# kubelet's file source parses EVERY file in staticPodPath, whatever the extension. A backup left
# BESIDE a manifest (e.g. kube-apiserver.yaml.bak.1778525225) therefore becomes a SECOND static pod
# declaring the SAME pod name -- and it can win, silently pinning the node to the old spec forever.
#
# That is not hypothetical: on 2701 seven such files had accumulated, and EVERY static-pod edit since
# 2026-05-11 was inert -- the apiserver stuck at 250m while its manifest said 1000m, and KCM running
# 1 of the 4 --leader-elect* flags its manifest declared. It was misdiagnosed for weeks as "kubelet
# is serving a stale spec, a restart will fix it". A restart does NOT fix it (verified), and neither
# does moving the manifest out and back -- the shadow keeps redefining the pod.
#
# Three cp sites in THIS script were creating them (\`cp "\$file" "\$file.bak.\$(date +%s)"\`). Hence
# both halves of the fix: never back up inside MDIR, and sweep MDIR before touching anything.
safe_manifest_backup() {   # <manifest-path> -- write the backup OUTSIDE the static-pod dir
  mkdir -p "\$SPBAK"
  cp "\$1" "\$SPBAK/\$(basename "\$1").bak.\$(date +%s)"
}
is_live_manifest() {   # 0 = a real live manifest; 1 = litter kubelet must never parse
  case "\$1" in
    *.bak*|*~|*.orig|*.save|*.rpmsave|*.rpmnew|*.dpkg-*|*.tmp|*.swp|*.old|*.disabled) return 1;;
  esac
  case "\$1" in
    *.yaml|*.yml) return 0;;
    *) return 1;;
  esac
}
# Count/list shadows using the SAME predicate the sweep uses, so --status can never report "clean"
# while a plain run would still move something. Loop rather than \`ls glob | wc -l\`: under
# \`set -o pipefail\` a non-matching glob makes ls exit non-zero, which fails the whole pipeline and
# aborts the script -- that bug ate the first --status run during development.
count_shadows() {
  local f n=0
  for f in "\$MDIR"/*; do
    [ -f "\$f" ] || continue
    is_live_manifest "\$(basename "\$f")" || n=\$((n+1))
  done
  echo "\$n"
}
list_shadows() {
  local f
  for f in "\$MDIR"/*; do
    [ -f "\$f" ] || continue
    is_live_manifest "\$(basename "\$f")" || echo "      \$(basename "\$f")"
  done
}
sweep_static_pod_dir() {
  # Deliberately pattern-based (litter suffixes + "not .yaml"), NOT an allowlist of expected pod
  # names: this script also runs against the VSP fleet CP, which legitimately carries a different
  # set of static pods. An allowlist would silently delete a real one.
  local f b n=0
  mkdir -p "\$SPBAK"
  for f in "\$MDIR"/*; do
    [ -e "\$f" ] || continue
    [ -f "\$f" ] || continue
    b="\$(basename "\$f")"
    if is_live_manifest "\$b"; then continue; fi
    if mv -f "\$f" "\$SPBAK/" 2>/dev/null; then
      echo "  SWEPT shadow static-pod file out of \$MDIR: \$b  -> \$SPBAK/"
      n=\$((n+1))
    else
      echo "  *** WARNING: could not move shadow file \$b out of \$MDIR -- it will keep shadowing."
    fi
  done
  if [ "\$n" -gt 0 ]; then
    echo "  \$n shadow file(s) moved. kubelet was parsing these as DUPLICATE static pods."
    echo "  Removing a shadow lets the REAL manifest take effect, so the running spec may change on"
    echo "  kubelet's next sync -- that is the intended outcome, not a side effect."
  else
    echo "  staticPodPath is clean (live manifests only) -- nothing to sweep."
  fi
}

# ── kube-apiserver CPU request (a SHARE lever, not a limit) ──────────────────────────────────
# Measured on 2701 2026-07-31: during a storm the apiserver burns 8x its healthy CPU (apiRUN 1613 vs
# 205 ms/s) yet waits 21618 ms/s -- wait:run 13.4:1, total demand ~23 of 24 cores. Nothing on the node
# has a cpu.max limit, so cpu.weight ALONE apportions CPU, and it binds only under contention, i.e.
# exactly during a storm. The apiserver's pod slice carried weight 10 of burstable's 858 across 136
# pod slices -- outside the top ten, while etcd (excluded by measurement as a bottleneck) carried 98.
# Since apiserver recovery is what ends each storm, raising its share is the targeted lever.
#
# This is a REQUEST, not a limit: cpu.max stays "max" and nr_throttled stays 0, so it can never
# throttle or evict. It only raises the proportional floor.
apiserver_cpu_live() {   # echo "<shares> <podSliceWeight>" from the RUNTIME
  # Deliberately NOT \`kubectl get pod\`: when a shadow file wedges the mirror pod, the API object
  # freezes and reports the OLD request forever. On 2701 it read 250m for 2.5 months. That false
  # reading cost three experiments. Read the runtime and the cgroup instead -- they cannot lie.
  local cid shares pid cg w i
  # Retry: crictl talks to containerd over a gRPC socket with a short deadline and intermittently
  # returns "DeadlineExceeded" when the node is loaded -- which is EXACTLY when this reading matters.
  # A single attempt reports a spurious "none" and looks like the pod is gone. Observed on 2701.
  cid=""
  for i in 1 2 3 4 5; do
    cid="\$(crictl ps --name '^kube-apiserver\$' -q 2>/dev/null | head -1 || true)"
    [ -n "\$cid" ] && break
    sleep 2
  done
  shares="none"
  if [ -n "\$cid" ]; then
    for i in 1 2 3; do
      shares="\$(crictl inspect "\$cid" 2>/dev/null | python3 -c "
import json,sys
try: print(json.load(sys.stdin)['info']['runtimeSpec']['linux']['resources']['cpu']['shares'])
except Exception: print('?')" 2>/dev/null || echo '?')"
      [ "\$shares" != "?" ] && [ -n "\$shares" ] && break
      sleep 2
    done
  fi
  pid="\$(pgrep -f 'kube-apiserver --advertise' 2>/dev/null | head -1 || true)"
  [ -z "\$pid" ] && pid="\$(pgrep -o -f kube-apiserver 2>/dev/null || true)"
  if [ -n "\$pid" ]; then
    cg="\$(awk -F: '{print \$3}' /proc/\$pid/cgroup 2>/dev/null | tail -1)"
    w="\$(cat "/sys/fs/cgroup\$(dirname "\$cg")/cpu.weight" 2>/dev/null || echo '?')"
  else w="none"; fi
  echo "\${shares:-?} \${w:-?}"
}
patch_apiserver_cpu_request() {
  local file="\$MDIR/kube-apiserver.yaml"
  [ -f "\$file" ] || { echo "  kube-apiserver: manifest not found at \$file -- skipping"; return; }
  local K="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
  # Size it from DISCOVERY, never a hardcoded number. On 2701, 20665m of 23870m was already
  # requested, and the binding constraint was not the weight but the headroom the largest single pod
  # needs to surge/reschedule (vcfapostgres-0 at 2200m). A fixed value would break a busier pod.
  local plan target cur total alloc surge
  plan="\$(\$K get nodes,pods -A -o json 2>/dev/null | python3 -c "
import json,sys
def milli(v):
    if not v: return 0
    v=str(v)
    return int(v[:-1]) if v.endswith('m') else int(float(v)*1000)
try: d=json.load(sys.stdin)
except Exception: print('ERR could not read cluster state'); raise SystemExit
alloc=0; total=0; cur=0; surge=0
for it in d.get('items',[]):
    k=it.get('kind')
    if k=='Node':
        alloc=max(alloc,milli(it.get('status',{}).get('allocatable',{}).get('cpu')))
    elif k=='Pod':
        if it.get('status',{}).get('phase') not in ('Running','Pending'): continue
        s=sum(milli((c.get('resources',{}).get('requests') or {}).get('cpu')) for c in it['spec']['containers'])
        total+=s
        surge=max(surge,s)
        if it['metadata']['name'].startswith('kube-apiserver-'): cur=s
if not alloc or not cur:
    print('ERR discovery incomplete alloc=%d cur=%d'%(alloc,cur)); raise SystemExit
# Largest candidate that still leaves headroom for the biggest single-pod surge on THIS node.
pick=cur
for cand in (4000,3000,2000,1500,1000,750,500):
    if cand<=cur: continue
    if (alloc-(total-cur+cand))>=surge: pick=cand; break
print('OK %d %d %d %d %d'%(pick,cur,total,alloc,surge))
" 2>/dev/null || echo 'ERR python failed')"
  set -- \$plan
  if [ "\${1:-ERR}" != "OK" ]; then
    echo "  kube-apiserver cpu request: discovery failed (\$plan) -- refusing to guess, skipping"
    return
  fi
  target="\$2"; cur="\$3"; total="\$4"; alloc="\$5"; surge="\$6"
  echo "  kube-apiserver cpu request: discovered total=\${total}m of alloc=\${alloc}m, largest single-pod"
  echo "    request (surge floor)=\${surge}m, apiserver currently \${cur}m"
  if [ "\$target" -le "\$cur" ]; then
    echo "    -> keeping \${cur}m: no larger candidate leaves headroom >= \${surge}m. Raise-only, so no change."
    echo "    live now: shares/podWeight = \$(apiserver_cpu_live)"
    return
  fi
  echo "    -> raising to \${target}m (leaves \$((alloc-(total-cur+target)))m headroom, >= \${surge}m surge floor)"
  local curstr
  curstr="\$(python3 -c "
import yaml,sys
d=yaml.safe_load(open('\$file'))
print((d['spec']['containers'][0].get('resources',{}).get('requests') or {}).get('cpu',''))" 2>/dev/null || true)"
  if [ -z "\$curstr" ]; then
    echo "    no resources.requests.cpu in the manifest -- refusing to guess where to insert, skipping"
    return
  fi
  safe_manifest_backup "\$file"
  python3 - "\$file" "\${target}m" <<'PY'
import re,sys
p,val=sys.argv[1],sys.argv[2]
lines=open(p).read().split('\n')
ri=None
for i,l in enumerate(lines):
    if re.match(r'^\s*resources:\s*\$',l): ri=i; break
if ri is None: sys.exit('no resources: block')
base=len(lines[ri])-len(lines[ri].lstrip()); tgt=None
for i in range(ri+1,min(ri+8,len(lines))):
    ind=len(lines[i])-len(lines[i].lstrip())
    if lines[i].strip() and ind<=base: break
    if re.match(r'^\s*cpu:\s*\S+\s*\$',lines[i]): tgt=i; break
if tgt is None: sys.exit('no cpu: under resources')
lines[tgt]=re.sub(r'(cpu:\s*)\S+',lambda m:m.group(1)+val,lines[tgt])
open(p,'w').write('\n'.join(lines))
PY
  echo "    manifest updated \${curstr} -> \${target}m (backup in \$SPBAK)"
  echo "    live now: shares/podWeight = \$(apiserver_cpu_live)"
  echo "    NOTE: this takes effect only when kubelet re-instantiates the static pod. It is INERT"
  echo "    until then -- deliberately, so a plain run never restarts your control plane. Use"
  echo "    --kubelet-reload when you are ready (expect a ~60-90s apiserver blip)."
}

do_kubelet_reload() {
  echo "  kubelet reload: restarting kubelet so static-pod manifest changes take effect."
  local stray
  stray="\$(count_shadows)"
  if [ "\${stray:-0}" -gt 0 ]; then
    echo "  *** REFUSING: \$stray shadow file(s) still in \$MDIR. Restarting now would just re-apply"
    echo "  *** the shadow spec. Run --static-pod-hygiene first."
    return 1
  fi
  echo "  before: apiserver shares/podWeight = \$(apiserver_cpu_live)"
  systemctl restart kubelet
  local i
  for i in \$(seq 1 100); do
    [ "\$(curl -s -k -o /dev/null -w '%{http_code}' --max-time 5 https://127.0.0.1:6443/healthz 2>/dev/null)" = "200" ] && break
    sleep 3
  done
  sleep 30
  echo "  after : apiserver shares/podWeight = \$(apiserver_cpu_live)  (1000m -> 1024/39, 250m -> 256/10)"
  echo "  healthz=\$(curl -s -k -o /dev/null -w '%{http_code}' --max-time 5 https://127.0.0.1:6443/healthz 2>/dev/null)"
}

patch_leader_elect() {
  local file="\$MDIR/\$1.yaml"
  [ -f "\$file" ] || { echo "  \$1: manifest not found at \$file -- skipping"; return; }
  local cur_lease cur_renew cur_retry
  cur_lease="\$(grep -oE -- '--leader-elect-lease-duration=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  cur_renew="\$(grep -oE -- '--leader-elect-renew-deadline=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  cur_retry="\$(grep -oE -- '--leader-elect-retry-period=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  if [ "\$cur_lease" = "\${LEASE_DURATION}" ] && [ "\$cur_renew" = "\${RENEW_DEADLINE}" ] && [ "\$cur_retry" = "\${RETRY_PERIOD}" ]; then
    echo "  \$1: already at target (\${LEASE_DURATION}/\${RENEW_DEADLINE}/\${RETRY_PERIOD}) -- no change"
    verify_static_pod_flags "\$1"
    return
  fi
  if ! grep -q -- '--leader-elect=true' "\$file"; then
    echo "  \$1: '--leader-elect=true' not found in \$file -- refusing to guess where to insert, skipping"
    return
  fi
  safe_manifest_backup "\$file"
  sed -i -E "/--leader-elect-lease-duration=/d; /--leader-elect-renew-deadline=/d; /--leader-elect-retry-period=/d" "\$file"
  sed -i "/--leader-elect=true/a\\\\    - --leader-elect-lease-duration=\${LEASE_DURATION}\\\\n    - --leader-elect-renew-deadline=\${RENEW_DEADLINE}\\\\n    - --leader-elect-retry-period=\${RETRY_PERIOD}" "\$file"
  if [ -n "\$cur_lease\$cur_renew\$cur_retry" ]; then
    echo "  \$1: drift corrected \${cur_lease:-unset}/\${cur_renew:-unset}/\${cur_retry:-unset} -> \${LEASE_DURATION}/\${RENEW_DEADLINE}/\${RETRY_PERIOD} (backup written). kubelet will restart the static pod automatically."
    sleep 20; verify_static_pod_flags "\$1"
  else
    echo "  \$1: patched (backup written alongside manifest). kubelet will restart the static pod automatically."
    sleep 20; verify_static_pod_flags "\$1"
  fi
}

# Verify the RUNNING container actually picked the flags up. A static-pod manifest edit is NOT
# self-verifying: on 2701 (2026-07-31) the file carried all four --leader-elect-* flags, parsed as
# valid YAML, and the container restarted 41 minutes later still on the OLD spec -- only
# --leader-elect=true reached /proc/<pid>/cmdline, and the Lease object still read 15s instead of
# 60s. The kubelet was holding a stale in-memory spec; \`touch\`ing the manifest did not force a
# re-read (verified over 200s). Family B therefore reported success while being completely inert.
# Never claim a static-pod patch worked without checking the process.
verify_static_pod_flags() {   # <component>
  local comp="\$1" cid runargs
  command -v crictl >/dev/null 2>&1 || return 0
  cid="\$(crictl ps --name "\$comp" -q 2>/dev/null | head -1)"
  [ -z "\$cid" ] && { echo "      (verify: \$comp not running right now -- re-check later)"; return 0; }
  runargs="\$(crictl inspect "\$cid" 2>/dev/null | tr ',' '\n' | grep -c 'leader-elect-lease-duration' || true)"
  if [ "\${runargs:-0}" -ge 1 ]; then
    echo "      verified: the running \$comp has the lease flags"
  else
    echo "      *** WARNING: \$comp is RUNNING WITHOUT the lease flags despite the manifest having them."
    echo "      *** This fix is INERT on this node until that is resolved."
    # The previous revision of this message said the fix "will activate on the next reboot" and that
    # "systemctl restart kubelet" applies it. BOTH claims were WRONG, and they sent the investigation
    # down a dead end for weeks. Measured on 2701 2026-07-31: a kubelet restart changed nothing (same
    # pod UID, same spec), and neither did moving the manifest out of staticPodPath and back. A reboot
    # would not have helped either. The real cause is a SHADOW file in staticPodPath -- kubelet parses
    # every file there, so a *.bak beside the manifest becomes a duplicate static pod that wins.
    local shadows
    shadows="\$(count_shadows)"
    if [ "\${shadows:-0}" -gt 0 ]; then
      echo "      *** CAUSE FOUND: \$shadows shadow file(s) in \$MDIR are being parsed as duplicate"
      echo "      *** static pods and are overriding the real manifest. Re-run with --static-pod-hygiene"
      echo "      *** (or a plain no-flag run) to sweep them, then --kubelet-reload to reconcile."
    else
      echo "      *** staticPodPath looks clean, so a shadow file is NOT the cause here. kubelet has"
      echo "      *** not reconciled yet -- give it a sync interval, then --kubelet-reload if needed."
      echo "      *** Do NOT assume a reboot fixes this; that assumption was disproven on 2701."
    fi
  fi
}

# Backups now live in \$SPBAK. Still look in \$MDIR too: nodes remediated by an older revision of
# this script have legacy backups there (and those are exactly the shadow files the sweep relocates,
# so after a sweep they are found in \$SPBAK anyway). Newest wins across both locations.
newest_backup() {   # <basename-of-manifest, e.g. etcd.yaml>
  ls -t "\$SPBAK/\$1".bak.* "\$MDIR/\$1".bak.* 2>/dev/null | head -1 || true
}
revert_leader_elect() {
  local name="\$1" file="\$MDIR/\$1.yaml"
  local latest
  latest="\$(newest_backup "\$1.yaml")"
  if [ -z "\$latest" ]; then
    echo "  \$name: no backup found in \$SPBAK or \$MDIR -- nothing to revert"
    return
  fi
  cp "\$latest" "\$file"
  echo "  \$name: reverted from \$latest"
}

revert_etcd() {
  local latest
  latest="\$(newest_backup etcd.yaml)"
  if [ -z "\$latest" ]; then
    echo "  etcd: no backup found in \$SPBAK or \$MDIR -- nothing to revert"
    return
  fi
  cp "\$latest" "\$MDIR/etcd.yaml"
  echo "  etcd: reverted from \$latest"
}

status_leader_elect() {
  local file="\$MDIR/\$1.yaml"
  [ -f "\$file" ] || { echo "  \$1: manifest not found"; return; }
  local cur_lease cur_renew cur_retry
  cur_lease="\$(grep -oE -- '--leader-elect-lease-duration=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  cur_renew="\$(grep -oE -- '--leader-elect-renew-deadline=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  cur_retry="\$(grep -oE -- '--leader-elect-retry-period=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  if [ -z "\$cur_lease\$cur_renew\$cur_retry" ]; then
    echo "  \$1: NOT patched -- running on client-go defaults (leaseDuration=15s renewDeadline=10s retryPeriod=2s)"
  elif [ "\$cur_lease" = "\${LEASE_DURATION}" ] && [ "\$cur_renew" = "\${RENEW_DEADLINE}" ] && [ "\$cur_retry" = "\${RETRY_PERIOD}" ]; then
    echo "  \$1: PATCHED, at target -- leaseDuration=\$cur_lease renewDeadline=\$cur_renew retryPeriod=\$cur_retry"
  else
    echo "  \$1: PATCHED but DRIFTED from target -- current leaseDuration=\${cur_lease:-unset} renewDeadline=\${cur_renew:-unset} retryPeriod=\${cur_retry:-unset} (target \${LEASE_DURATION}/\${RENEW_DEADLINE}/\${RETRY_PERIOD} -- re-run --apply-lease to fix)"
  fi
}

etcd_cpu_status() {
  local file="\$MDIR/etcd.yaml"
  [ -f "\$file" ] || { echo "  etcd: manifest not found"; return; }
  local cur
  cur="\$(grep -A1 'requests:' "\$file" | grep 'cpu:' | awk '{print \$2}' || true)"
  if [ -z "\$cur" ]; then
    echo "  etcd cpu request: NOT SET (no resources.requests.cpu at all) -- needs \${ETCD_CPU_REQUEST}"
  else
    echo "  etcd cpu request: \$cur (target \${ETCD_CPU_REQUEST})"
  fi
}

etcd_cpu_apply() {
  local file="\$MDIR/etcd.yaml"
  [ -f "\$file" ] || { echo "  etcd: manifest not found -- skipping"; return; }
  local cur
  cur="\$(grep -A1 'requests:' "\$file" | grep 'cpu:' | awk '{print \$2}' || true)"
  if [ "\$cur" = "\${ETCD_CPU_REQUEST}" ]; then
    echo "  etcd cpu request: already \${ETCD_CPU_REQUEST} -- no change"
    return
  fi
  if [ -z "\$cur" ]; then
    echo "  etcd: no resources.requests.cpu found -- refusing to guess where to insert, skipping (needs manual review)"
    return
  fi
  safe_manifest_backup "\$file"
  sed -i "0,/cpu: \$cur/s//cpu: \${ETCD_CPU_REQUEST}/" "\$file"
  echo "  etcd cpu request: \$cur -> \${ETCD_CPU_REQUEST} (backup written). kubelet will restart the static pod automatically."
  echo "  Verifying live cpu_shares (expect roughly millicores * 1.024 -- e.g. 2500m -> 2560)..."
  local i pod_id cid shares
  for i in 1 2 3 4 5 6 7 8; do
    sleep 3
    pod_id="\$(crictl pods --name '^etcd-'"\$(hostname)"'\$' -q 2>/dev/null | head -1 || true)"
    cid="\$(crictl ps --pod "\$pod_id" -q 2>/dev/null | head -1 || true)"
    [ -n "\$cid" ] && break
  done
  if [ -n "\$cid" ]; then
    shares="\$(crictl inspect "\$cid" 2>/dev/null | grep cpu_shares | grep -oE '[0-9]+' | head -1 || true)"
    echo "  Verified live: crictl inspect on the new etcd container shows cpu_shares=\${shares:-unknown}."
  else
    echo "  WARNING: could not find the restarted etcd container via crictl to verify cpu_shares -- the file is"
    echo "  updated and kubelet should still pick it up on its own. Re-run --status to check."
  fi
}

etcd_compaction_status() {
  local file="\$MDIR/etcd.yaml"
  [ -f "\$file" ] || { echo "  etcd: manifest not found"; return; }
  local cur_mode cur_retention
  cur_mode="\$(grep -oE -- '--auto-compaction-mode=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  cur_retention="\$(grep -oE -- '--auto-compaction-retention=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  if [ -z "\$cur_mode" ]; then
    echo "  etcd auto-compaction: NOT enabled (no --auto-compaction-mode flag)"
  elif [ "\$cur_mode" = "periodic" ] && [ "\$cur_retention" = "1h" ]; then
    echo "  etcd auto-compaction: ENABLED, at target -- mode=\$cur_mode retention=\$cur_retention"
  else
    echo "  etcd auto-compaction: ENABLED but DRIFTED -- mode=\${cur_mode:-unset} retention=\${cur_retention:-unset} (target periodic/1h -- re-run --etcd-compaction to fix)"
  fi
}

etcd_compaction_apply() {
  local file="\$MDIR/etcd.yaml"
  [ -f "\$file" ] || { echo "  etcd: manifest not found -- skipping"; return; }
  local cur_mode cur_retention
  cur_mode="\$(grep -oE -- '--auto-compaction-mode=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  cur_retention="\$(grep -oE -- '--auto-compaction-retention=[^[:space:]]+' "\$file" | head -1 | cut -d= -f2 || true)"
  if [ "\$cur_mode" = "periodic" ] && [ "\$cur_retention" = "1h" ]; then
    echo "  etcd auto-compaction: already at target (periodic, 1h retention) -- no manifest change"
  else
    if ! grep -q -- '--election-timeout=' "\$file"; then
      echo "  etcd: anchor flag '--election-timeout=' not found -- refusing to guess insertion point, skipping"
      return
    fi
    safe_manifest_backup "\$file"
    sed -i -E "/--auto-compaction-mode=/d; /--auto-compaction-retention=/d" "\$file"
    sed -i "/--election-timeout=/a\\\\    - --auto-compaction-mode=periodic\\\\n    - --auto-compaction-retention=1h" "\$file"
    if [ -n "\$cur_mode\$cur_retention" ]; then
      echo "  etcd auto-compaction: drift corrected \${cur_mode:-unset}/\${cur_retention:-unset} -> periodic/1h (backup written). kubelet will restart etcd."
    else
      echo "  etcd auto-compaction: enabled (periodic, 1h retention; backup written). kubelet will restart etcd."
    fi
    echo "  NOTE: etcd will restart to pick this up -- for a single-member etcd (the normal HOL topology) this is a"
    echo "  brief etcd (and therefore apiserver) unavailability window, not an instant no-downtime change like the"
    echo "  lease-tuning/CPU-request edits above."
  fi
  echo "  Running one-time defrag (safe, does not require the flag above; independent action)..."
  if ETCDCTL_API=3 etcdctl --cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/server.crt --key=/etc/kubernetes/pki/etcd/server.key --endpoints=https://127.0.0.1:2379 endpoint status --write-out=table >/tmp/etcd-status-before.txt 2>&1; then
    cat /tmp/etcd-status-before.txt
  fi
  ETCDCTL_API=3 etcdctl --cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/server.crt --key=/etc/kubernetes/pki/etcd/server.key --endpoints=https://127.0.0.1:2379 defrag && echo "  defrag: OK" || echo "  defrag: FAILED (see output above)"
}

kube_vip_drift_status() {
  echo "kube-vip:"
  local file="\$MDIR/kube-vip.yaml"
  if [ ! -f "\$file" ]; then echo "  manifest not found"; return; fi
  echo "  on-disk file:"
  grep -A1 'vip_lease\|vip_renewdeadline\|vip_retryperiod\|vip_preserve_on_leadership_loss' "\$file" | grep -E 'name|value' | paste - - | sed 's/^/    /'
  local cluster_ns_name
  cluster_ns_name="\$(kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s get cluster -A -o json 2>/dev/null | jq -r '.items[] | select(.spec.controlPlaneRef.kind=="KubeadmControlPlane") | "\(.metadata.namespace) \(.metadata.name)"' | head -1 || true)"
  if [ -z "\$cluster_ns_name" ]; then
    echo "  Cluster object: could not discover (jq missing, apiserver unreachable/timed out within 20s, or no Cluster with a KubeadmControlPlane controlPlaneRef visible from this node's own apiserver)"
    return
  fi
  local ns name
  ns="\$(echo "\$cluster_ns_name" | awk '{print \$1}')"
  name="\$(echo "\$cluster_ns_name" | awk '{print \$2}')"
  echo "  Cluster object: \$name -n \$ns"
  local cur
  cur="\$(kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s get cluster "\$name" -n "\$ns" -o json 2>/dev/null | jq -r '.spec.topology.variables[]? | select(.name=="kubeVipPodManifest") | .value' 2>/dev/null || true)"
  if [ -z "\$cur" ]; then
    echo "  Cluster variable kubeVipPodManifest: not found on this Cluster object (different ClusterClass/mechanism)."
    echo "  Doesn't affect --kube-vip-apply (file-only, doesn't need this variable) -- but --kube-vip-cluster-patch"
    echo "  definitely cannot be used here without checking further; it would have nothing to patch."
    return
  fi
  echo "  Cluster variable kubeVipPodManifest (only rendered onto a NEW machine at creation time -- NOT"
  echo "  what's running now; the file above is what kubelet actually reads and runs):"
  echo "\$cur" | grep -A1 'vip_lease\|vip_renewdeadline\|vip_retryperiod\|vip_preserve_on_leadership_loss' | grep -E 'name|value' | paste - - | sed 's/^/    /'
  local file_renew cluster_renew
  file_renew="\$(grep -A1 'vip_renewdeadline' "\$file" | grep value | sed -E 's/.*value: *"?([0-9]+)"?.*/\\1/' || true)"
  cluster_renew="\$(echo "\$cur" | grep -A1 'vip_renewdeadline' | grep value | sed -E 's/.*value: *"?([0-9]+)"?.*/\\1/' || true)"
  if [ "\$file_renew" != "\$cluster_renew" ]; then
    echo "  ** NOTE: on-disk file (renewdeadline=\$file_renew) differs from the Cluster variable (renewdeadline=\$cluster_renew)."
    echo "     This is expected after --kube-vip-apply (it intentionally only edits the file, never the Cluster object)."
    echo "     Not a problem for the CURRENTLY RUNNING kube-vip -- it reads the file, not the Cluster variable. It only"
    echo "     matters if this control-plane machine is ever replaced in the future: the replacement would render from"
    echo "     the Cluster variable's value above, silently reverting to it. Re-run --kube-vip-apply after any such"
    echo "     replacement, or use --kube-vip-cluster-patch instead for a fix that survives it (disruptive, see header). **"
  else
    echo "  File and Cluster variable agree (renewdeadline=\$cluster_renew) -- no drift, and safe even across a future replacement."
  fi
}

case "\$FAMB_ACTION" in
  status)
    echo "staticPodPath hygiene (shadow files silently override every manifest edit):"
    _stray="\$(count_shadows)"
    if [ "\${_stray:-0}" -gt 0 ]; then
      echo "  *** \$_stray SHADOW file(s) in \$MDIR -- static-pod edits on this node are INERT:"
      list_shadows
      echo "  *** Fix with --static-pod-hygiene (or a plain no-flag run), then --kubelet-reload."
    else
      echo "  clean -- live manifests only (\$(ls -1 \$MDIR | tr '\n' ' '))"
    fi
    echo "  kube-apiserver live shares/podWeight: \$(apiserver_cpu_live)  (250m=256/10, 1000m=1024/39)"
    echo ""
    echo "kube-controller-manager / kube-scheduler:"
    status_leader_elect kube-controller-manager
    status_leader_elect kube-scheduler
    echo ""
    echo "etcd:"
    etcd_cpu_status
    etcd_compaction_status
    echo ""
    kube_vip_drift_status
    ;;
  static-pod-hygiene)
    echo "staticPodPath sweep (shadow .bak files are parsed as duplicate static pods):"
    sweep_static_pod_dir
    echo ""
    echo "kube-apiserver CPU request (share lever, discovery-sized):"
    patch_apiserver_cpu_request
    ;;
  kubelet-reload)
    do_kubelet_reload
    ;;
  apply-lease)
    # Sweep FIRST: if a shadow file is present, every edit below is silently overridden by it, and
    # the run would report success while changing nothing on the running node.
    echo "staticPodPath sweep (must precede any manifest edit):"
    sweep_static_pod_dir
    echo ""
    echo "kube-controller-manager / kube-scheduler:"
    patch_leader_elect kube-controller-manager
    patch_leader_elect kube-scheduler
    echo ""
    echo "etcd (CPU request only -- use --etcd-compaction separately for auto-compaction/defrag):"
    etcd_cpu_apply
    echo ""
    echo "kube-apiserver CPU request (share lever, discovery-sized):"
    patch_apiserver_cpu_request
    ;;
  revert-lease)
    echo "kube-controller-manager / kube-scheduler:"
    revert_leader_elect kube-controller-manager
    revert_leader_elect kube-scheduler
    echo ""
    echo "etcd:"
    revert_etcd
    ;;
  etcd-compaction)
    echo "etcd auto-compaction + defrag:"
    etcd_compaction_apply
    ;;
  kube-vip-status)
    kube_vip_drift_status
    ;;
esac
REMOTE

cat > "$TMP/kube-vip-apply.sh" <<REMOTE
#!/bin/bash
set -euo pipefail
VIP_LEASE_DURATION="${VIP_LEASE_DURATION}"
VIP_RENEW_DEADLINE="${VIP_RENEW_DEADLINE}"
VIP_RETRY_PERIOD="${VIP_RETRY_PERIOD}"
FILE=/etc/kubernetes/manifests/kube-vip.yaml

[ -f "\$FILE" ] || { echo "ERROR: \$FILE not found -- skipping."; exit 3; }

CUR="\$(cat "\$FILE")"
echo "Current kube-vip lease settings:"
echo "\$CUR" | grep -A1 'vip_lease\|vip_renewdeadline\|vip_retryperiod' | grep -E 'name|value' | paste - - | sed 's/^/  /'

NEW="\$(echo "\$CUR" | sed -E "/name: vip_leaseduration/{n;s/value: *\"?[0-9]+\"?/value: \"\${VIP_LEASE_DURATION}\"/}" \\
                     | sed -E "/name: vip_renewdeadline/{n;s/value: *\"?[0-9]+\"?/value: \"\${VIP_RENEW_DEADLINE}\"/}" \\
                     | sed -E "/name: vip_retryperiod/{n;s/value: *\"?[0-9]+\"?/value: \"\${VIP_RETRY_PERIOD}\"/}")"

if [ "\$NEW" = "\$CUR" ]; then
  echo ""
  echo "No change needed -- current values already match the target (\${VIP_LEASE_DURATION}/\${VIP_RENEW_DEADLINE}/\${VIP_RETRY_PERIOD})."
  exit 0
fi

echo ""
echo "New kube-vip lease settings (about to apply):"
echo "\$NEW" | grep -A1 'vip_lease\|vip_renewdeadline\|vip_retryperiod' | grep -E 'name|value' | paste - - | sed 's/^/  /'
echo ""
echo "This edits the static manifest file directly -- kubelet restarts just the kube-vip pod in place. No"
echo "control-plane VM is provisioned or replaced. Expect a brief VIP flap (a few seconds) during the restart,"
echo "not a control-plane outage. (For the durable-but-disruptive alternative that patches the Cluster object"
echo "instead, see --kube-vip-cluster-patch -- deliberately separate, not run automatically.)"

mkdir -p /root/manifest-bak
mv /etc/kubernetes/manifests/kube-vip.yaml.bak.* /root/manifest-bak/ 2>/dev/null || true

cp "\$FILE" "/root/manifest-bak/kube-vip.yaml.bak.\$(date +%s)"

TMPFILE="/root/.hol-remediate-kubevip-\$\$"
printf '%s\\n' "\$NEW" > "\$TMPFILE"
cat "\$TMPFILE" > "\$FILE"
rm -f "\$TMPFILE"

POD_ID="\$(crictl pods --name '^kube-vip-'"\$(hostname)"'\$' -q 2>/dev/null | head -1 || true)"
if [ -z "\$POD_ID" ]; then
  echo "  WARNING: could not find the kube-vip pod via crictl to force a restart -- file is updated, but"
  echo "  kubelet may not pick it up until it next restarts on its own. Re-run --kube-vip-status to check."
  exit 0
fi
CID="\$(crictl ps --pod "\$POD_ID" -q 2>/dev/null | head -1 || true)"
[ -n "\$CID" ] && crictl stop "\$CID" >/dev/null 2>&1 || true

NEW_CID=""
for _ in 1 2 3 4 5 6 7 8; do
  sleep 2
  NEW_POD_ID="\$(crictl pods --name '^kube-vip-'"\$(hostname)"'\$' -q 2>/dev/null | head -1 || true)"
  NEW_CID="\$(crictl ps --pod "\$NEW_POD_ID" -q 2>/dev/null | head -1 || true)"
  [ -n "\$NEW_CID" ] && break
done
if [ -n "\$NEW_CID" ]; then
  LIVE_RENEW="\$(crictl inspect "\$NEW_CID" 2>/dev/null | grep -A1 '"key": "vip_renewdeadline"' | grep value | sed -E 's/.*"value": *"([0-9]+)".*/\\1/' || true)"
  if [ "\$LIVE_RENEW" = "\$VIP_RENEW_DEADLINE" ]; then
    echo ""
    echo "Applied and CONFIRMED live: the running kube-vip container now shows renewdeadline=\$LIVE_RENEW."
  else
    echo ""
    echo "  WARNING: file was updated and the pod was restarted, but the running container's renewdeadline"
    echo "  reads '\$LIVE_RENEW', not the expected \$VIP_RENEW_DEADLINE. Re-run --kube-vip-status to check --"
    echo "  kubelet may need another restart cycle to pick it up."
  fi
else
  echo ""
  echo "  WARNING: file was updated, but couldn't find a running kube-vip container afterward to verify against."
  echo "  Re-run --kube-vip-status to check."
fi
REMOTE

cat > "$TMP/kube-vip-cluster-patch.sh" <<REMOTE
#!/bin/bash
set -euo pipefail
VIP_LEASE_DURATION="${VIP_LEASE_DURATION}"
VIP_RENEW_DEADLINE="${VIP_RENEW_DEADLINE}"
VIP_RETRY_PERIOD="${VIP_RETRY_PERIOD}"

command -v jq >/dev/null 2>&1 || { echo "ERROR: jq not found on this node -- required for a safe read-modify-write of the Cluster object. Aborting, no changes made."; exit 3; }

cluster_ns_name="\$(kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s get cluster -A -o json | jq -r '.items[] | select(.spec.controlPlaneRef.kind=="KubeadmControlPlane") | "\(.metadata.namespace) \(.metadata.name)"' | head -1 || true)"
if [ -z "\$cluster_ns_name" ]; then
  echo "ERROR: could not discover a Cluster object with a KubeadmControlPlane controlPlaneRef from this node's own apiserver (or the apiserver did not respond within 20s). Aborting, no changes made."
  exit 3
fi
NS="\$(echo "\$cluster_ns_name" | awk '{print \$1}')"
NAME="\$(echo "\$cluster_ns_name" | awk '{print \$2}')"
echo "Target Cluster object: \$NAME -n \$NS"

CUR="\$(kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s get cluster "\$NAME" -n "\$NS" -o json | jq -r '.spec.topology.variables[]? | select(.name=="kubeVipPodManifest") | .value' || true)"
if [ -z "\$CUR" ]; then
  echo "ERROR: this Cluster object has no kubeVipPodManifest topology variable -- this cluster does not use the"
  echo "  mechanism this action assumes. Refusing to guess. Aborting, no changes made."
  exit 3
fi

echo ""
echo "Current kubeVipPodManifest lease settings:"
echo "\$CUR" | grep -A1 'vip_lease\|vip_renewdeadline\|vip_retryperiod' | grep -E 'name|value' | paste - - | sed 's/^/  /'

NEW="\$(echo "\$CUR" | sed -E "/name: vip_leaseduration/{n;s/value: *\"?[0-9]+\"?/value: \"\${VIP_LEASE_DURATION}\"/}" \\
                     | sed -E "/name: vip_renewdeadline/{n;s/value: *\"?[0-9]+\"?/value: \"\${VIP_RENEW_DEADLINE}\"/}" \\
                     | sed -E "/name: vip_retryperiod/{n;s/value: *\"?[0-9]+\"?/value: \"\${VIP_RETRY_PERIOD}\"/}")"

if [ "\$NEW" = "\$CUR" ]; then
  echo ""
  echo "No change needed -- current values already match the target (\${VIP_LEASE_DURATION}/\${VIP_RENEW_DEADLINE}/\${VIP_RETRY_PERIOD})."
  exit 0
fi

echo ""
echo "New kubeVipPodManifest lease settings (about to apply):"
echo "\$NEW" | grep -A1 'vip_lease\|vip_renewdeadline\|vip_retryperiod' | grep -E 'name|value' | paste - - | sed 's/^/  /'
echo ""
echo "This is expected to trigger a KubeadmControlPlane rollout (control-plane machine replace) on a CAPI-managed"
echo "cluster. For a single-control-plane-node cluster that means a brief real control-plane outage during rollout"
echo "-- and if the underlying infrastructure can't provision the replacement (seen live: a nested-ESXi host"
echo "unable to give the new VM enough vCPUs), the cluster can get stuck mid-rollout until manually unstuck."
echo "If you just want the lease values fixed without any of that risk, abort this and use --kube-vip-apply"
echo "instead -- it edits the running node's file directly and never touches this Cluster object."
echo ""
read -r -p "Type CONFIRM to patch Cluster/\$NAME -n \$NS now, anything else to abort: " ans
if [ "\$ans" != "CONFIRM" ]; then
  echo "Aborted -- no changes made."
  exit 0
fi

PATCH_BODY="\$(kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s get cluster "\$NAME" -n "\$NS" -o json | \\
  jq -c --arg newval "\$NEW" '{spec:{topology:{variables: (.spec.topology.variables | map(if .name=="kubeVipPodManifest" then .value=\$newval else . end))}}}')"

kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s patch cluster "\$NAME" -n "\$NS" --type=merge -p "\$PATCH_BODY"
echo ""
echo "Patched. Monitor rollout with:"
echo "  kubectl --kubeconfig=/etc/kubernetes/admin.conf get kubeadmcontrolplane -n \$NS -w"
echo "  kubectl --kubeconfig=/etc/kubernetes/admin.conf get machines -n \$NS -w"
echo "If a replacement machine gets stuck in Provisioning (infra can't create the VM), the old machine stays"
echo "healthy and untouched -- there's no outage while it's stuck, but it also won't finish on its own. Pause"
echo "the Cluster (kubectl patch cluster \$NAME -n \$NS --type=merge -p '{\"spec\":{\"paused\":true}}') and delete"
echo "the stuck replacement Machine object to stop CAPI from endlessly retrying the same doomed provision."
REMOTE

cat > "$TMP/kyverno-resync.sh" <<REMOTE
#!/bin/bash
set -euo pipefail
K="kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s"
KYVERNO_RESYNC_TARGET="${KYVERNO_RESYNC_TARGET}"
FAMC_ACTION="\$1"

## Retry a discovery 'get -o name' call 3x (5s apart) before concluding "not found." INCIDENT
## (2026-08-11, HOL-2711 10.138.150.5): a transient apiserver blip (plausibly the storm this
## remediation exists to fix) made this exact call return empty on two consecutive full runs even
## though the target ReleaseTemplate had existed for two months; a third run, zero code changes,
## succeeded. The old '2>/dev/null | grep ... || true' pattern made a failed kubectl call and a
## genuinely-absent object look identical. Now a persistent failure prints an unmistakable WARNING
## distinct from the normal "could not discover" message, so a log reviewer knows to re-run rather
## than trust the skip.
kc_discover_name() {  # <kubectl get ... -o name args...>
  local out rc i errf
  errf="\$(mktemp)"
  for i in 1 2 3; do
    out="\$(\$K "\$@" 2>"\$errf")"; rc=\$?
    if [ \$rc -eq 0 ]; then rm -f "\$errf"; printf '%s\n' "\$out"; return 0; fi
    [ "\$i" -lt 3 ] && sleep 5
  done
  echo "  WARNING: 'kubectl \$*' failed 3x in a row (apiserver unreachable/overloaded?) -- the" >&2
  echo "  'could not discover' message that may follow this is UNRELIABLE, not a confirmed absence." >&2
  echo "  last error: \$(tail -1 "\$errf" 2>/dev/null)" >&2
  rm -f "\$errf"
  return 2
}

discover_kyverno_rt() {
  kc_discover_name get releasetemplate -n vmsp-platform -o name | grep -iE '^releasetemplate\.releases\.vmsp\.vmware\.com/kyverno-' | grep -v policies | head -1 || true
}

kyverno_resync_status() {
  local rt cur
  rt="\$(discover_kyverno_rt)"
  if [ -z "\$rt" ]; then
    echo "  kyverno background-controller resyncPeriod: could not discover a kyverno ReleaseTemplate in -n vmsp-platform on this node (not installed here, apiserver unreachable, or a different platform version names/organizes it differently) -- skipping, not guessing"
    return
  fi
  cur="\$(\$K get "\$rt" -n vmsp-platform -o jsonpath='{.spec.helm.values.backgroundController.resyncPeriod}' 2>/dev/null || true)"
  if [ -z "\$cur" ]; then
    echo "  kyverno background-controller resyncPeriod: NOT SET in \$rt (chart default applies, 15m) -- target \${KYVERNO_RESYNC_TARGET}"
  else
    echo "  kyverno background-controller resyncPeriod: \$cur (\$rt) -- target \${KYVERNO_RESYNC_TARGET}"
  fi
}

kyverno_resync_apply() {
  local rt cur
  rt="\$(discover_kyverno_rt)"
  if [ -z "\$rt" ]; then
    echo "  kyverno: could not discover a kyverno ReleaseTemplate in -n vmsp-platform on this node -- refusing to guess, skipping"
    return
  fi
  cur="\$(\$K get "\$rt" -n vmsp-platform -o jsonpath='{.spec.helm.values.backgroundController.resyncPeriod}' 2>/dev/null || true)"
  if [ "\$cur" = "\${KYVERNO_RESYNC_TARGET}" ]; then
    echo "  kyverno background-controller resyncPeriod: already at target (\${KYVERNO_RESYNC_TARGET}, \$rt) -- no change"
    return
  fi
  if echo "\$cur" | grep -qE '^[0-9]+h\$'; then
    echo "  kyverno background-controller resyncPeriod: currently \$cur (\$rt), already >= the \${KYVERNO_RESYNC_TARGET} target -- leaving as-is"
    return
  fi
  mkdir -p /root/manifest-bak
  \$K get "\$rt" -n vmsp-platform -o yaml > "/root/manifest-bak/kyverno-releasetemplate-\$(date +%s).yaml"
  echo "  kyverno: backed up \$rt to /root/manifest-bak/ before patching"
  \$K patch "\$rt" -n vmsp-platform --type=merge -p '{"spec":{"helm":{"values":{"backgroundController":{"resyncPeriod":"'"\${KYVERNO_RESYNC_TARGET}"'"}}}}}' >/dev/null
  echo "  kyverno background-controller resyncPeriod: \${cur:-unset(15m default)} -> \${KYVERNO_RESYNC_TARGET} patched on \$rt."
  echo "  This is a scoped merge patch -- only backgroundController.resyncPeriod, nothing else in the chart's values touched."
  echo "  vmsp-operator re-renders this into the live Deployment within ~30-60s. Verifying..."
  local i live
  for i in 1 2 3 4 5 6; do
    sleep 10
    live="\$(\$K get deployment kyverno-background-controller -n vmsp-policies -o jsonpath='{.spec.template.spec.containers[0].args}' 2>/dev/null | grep -oE -- '--resyncPeriod=[^[:space:]]*' || true)"
    [ -n "\$live" ] && [ "\$live" != "--resyncPeriod=15m" ] && break
  done
  if [ -n "\$live" ]; then
    echo "  Verified live on deployment/kyverno-background-controller -n vmsp-policies: \$live"
  else
    echo "  WARNING: could not confirm the new resyncPeriod on the live deployment within ~60s -- vmsp-operator's"
    echo "  reconcile cycle may just be slower right now under load. Re-run --status (or this flag again) to check."
  fi
  echo ""
  echo "  NOTE (expected, not a failure): patching this ReleaseTemplate makes vmsp-operator do a full re-list of"
  echo "  every ReleaseTemplate object in the cluster, which briefly spikes etcd read latency and commonly triggers"
  echo "  ONE instance of the platform-wide leader-election restart-cascade (see header) as a side effect within"
  echo "  the next minute or two. This is a known, one-time, worthwhile cost of applying this durable fix -- not a"
  echo "  new problem the fix introduced. It does not recur on its own afterward."
}

case "\$FAMC_ACTION" in
  status) kyverno_resync_status ;;
  apply)  kyverno_resync_apply ;;
esac
REMOTE

cat > "$TMP/envoy-gateway-fix.sh" <<REMOTE
#!/bin/bash
set -euo pipefail
K="kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s"
EG_MEM_LIMIT="${EG_MEM_LIMIT}"
EG_MEM_REQUEST="${EG_MEM_REQUEST}"
FAMC_ACTION="\$1"

detect_and_handle_rogue_mem_timer() {
  local units unit
  units="\$(systemctl list-unit-files --all --no-legend 2>/dev/null | awk '{print \$1}' | grep -iE 'envoy.*mem|mem.*envoy' || true)"
  if [ -z "\$units" ]; then
    echo "  rogue memory-clamp timer/service: none found on this node matching 'envoy'+'mem' in the unit name"
    echo "  (this is the common case -- only seen in one lab so far)."
    return
  fi
  echo "  rogue memory-clamp timer/service: FOUND on this node -- left running, this will fight any"
  echo "  Helm/ReleaseTemplate-based memory fix below within about one of its own run intervals:"
  for unit in \$units; do
    echo "    \$unit:"
    systemctl cat "\$unit" 2>/dev/null | sed 's/^/      /' || true
    if systemctl is-active --quiet "\$unit" 2>/dev/null || systemctl is-enabled --quiet "\$unit" 2>/dev/null; then
      if [ "\$FAMC_ACTION" = "apply" ]; then
        systemctl disable --now "\$unit" 2>/dev/null || true
        echo "    -> disabled --now \$unit (unit file left on disk, not deleted -- fully reversible)"
      else
        echo "    -> ACTIVE/ENABLED -- would be disabled by --envoy-gateway-fix (status-only check here, no change made)"
      fi
    else
      echo "    -> already inactive/disabled -- no action needed"
    fi
  done
}

## See kc_discover_name's incident note in kyverno-resync.sh's copy of this same helper -- retries
## a discovery call 3x before concluding "not found," and warns loudly (distinct from the normal
## "could not discover" message) rather than silently trusting a possibly-transient API failure.
kc_discover_name() {  # <kubectl get ... -o name args...>
  local out rc i errf
  errf="\$(mktemp)"
  for i in 1 2 3; do
    out="\$(\$K "\$@" 2>"\$errf")"; rc=\$?
    if [ \$rc -eq 0 ]; then rm -f "\$errf"; printf '%s\n' "\$out"; return 0; fi
    [ "\$i" -lt 3 ] && sleep 5
  done
  echo "  WARNING: 'kubectl \$*' failed 3x in a row (apiserver unreachable/overloaded?) -- the" >&2
  echo "  'could not discover' message that may follow this is UNRELIABLE, not a confirmed absence." >&2
  echo "  last error: \$(tail -1 "\$errf" 2>/dev/null)" >&2
  rm -f "\$errf"
  return 2
}

discover_eg_rt() {
  kc_discover_name get releasetemplate -n vmsp-platform -o name | grep -iE '^releasetemplate\.releases\.vmsp\.vmware\.com/envoyproxy-gateway-' | head -1 || true
}

eg_status() {
  local rt cur_mem cur_le
  rt="\$(discover_eg_rt)"
  if [ -z "\$rt" ]; then
    echo "  envoy-gateway ReleaseTemplate: could not discover one in -n vmsp-platform on this node -- skipping, not guessing"
    return
  fi
  cur_mem="\$(\$K get "\$rt" -n vmsp-platform -o jsonpath='{.spec.helm.values.deployment.envoyGateway.resources.limits.memory}' 2>/dev/null || true)"
  cur_le="\$(\$K get "\$rt" -n vmsp-platform -o jsonpath='{.spec.helm.values.config.envoyGateway.provider.kubernetes.leaderElection.disable}' 2>/dev/null || true)"
  echo "  envoy-gateway ReleaseTemplate (\$rt): memory.limit=\${cur_mem:-unset(chart default)} leaderElection.disable=\${cur_le:-unset(false/enabled)} -- target memory.limit=\${EG_MEM_LIMIT} leaderElection.disable=true"
}

eg_apply() {
  local rt cur_mem cur_le replicas
  rt="\$(discover_eg_rt)"
  if [ -z "\$rt" ]; then
    echo "  envoy-gateway: could not discover a ReleaseTemplate in -n vmsp-platform on this node -- refusing to guess, skipping"
    return
  fi
  cur_mem="\$(\$K get "\$rt" -n vmsp-platform -o jsonpath='{.spec.helm.values.deployment.envoyGateway.resources.limits.memory}' 2>/dev/null || true)"
  cur_le="\$(\$K get "\$rt" -n vmsp-platform -o jsonpath='{.spec.helm.values.config.envoyGateway.provider.kubernetes.leaderElection.disable}' 2>/dev/null || true)"
  if [ "\$cur_mem" = "\${EG_MEM_LIMIT}" ] && [ "\$cur_le" = "true" ]; then
    echo "  envoy-gateway ReleaseTemplate: already at target (memory.limit=\${EG_MEM_LIMIT}, leaderElection.disable=true, \$rt) -- no change"
    return
  fi
  replicas="\$(\$K get deployment envoy-gateway -n vmsp-platform -o jsonpath='{.spec.replicas}' 2>/dev/null || true)"
  if [ -n "\$replicas" ] && [ "\$replicas" != "1" ]; then
    echo "  envoy-gateway: deployment/envoy-gateway -n vmsp-platform is running \$replicas replicas, not 1 -- disabling"
    echo "  leader election on a genuinely multi-replica HA deployment would be a correctness regression, not a"
    echo "  fix. Refusing to apply leaderElection.disable=true. Skipping this whole action -- verify manually."
    return
  fi
  mkdir -p /root/manifest-bak
  \$K get "\$rt" -n vmsp-platform -o yaml > "/root/manifest-bak/envoy-gateway-releasetemplate-\$(date +%s).yaml"
  echo "  envoy-gateway: backed up \$rt to /root/manifest-bak/ before patching"
  \$K patch "\$rt" -n vmsp-platform --type=merge -p '{"spec":{"helm":{"values":{"deployment":{"envoyGateway":{"resources":{"limits":{"memory":"'"\${EG_MEM_LIMIT}"'"},"requests":{"memory":"'"\${EG_MEM_REQUEST}"'"}}}},"config":{"envoyGateway":{"provider":{"kubernetes":{"leaderElection":{"disable":true}}}}}}}}}' >/dev/null
  echo "  envoy-gateway ReleaseTemplate: memory.limit \${cur_mem:-unset}->\${EG_MEM_LIMIT}, leaderElection.disable \${cur_le:-unset(false)}->true patched on \$rt."
  echo ""
  echo "  Forcing an immediate Flux reconcile rather than waiting on its normal interval (helm-controller's own"
  echo "  interval can be much slower than the ~15-30s vmsp-operator cycle that renders this ReleaseTemplate into"
  echo "  the live HelmRelease -- waiting passively left the Deployment on stale values for several minutes in"
  echo "  testing)..."
  \$K annotate helmrelease envoyproxy-gateway -n vmsp-platform reconcile.fluxcd.io/requestedAt="\$(date +%s)" --overwrite >/dev/null 2>&1 \\
    && echo "  Flux reconcile requested." \\
    || echo "  WARNING: could not annotate helmrelease/envoyproxy-gateway -n vmsp-platform to force reconcile (name/namespace may differ on this platform version) -- it will still pick this up on Flux's normal interval, just possibly not for a few minutes."
  echo ""
  echo "  Verifying (can take 1-2 minutes for the Deployment + ConfigMap to actually update)..."
  local i live_res live_le
  for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
    sleep 10
    live_res="\$(\$K get deployment envoy-gateway -n vmsp-platform -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}' 2>/dev/null || true)"
    [ "\$live_res" = "\${EG_MEM_LIMIT}" ] && break
  done
  live_le="\$(\$K get cm envoy-gateway-config -n vmsp-platform -o jsonpath='{.data.envoy-gateway\.yaml}' 2>/dev/null | grep -A2 leaderElection || true)"
  if [ "\$live_res" = "\${EG_MEM_LIMIT}" ]; then
    echo "  Verified live: deployment/envoy-gateway -n vmsp-platform memory.limit=\$live_res"
  else
    echo "  WARNING: deployment/envoy-gateway -n vmsp-platform memory.limit still reads '\${live_res:-unset}', not"
    echo "  \${EG_MEM_LIMIT}, after ~2 minutes of waiting. Re-run --status (or this flag again) to check -- vmsp-"
    echo "  operator/Flux may just be slower than usual under load, or the rogue memory-clamp timer above may"
    echo "  still be active if it wasn't found/disabled."
  fi
  if echo "\$live_le" | grep -q 'disable: true'; then
    echo "  Verified live: configmap/envoy-gateway-config -n vmsp-platform shows leaderElection.disable: true"
  else
    echo "  WARNING: configmap/envoy-gateway-config -n vmsp-platform does not yet show leaderElection.disable: true:"
    echo "\$live_le" | sed 's/^/    /'
    echo "  Re-run --status to check again shortly."
  fi
  echo ""
  echo "  NOTE (expected, not a failure): patching this ReleaseTemplate makes vmsp-operator do a full re-list of"
  echo "  every ReleaseTemplate object in the cluster, which briefly spikes etcd read latency and commonly triggers"
  echo "  ONE instance of the platform-wide leader-election restart-cascade (see header) as a side effect within"
  echo "  the next minute or two. This is a known, one-time, worthwhile cost of applying this durable fix."
}

case "\$FAMC_ACTION" in
  status) detect_and_handle_rogue_mem_timer; eg_status ;;
  apply)  detect_and_handle_rogue_mem_timer; eg_apply ;;
esac
REMOTE
}

# ── Family B/C delivery: run the right remote script on node <ip> ─────────────
famb_run() {  # <ip> <role> <action>
  local ip="$1" role="$2" action="$3"
  echo "=== ${role} (${ip}) -- ${action} ==="
  reachable "$ip" || { echo "  ERROR: cannot reach ${NODE_USER}@${ip} via manager"; return 2; }
  case "$action" in
    kube-vip-apply)         node_run_file "$ip" "$TMP/kube-vip-apply.sh" ;;
    kube-vip-cluster-patch) node_run_file_confirm "$ip" "$TMP/kube-vip-cluster-patch.sh" ;;
    kyverno-resync-relax)   node_run_file "$ip" "$TMP/kyverno-resync.sh" apply ;;
    kyverno-status)         node_run_file "$ip" "$TMP/kyverno-resync.sh" status ;;
    envoy-gateway-fix)      node_run_file "$ip" "$TMP/envoy-gateway-fix.sh" apply ;;
    envoy-status)           node_run_file "$ip" "$TMP/envoy-gateway-fix.sh" status ;;
    *)                      node_run_file "$ip" "$TMP/remediate-lease.sh" "$action" ;;
  esac
  echo ""
}

# ── Family A (VSP CP): drift-keeper files (VERBATIM from hol-remediate.sh) ─────
build_vsp_keeper_files() {
  cat > "$TMP/vsp-fleet-depot-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: download-service
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 15}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 15}
        startupProbe:   {timeoutSeconds: 10, failureThreshold: 60}
        resources: {limits: {memory: 2Gi}, requests: {cpu: 300m, memory: 512Mi}}
      - name: file-server
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 15}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 15}
      - name: proxy-forwarder
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 15}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 15}
PATCH

  cat > "$TMP/vsp-fleet-lcm-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: fleetbuild
        livenessProbe:  {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        readinessProbe: {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        startupProbe:   {timeoutSeconds: 10, failureThreshold: 60, periodSeconds: 10}
PATCH

  cat > "$TMP/vsp-envoy-gateway-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: envoy-gateway
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 20}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 10}
        startupProbe:
          httpGet: {path: /healthz, port: 8081, scheme: HTTP}
          timeoutSeconds: 10
          failureThreshold: 60
          periodSeconds: 10
PATCH

  cat > "$TMP/vsp-vidb-service-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: vidb-service
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 10}
PATCH

  cat > "$TMP/vsp-sddcbuild-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: sddcbuild
        livenessProbe:  {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        readinessProbe: {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        startupProbe:   {timeoutSeconds: 10, failureThreshold: 60, periodSeconds: 10}
PATCH

  cat > "$TMP/vsp-sddcupgrade-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: sddcupgrade
        livenessProbe:  {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        readinessProbe: {timeoutSeconds: 15, failureThreshold: 8, periodSeconds: 15}
        startupProbe:   {timeoutSeconds: 10, failureThreshold: 60, periodSeconds: 10}
PATCH

  cat > "$TMP/vsp-prometheus-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: prometheus
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 8, periodSeconds: 10}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 8, periodSeconds: 10}
        resources: {limits: {memory: 4Gi}, requests: {memory: 1Gi}}
PATCH

  cat > "$TMP/vsp-ksm-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: kube-state-metrics
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6}
PATCH

  cat > "$TMP/vsp-node-exporter-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: node-exporter
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6}
PATCH

  cat > "$TMP/vsp-fleet-depot-keeper.sh" <<'KEEPER'
#!/bin/bash
# Re-applies the VSP fleet + gateway fixes if a reconciler reverted them.
export KUBECONFIG=/etc/kubernetes/admin.conf
KB=/usr/local/bin/kubectl
PROBE_TARGETS='
vcf-fleet-depot deployment/depot-service download-service 10 /usr/local/etc/vsp-fleet-depot-patch.yaml
vcf-fleet-lcm deployment/vcf-fleet-build-service-fleetbuild fleetbuild 15 /usr/local/etc/vsp-fleet-lcm-patch.yaml
vidb-external deployment/vidb-service vidb-service 10 /usr/local/etc/vsp-vidb-service-patch.yaml
vcf-sddc-lcm deployment/vcf-sddc-build-service-sddcbuild sddcbuild 15 /usr/local/etc/vsp-sddcbuild-patch.yaml
vcf-sddc-lcm deployment/vcf-sddc-upgrade-service-sddcupgrade sddcupgrade 15 /usr/local/etc/vsp-sddcupgrade-patch.yaml
vmsp-platform statefulset/prometheus-kube-prometheus-stack-prometheus prometheus 10 /usr/local/etc/vsp-prometheus-patch.yaml
vmsp-platform deployment/kube-prometheus-stack-kube-state-metrics kube-state-metrics 10 /usr/local/etc/vsp-ksm-patch.yaml
vmsp-platform daemonset/kube-prometheus-stack-prometheus-node-exporter node-exporter 10 /usr/local/etc/vsp-node-exporter-patch.yaml
'
echo "$PROBE_TARGETS" | while read -r NS REF CON WANT PF; do
  [ -z "$NS" ] && continue
  CUR=$($KB -n "$NS" get "$REF" -o jsonpath="{.spec.template.spec.containers[?(@.name==\"$CON\")].livenessProbe.timeoutSeconds}" 2>/dev/null)
  if [ "$CUR" != "$WANT" ]; then
    $KB -n "$NS" patch "$REF" --type=strategic --patch-file "$PF" >/dev/null 2>&1 \
      && logger -t vsp-fleet-depot-keeper "drift corrected: $NS/$REF probes (livenessTimeout was '${CUR:-unset}')"
  fi
done
# envoy-gateway memory: MUST equal Family C EG_MEM_LIMIT/EG_MEM_REQUEST (remediate-lab.sh:70 =
# 8Gi/1536Mi). Family C sets those in the ReleaseTemplate (durable, vmsp-operator-rendered); this
# live clamp is defense-in-depth ONLY and MUST match that value. If it disagrees (e.g. stale 4Gi)
# it fights vmsp-operator every 60s -> envoy-gateway rollout churn -> vmsp-gateway restarts ->
# VCF Ops "Software Depot"/"Lifecycle" UI flaps. Keep these two literals in sync with line 70.
EGMEM=$($KB -n vmsp-platform get deploy envoy-gateway -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}' 2>/dev/null)
if [ "$EGMEM" != "8Gi" ]; then
  $KB -n vmsp-platform set resources deploy/envoy-gateway --limits=memory=8Gi --requests=memory=1536Mi >/dev/null 2>&1 \
    && logger -t vsp-fleet-depot-keeper "drift corrected: envoy-gateway memory -> 8Gi (was '${EGMEM:-unset}')"
fi
EGPROBE=$($KB -n vmsp-platform get deploy envoy-gateway -o jsonpath='{.spec.template.spec.containers[?(@.name=="envoy-gateway")].livenessProbe.timeoutSeconds}' 2>/dev/null)
if [ "$EGPROBE" != "10" ]; then
  $KB -n vmsp-platform patch deploy envoy-gateway --type=strategic --patch-file /usr/local/etc/vsp-envoy-gateway-patch.yaml >/dev/null 2>&1 \
    && logger -t vsp-fleet-depot-keeper "drift corrected: envoy-gateway probes (livenessTimeout was '${EGPROBE:-unset}')"
fi
PROMMEM=$($KB -n vmsp-platform get statefulset prometheus-kube-prometheus-stack-prometheus -o jsonpath='{.spec.template.spec.containers[?(@.name=="prometheus")].resources.limits.memory}' 2>/dev/null)
if [ "$PROMMEM" != "4Gi" ]; then
  $KB -n vmsp-platform patch statefulset prometheus-kube-prometheus-stack-prometheus --type=strategic --patch-file /usr/local/etc/vsp-prometheus-patch.yaml >/dev/null 2>&1 \
    && logger -t vsp-fleet-depot-keeper "drift corrected: prometheus memory -> 4Gi (was '${PROMMEM:-unset}')"
fi
KEEPER

  cat > "$TMP/vsp-fleet-depot-keeper.service" <<'SVC'
[Unit]
Description=VSP: re-apply vcf-fleet-depot + fleet-lcm + envoy-gateway fixes (drift keeper)
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/vsp-fleet-depot-keeper.sh
SVC

  cat > "$TMP/vsp-fleet-depot-keeper.timer" <<'TIMER'
[Unit]
Description=VSP: run vsp-fleet-depot-keeper every 60s (drift watcher)
[Timer]
OnBootSec=2min
OnUnitActiveSec=60s
Unit=vsp-fleet-depot-keeper.service
[Install]
WantedBy=timers.target
TIMER
}

install_vsp_keeper() {  # <ip>
  local ip="$1"
  echo "=== VSP Family A keepers -> ${ip} ==="
  build_vsp_keeper_files
  TARFILES="vsp-fleet-depot-patch.yaml vsp-fleet-lcm-patch.yaml vsp-envoy-gateway-patch.yaml vsp-vidb-service-patch.yaml vsp-sddcbuild-patch.yaml vsp-sddcupgrade-patch.yaml vsp-prometheus-patch.yaml vsp-ksm-patch.yaml vsp-node-exporter-patch.yaml vsp-fleet-depot-keeper.sh vsp-fleet-depot-keeper.service vsp-fleet-depot-keeper.timer"
  push_files_and_run "$ip" <<'INSTALL'
install -m 0755 /tmp/vsp-fleet-depot-keeper.sh /usr/local/bin/vsp-fleet-depot-keeper.sh &&
install -d /usr/local/etc &&
install -m 0644 /tmp/vsp-fleet-depot-patch.yaml /usr/local/etc/vsp-fleet-depot-patch.yaml &&
install -m 0644 /tmp/vsp-fleet-lcm-patch.yaml   /usr/local/etc/vsp-fleet-lcm-patch.yaml &&
install -m 0644 /tmp/vsp-envoy-gateway-patch.yaml /usr/local/etc/vsp-envoy-gateway-patch.yaml &&
install -m 0644 /tmp/vsp-vidb-service-patch.yaml /usr/local/etc/vsp-vidb-service-patch.yaml &&
install -m 0644 /tmp/vsp-sddcbuild-patch.yaml /usr/local/etc/vsp-sddcbuild-patch.yaml &&
install -m 0644 /tmp/vsp-sddcupgrade-patch.yaml /usr/local/etc/vsp-sddcupgrade-patch.yaml &&
install -m 0644 /tmp/vsp-prometheus-patch.yaml /usr/local/etc/vsp-prometheus-patch.yaml &&
install -m 0644 /tmp/vsp-ksm-patch.yaml /usr/local/etc/vsp-ksm-patch.yaml &&
install -m 0644 /tmp/vsp-node-exporter-patch.yaml /usr/local/etc/vsp-node-exporter-patch.yaml &&
install -m 0644 /tmp/vsp-fleet-depot-keeper.service /etc/systemd/system/vsp-fleet-depot-keeper.service &&
install -m 0644 /tmp/vsp-fleet-depot-keeper.timer   /etc/systemd/system/vsp-fleet-depot-keeper.timer &&
systemctl daemon-reload && systemctl enable --now vsp-fleet-depot-keeper.timer &&
/usr/local/bin/vsp-fleet-depot-keeper.sh ;
echo "   VSP keeper active: $(systemctl is-active vsp-fleet-depot-keeper.timer 2>/dev/null)"
rm -f /tmp/vsp-fleet-depot-* /tmp/vsp-fleet-lcm-* /tmp/vsp-envoy-gateway-* /tmp/vsp-vidb-service-* /tmp/vsp-sddcbuild-* /tmp/vsp-sddcupgrade-* /tmp/vsp-prometheus-* /tmp/vsp-ksm-* /tmp/vsp-node-exporter-*
INSTALL
  echo ""
}

# ── Family A (VCFA auto-a): drift-keeper files (VERBATIM from hol-remediate.sh) ─
#    PLUS the NEW prelude leader-election keeper that fixes the provider login.
build_vcfa_keeper_files() {
  cat > "$TMP/vcfa-eg-mem-keeper.sh" <<'EGK'
#!/usr/bin/env bash
# Keep the auto-a Envoy Gateway operator memory up (OOM@1Gi otherwise -> the
# operator flaps -> vcfa-gateway-configuration envoy loses xDS config -> UI 'rc=000').
# WANT_LIM/WANT_REQ MUST equal Family C EG_MEM_LIMIT/EG_MEM_REQUEST (remediate-lab.sh:70 =
# 8Gi/1536Mi). Family C sets those durably in the ReleaseTemplate; this live clamp is
# defense-in-depth and MUST match, else it fights vmsp-operator every 60s (rollout churn).
set -u
K="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
NS="vmsp-platform"; WANT_LIM="8Gi"; WANT_REQ="1536Mi"
$K -n "$NS" get deploy envoy-gateway >/dev/null 2>&1 || exit 0
CUR_LIM=$($K -n "$NS" get deploy envoy-gateway -o jsonpath='{.spec.template.spec.containers[?(@.name=="envoy-gateway")].resources.limits.memory}' 2>/dev/null || echo "")
CUR_REQ=$($K -n "$NS" get deploy envoy-gateway -o jsonpath='{.spec.template.spec.containers[?(@.name=="envoy-gateway")].resources.requests.memory}' 2>/dev/null || echo "")
if [[ "$CUR_LIM" != "$WANT_LIM" || "$CUR_REQ" != "$WANT_REQ" ]]; then
  $K -n "$NS" set resources deploy/envoy-gateway --limits=memory=$WANT_LIM --requests=memory=$WANT_REQ >/dev/null 2>&1 \
    && logger -t vcfa-eg-mem-keeper "drift corrected: envoy-gateway mem limit=$CUR_LIM->$WANT_LIM req=$CUR_REQ->$WANT_REQ"
fi
EGPROBE=$($K -n "$NS" get deploy envoy-gateway -o jsonpath='{.spec.template.spec.containers[?(@.name=="envoy-gateway")].livenessProbe.timeoutSeconds}' 2>/dev/null || echo "")
if [ "$EGPROBE" != "10" ]; then
  $K -n "$NS" patch deploy envoy-gateway --type=strategic --patch-file /usr/local/etc/vcfa-envoy-gateway-patch.yaml >/dev/null 2>&1 \
    && logger -t vcfa-eg-mem-keeper "drift corrected: envoy-gateway probes (livenessTimeout was '${EGPROBE:-unset}')"
fi
EGK

  cat > "$TMP/vcfa-eg-mem-keeper.service" <<'EGS'
[Unit]
Description=VCFA: keep envoy-gateway operator memory at 8Gi (drift keeper)
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/vcfa-eg-mem-keeper.sh
EGS

  cat > "$TMP/vcfa-eg-mem-keeper.timer" <<'EGT'
[Unit]
Description=VCFA: run vcfa-eg-mem-keeper every 60s (drift watcher)
[Timer]
OnBootSec=2min
OnUnitActiveSec=60s
AccuracySec=10s
Unit=vcfa-eg-mem-keeper.service
[Install]
WantedBy=timers.target
EGT

  cat > "$TMP/vcfa-vip-watchdog.sh" <<'VIPWD'
#!/bin/bash
# Event-driven VCFA VIP watchdog: re-adds/repairs the provider CP + gateway VIPs on
# eth0 immediately if the platform deletes/deprecates them (keeps the API/UI reachable).
VIPS="10.1.1.72 10.1.1.69 10.1.1.70"
ETH=eth0
fix_vips() {
  for vip in $VIPS; do
    STATUS=$(ip -oneline addr show dev $ETH 2>/dev/null | grep -F "${vip}/32" | head -1)
    if [ -z "$STATUS" ]; then
      ip addr add ${vip}/32 dev $ETH valid_lft forever preferred_lft forever 2>/dev/null || \
        ip addr replace ${vip}/32 dev $ETH valid_lft forever preferred_lft forever 2>/dev/null || true
      command -v arping >/dev/null 2>&1 && arping -c 1 -A -I $ETH $vip &>/dev/null &
    elif echo "$STATUS" | grep -q deprecated; then
      ip addr change ${vip}/32 dev $ETH valid_lft forever preferred_lft forever 2>/dev/null || true
    fi
  done
}
fix_vips
/usr/sbin/ip monitor addr dev $ETH 2>&1 | while read line; do
  echo "$line" | grep -qEi "deleted|deprecated" && fix_vips
done
VIPWD

  cat > "$TMP/vcfa-vip-watchdog.service" <<'VIPS'
[Unit]
Description=VCFA VIP Watchdog - keeps CP and gateway VIPs preferred_lft=forever
After=network.target
DefaultDependencies=no
[Service]
Type=simple
ExecStart=/usr/local/bin/vcfa-vip-watchdog.sh
Restart=always
RestartSec=2
[Install]
WantedBy=multi-user.target
VIPS

  cat > "$TMP/vcfa-envoy-gateway-patch.yaml" <<'PATCH'
spec:
  template:
    spec:
      containers:
      - name: envoy-gateway
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 20}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 10}
        startupProbe:
          httpGet: {path: /healthz, port: 8081, scheme: HTTP}
          timeoutSeconds: 10
          failureThreshold: 60
          periodSeconds: 10
PATCH

  cat > "$TMP/vcfa-support-bundle-operator-patch.yaml" <<'SBOP'
spec:
  template:
    spec:
      containers:
      - name: operator
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 10}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 10}
SBOP

  cat > "$TMP/vcfa-support-bundle-copier-patch.yaml" <<'SBCP'
spec:
  template:
    spec:
      containers:
      - name: support-bundle
        livenessProbe:  {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 10}
        readinessProbe: {timeoutSeconds: 10, failureThreshold: 6, periodSeconds: 10}
SBCP

  cat > "$TMP/vcfa-support-bundle-keeper.sh" <<'SBK'
#!/bin/bash
# Relax over-tight (1s) liveness/readiness probes on the support-bundle operator +
# logcopiers -- same probe-kill signature as kyverno/capi/VSP depot-service. Flux/Helm
# (support-bundle-9.1.1666 chart) reverts plain patches, hence the keeper.
# support-bundle-cleanup-nodes and support-bundle-logoffloader have NO probes
# configured -- their restarts aren't probe-kills, so they're intentionally not here.
K="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
NS="vmsp-platform"
check_patch() {
  local ref="$1" container="$2" patchfile="$3" cur
  cur=$($K -n "$NS" get "$ref" -o jsonpath="{.spec.template.spec.containers[?(@.name==\"$container\")].livenessProbe.timeoutSeconds}" 2>/dev/null)
  if [ "$cur" != "10" ]; then
    $K -n "$NS" patch "$ref" --type=strategic --patch-file "$patchfile" >/dev/null 2>&1 \
      && logger -t vcfa-support-bundle-keeper "drift corrected: $ref/$container probes (livenessTimeout was '${cur:-unset}')"
  fi
}
check_patch "deployment/support-bundle" "operator" "/usr/local/etc/vcfa-support-bundle-operator-patch.yaml"
check_patch "deployment/support-bundle-logcopier-event-tailer" "support-bundle" "/usr/local/etc/vcfa-support-bundle-copier-patch.yaml"
check_patch "daemonset/support-bundle-logcopier" "support-bundle" "/usr/local/etc/vcfa-support-bundle-copier-patch.yaml"
SBK

  cat > "$TMP/vcfa-support-bundle-keeper.service" <<'SBS'
[Unit]
Description=VCFA: relax support-bundle operator/logcopier probes (drift keeper)
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/vcfa-support-bundle-keeper.sh
SBS

  cat > "$TMP/vcfa-support-bundle-keeper.timer" <<'SBT'
[Unit]
Description=VCFA: run vcfa-support-bundle-keeper every 60s (drift watcher)
[Timer]
OnBootSec=2min
OnUnitActiveSec=60s
AccuracySec=10s
Unit=vcfa-support-bundle-keeper.service
[Install]
WantedBy=timers.target
SBT

  # ── NEW: prelude (VCF Automation) leader-election keeper -- THE PROVIDER-LOGIN FIX ──
  cat > "$TMP/vcfa-prelude-le-keeper.sh" <<'PRELUDE'
#!/bin/bash
# VCFA PROVIDER-LOGIN FIX. The VCF Automation backend (namespace "prelude") is in the same
# etcd leader-election cascade as the rest of this node: resource-manager-server,
# vcfa-service-manager, encryption-manager, intent-server (all replicas=1) die with
#   "failed to renew lease ... context deadline exceeded" -> "leader election lost" -> restart.
# When they flap the provider UI/API returns
#   "upstream connect error or disconnect/reset before headers ... connection timeout"
# at auto-a.../login. etcd is already 2500m (Family B) and the cascade persists, so the durable
# fix is to DISABLE leader election on these replicas=1 services (LE buys zero HA at 1 replica).
# They are Helm/Flux-managed (helm-controller reverts a direct patch), so this runs as a KEEPER
# (re-applies on an interval) -- the same Family-A pattern used elsewhere in this toolkit.
# Each service's LE toggle DIFFERS and is handled by its own known mechanism; only ever acts
# when the service is actually replicas==1, and only patches on real drift (idempotent).
K="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
NS=prelude

# resource-manager-server: arg is --enable-leader-election=$(ENABLE_LEADER_ELECTION) --
# disable by setting the referenced env var to false.
if $K -n "$NS" get deploy resource-manager-server >/dev/null 2>&1; then
  repl=$($K -n "$NS" get deploy resource-manager-server -o jsonpath='{.spec.replicas}' 2>/dev/null)
  if [ "$repl" = "1" ]; then
    cur=$($K -n "$NS" get deploy resource-manager-server -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="ENABLE_LEADER_ELECTION")].value}' 2>/dev/null)
    if [ "$cur" != "false" ]; then
      $K -n "$NS" set env deploy/resource-manager-server ENABLE_LEADER_ELECTION=false >/dev/null 2>&1 \
        && logger -t vcfa-prelude-le-keeper "drift corrected: resource-manager-server ENABLE_LEADER_ELECTION='${cur:-unset}'->false"
    fi
  fi
fi

# vcfa-service-manager (--leader-elect) and encryption-manager (--leader-election-enabled):
# flip the bare / '=true' arg to '=false' via a surgical JSON patch (locate the arg index).
# Only for replicas==1. python3 is present on these nodes (same as the VSP disable-capi-le path).
python3 - <<'PY'
import subprocess, json
K=["kubectl","--kubeconfig=/etc/kubernetes/admin.conf","-n","prelude"]
targets=[
    ("vcfa-service-manager","--leader-elect","--leader-elect=false"),
    ("encryption-manager","--leader-election-enabled","--leader-election-enabled=false"),
]
for dep, base, false_form in targets:
    r=subprocess.run(K+["get","deploy",dep,"-o","json"],capture_output=True,text=True)
    if r.returncode!=0:
        continue
    d=json.loads(r.stdout)
    if d["spec"].get("replicas") not in (1, None):   # only replicas==1 (LE gives HA otherwise)
        continue
    for ci,c in enumerate(d["spec"]["template"]["spec"]["containers"]):
        for ai,a in enumerate(c.get("args",[]) or []):
            if a in (base, base+"=true"):
                p=[{"op":"replace","path":"/spec/template/spec/containers/%d/args/%d"%(ci,ai),"value":false_form}]
                pr=subprocess.run(K+["patch","deploy",dep,"--type=json","-p",json.dumps(p)],capture_output=True,text=True)
                if pr.returncode==0:
                    subprocess.run(["logger","-t","vcfa-prelude-le-keeper","drift corrected: %s %s -> %s"%(dep,a,false_form)])
PY

# intent-server: NO leader-election ARG (internal leader election). TODO(needs-investigation):
# its disable knob is unknown -- likely a helm value / config / env not yet identified. Do NOT
# guess. Left untouched on purpose; if intent-server keeps flapping once the three above are
# fixed, investigate its LE config before adding it here.
if $K -n "$NS" get deploy intent-server >/dev/null 2>&1; then
  :   # intentional no-op -- see TODO above
fi
PRELUDE

  cat > "$TMP/vcfa-prelude-le-keeper.service" <<'PLS'
[Unit]
Description=VCFA: disable leader election on prelude (VCF Automation) replicas=1 services -- provider-login-outage fix (drift keeper)
After=network-online.target
[Service]
Type=oneshot
ExecStart=/usr/local/bin/vcfa-prelude-le-keeper.sh
PLS

  cat > "$TMP/vcfa-prelude-le-keeper.timer" <<'PLT'
[Unit]
Description=VCFA: run vcfa-prelude-le-keeper every 60s (drift watcher)
[Timer]
OnBootSec=2min
OnUnitActiveSec=60s
AccuracySec=10s
Unit=vcfa-prelude-le-keeper.service
[Install]
WantedBy=timers.target
PLT
}

install_vcfa_keepers() {  # <ip>
  local ip="$1"
  echo "=== VCFA Family A keepers (incl. prelude leader-election / login fix) -> ${ip} ==="
  build_vcfa_keeper_files
  TARFILES="vcfa-eg-mem-keeper.sh vcfa-eg-mem-keeper.service vcfa-eg-mem-keeper.timer vcfa-vip-watchdog.sh vcfa-vip-watchdog.service vcfa-envoy-gateway-patch.yaml vcfa-support-bundle-operator-patch.yaml vcfa-support-bundle-copier-patch.yaml vcfa-support-bundle-keeper.sh vcfa-support-bundle-keeper.service vcfa-support-bundle-keeper.timer vcfa-prelude-le-keeper.sh vcfa-prelude-le-keeper.service vcfa-prelude-le-keeper.timer"
  push_files_and_run "$ip" <<'INSTALL'
install -m 0755 /tmp/vcfa-eg-mem-keeper.sh  /usr/local/bin/vcfa-eg-mem-keeper.sh &&
install -m 0755 /tmp/vcfa-vip-watchdog.sh   /usr/local/bin/vcfa-vip-watchdog.sh &&
install -m 0755 /tmp/vcfa-support-bundle-keeper.sh /usr/local/bin/vcfa-support-bundle-keeper.sh &&
install -m 0755 /tmp/vcfa-prelude-le-keeper.sh /usr/local/bin/vcfa-prelude-le-keeper.sh &&
install -d /usr/local/etc &&
install -m 0644 /tmp/vcfa-envoy-gateway-patch.yaml /usr/local/etc/vcfa-envoy-gateway-patch.yaml &&
install -m 0644 /tmp/vcfa-support-bundle-operator-patch.yaml /usr/local/etc/vcfa-support-bundle-operator-patch.yaml &&
install -m 0644 /tmp/vcfa-support-bundle-copier-patch.yaml   /usr/local/etc/vcfa-support-bundle-copier-patch.yaml &&
install -m 0644 /tmp/vcfa-eg-mem-keeper.service /etc/systemd/system/vcfa-eg-mem-keeper.service &&
install -m 0644 /tmp/vcfa-eg-mem-keeper.timer   /etc/systemd/system/vcfa-eg-mem-keeper.timer &&
install -m 0644 /tmp/vcfa-vip-watchdog.service  /etc/systemd/system/vcfa-vip-watchdog.service &&
install -m 0644 /tmp/vcfa-support-bundle-keeper.service /etc/systemd/system/vcfa-support-bundle-keeper.service &&
install -m 0644 /tmp/vcfa-support-bundle-keeper.timer   /etc/systemd/system/vcfa-support-bundle-keeper.timer &&
install -m 0644 /tmp/vcfa-prelude-le-keeper.service /etc/systemd/system/vcfa-prelude-le-keeper.service &&
install -m 0644 /tmp/vcfa-prelude-le-keeper.timer   /etc/systemd/system/vcfa-prelude-le-keeper.timer &&
systemctl daemon-reload &&
systemctl enable --now vcfa-eg-mem-keeper.timer &&
systemctl enable --now vcfa-vip-watchdog.service &&
systemctl enable --now vcfa-support-bundle-keeper.timer &&
systemctl enable --now vcfa-prelude-le-keeper.timer &&
/usr/local/bin/vcfa-eg-mem-keeper.sh ;
/usr/local/bin/vcfa-support-bundle-keeper.sh ;
/usr/local/bin/vcfa-prelude-le-keeper.sh ;
echo "   VCFA keepers: eg-mem=$(systemctl is-active vcfa-eg-mem-keeper.timer 2>/dev/null) vip-watchdog=$(systemctl is-active vcfa-vip-watchdog.service 2>/dev/null) support-bundle=$(systemctl is-active vcfa-support-bundle-keeper.timer 2>/dev/null) prelude-le=$(systemctl is-active vcfa-prelude-le-keeper.timer 2>/dev/null)"
rm -f /tmp/vcfa-eg-mem-keeper.* /tmp/vcfa-vip-watchdog.* /tmp/vcfa-support-bundle-* /tmp/vcfa-envoy-gateway-* /tmp/vcfa-prelude-le-keeper.*
INSTALL
  echo ""
}

# ── --remove: uninstall all Family A keepers (leaves B/C objects patched) ─────
do_remove() {
  if [ "$DO_VSP" = 1 ] && reachable "$VSP_CP_IP"; then
    node_run "$VSP_CP_IP" <<'RM'
systemctl disable --now vsp-fleet-depot-keeper.timer 2>/dev/null || true
rm -f /etc/systemd/system/vsp-fleet-depot-keeper.service /etc/systemd/system/vsp-fleet-depot-keeper.timer /usr/local/bin/vsp-fleet-depot-keeper.sh /usr/local/etc/vsp-fleet-depot-patch.yaml /usr/local/etc/vsp-fleet-lcm-patch.yaml /usr/local/etc/vsp-envoy-gateway-patch.yaml /usr/local/etc/vsp-vidb-service-patch.yaml /usr/local/etc/vsp-sddcbuild-patch.yaml /usr/local/etc/vsp-sddcupgrade-patch.yaml /usr/local/etc/vsp-prometheus-patch.yaml /usr/local/etc/vsp-ksm-patch.yaml /usr/local/etc/vsp-node-exporter-patch.yaml
systemctl daemon-reload
echo "VSP keeper removed."
RM
  fi
  if [ "$DO_VCFA" = 1 ] && reachable "$AUTOA_IP"; then
    node_run "$AUTOA_IP" <<'RM'
systemctl disable --now vcfa-eg-mem-keeper.timer vcfa-vip-watchdog.service vcfa-support-bundle-keeper.timer vcfa-prelude-le-keeper.timer 2>/dev/null || true
rm -f /etc/systemd/system/vcfa-eg-mem-keeper.service /etc/systemd/system/vcfa-eg-mem-keeper.timer /etc/systemd/system/vcfa-vip-watchdog.service /etc/systemd/system/vcfa-support-bundle-keeper.service /etc/systemd/system/vcfa-support-bundle-keeper.timer /etc/systemd/system/vcfa-prelude-le-keeper.service /etc/systemd/system/vcfa-prelude-le-keeper.timer /usr/local/bin/vcfa-eg-mem-keeper.sh /usr/local/bin/vcfa-vip-watchdog.sh /usr/local/bin/vcfa-support-bundle-keeper.sh /usr/local/bin/vcfa-prelude-le-keeper.sh /usr/local/etc/vcfa-support-bundle-operator-patch.yaml /usr/local/etc/vcfa-support-bundle-copier-patch.yaml /usr/local/etc/vcfa-envoy-gateway-patch.yaml
systemctl daemon-reload
echo "VCFA keepers removed."
RM
  fi
  echo "(Deployments/VIPs/lease/etcd objects left as-is. Prelude services will revert to leader-election=on within a reconcile cycle now that the keeper is gone.)"
}

# ═══════════ VSP fleet-cluster kubectl actions (run on the VSP CP node) ═══════
# All of these target ONLY the VSP cluster, via its CP node's admin.conf.

do_right_size() {
  echo "=== VSP: right-size oversized vodap/ops-logs requests (big container only; rolling restart) ==="
  echo "  (idempotent -- 'set resources' to identical values is a no-op; app workloads may be RT-managed, re-run --status to confirm)"
  node_run "$VSP_CP_IP" <<'R'
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
setr(){ echo ">> $1/$3 [$4] -> cpu=$5 mem=$6"; $KC set resources "$1/$3" -n "$2" --containers="$4" --requests=cpu=$5,memory=$6 2>&1 | sed 's/^/   /'; }
setr statefulset vodap    chi-vcf-obs-vcf-obs-0-0  clickhouse    250m 1Gi
setr statefulset ops-logs log-store                opensearch    250m 8Gi
setr statefulset ops-logs log-processor            vcf-ops-logs  250m 2Gi
setr deploy vmsp-platform ops-logs-gateway                     envoy                                200m 256Mi
setr deploy vodap         vcf-obs-esx-collector-service        vcf-obs-esx-collector-service        200m 1536Mi
setr deploy vodap         vcf-obs-vc-collector-service         vcf-obs-vc-collector-service         200m 1Gi
setr deploy vodap         vcf-obs-data-query-service           vcf-obs-data-query-service           200m 1Gi
setr deploy vodap         vcf-obs-collector-controller-service vcf-obs-collector-controller-service 200m 1Gi
setr deploy vodap         vcf-obs-netops-collector-service     vcf-obs-netops-collector-service     200m 1Gi
R
  echo ""
}

do_reduce_ha() {
  echo "=== VSP: reduce leader-election controllers + coredns to 1 replica (idempotent) ==="
  node_run "$VSP_CP_IP" <<'H'
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
$KC scale deploy coredns -n kube-system --replicas=1 2>&1 | sed 's/^/  /'
for d in capi-controller-manager capi-ipam-in-cluster-controller-manager \
         capi-kubeadm-bootstrap-controller-manager capi-kubeadm-control-plane-controller-manager \
         capv-controller-manager ndc-controller-manager vmsp-identity; do
  $KC scale deploy "$d" -n vmsp-platform --replicas=1 2>&1 | sed 's/^/  /'
done
H
  echo ""
}

do_safe_evict() {
  echo "=== VSP: annotate vodap collector Deployments (hostPath log dir) safe-to-evict ==="
  node_run "$VSP_CP_IP" <<'E'
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
for dep in $($KC get deploy -n vodap --no-headers -o custom-columns=N:.metadata.name 2>/dev/null); do
  hp=$($KC get deploy "$dep" -n vodap -o jsonpath='{.spec.template.spec.volumes[*].hostPath.path}' 2>/dev/null)
  [ -n "$hp" ] && $KC patch deploy "$dep" -n vodap --type=merge \
    -p '{"spec":{"template":{"metadata":{"annotations":{"cluster-autoscaler.kubernetes.io/safe-to-evict":"true"}}}}}' >/dev/null 2>&1 \
    && echo "  annotated: $dep (hostPath=$hp)"
done
E
  echo ""
}

do_disable_capi_le() {
  echo "=== VSP: disable leader election on the 5 clusterctl CAPI/CAPV controllers (replicas=1) ==="
  echo "  (direct Deployment patch -- clusterctl-managed, so it STICKS; re-run after any 'clusterctl upgrade')"
  node_run "$VSP_CP_IP" <<'REMOTE'
python3 - <<'PY'
import subprocess,json
KC=["kubectl","--kubeconfig=/etc/kubernetes/admin.conf"]
deps=["capi-controller-manager","capi-ipam-in-cluster-controller-manager","capi-kubeadm-bootstrap-controller-manager","capi-kubeadm-control-plane-controller-manager","capv-controller-manager"]
for dep in deps:
    r=subprocess.run(KC+["get","deploy",dep,"-n","vmsp-platform","-o","json"],capture_output=True,text=True)
    if r.returncode!=0: print("  %s: NOT FOUND"%dep); continue
    d=json.loads(r.stdout); repl=d["spec"].get("replicas")
    if repl not in (1,None):
        print("  %s: replicas=%s (>1) -- SKIP (LE provides real failover here)"%(dep,repl)); continue
    done=False
    for ci,c in enumerate(d["spec"]["template"]["spec"]["containers"]):
        for ai,a in enumerate(c.get("args",[]) or []):
            if a in ("--leader-elect","--leader-elect=true"):
                p=[{"op":"replace","path":"/spec/template/spec/containers/%d/args/%d"%(ci,ai),"value":"--leader-elect=false"}]
                pr=subprocess.run(KC+["patch","deploy",dep,"-n","vmsp-platform","--type=json","-p",json.dumps(p)],capture_output=True,text=True)
                print("  %s: %s -> --leader-elect=false [%s]"%(dep,a,"OK" if pr.returncode==0 else pr.stderr.strip())); done=True
            elif a=="--leader-elect=false":
                print("  %s: already --leader-elect=false"%dep); done=True
    if not done: print("  %s: no --leader-elect arg (skipped)"%dep)
PY
REMOTE
  echo ""
}

do_pin() {
  [ -z "$AS_RT" ] && { echo "=== VSP: pin autoscaler -- SKIPPED (no cluster-autoscaler ReleaseTemplate discovered) ==="; return 0; }
  if [ "$AS_RT_REPLICAS" = "0" ]; then
    echo "=== VSP: autoscaler already pinned off (RT ${AS_RT} replicaCount=0) -- no change ==="
    return 0
  fi
  echo "=== VSP: PIN autoscaler off -- ReleaseTemplate ${AS_RT} replicaCount -> 0 (durable) ==="
  echo "  (one-time cost: vmsp-operator re-render briefly spikes etcd; freezes worker count where it is)"
  kc patch releasetemplate "$AS_RT" -n "$CLS_NS" --type=merge -p '{"spec":{"helm":{"values":{"replicaCount":0}}}}' >/dev/null
  echo "  RT patched; waiting ~75s for vmsp-operator -> HelmRelease -> Deployment"; sleep 75
  kc get deploy cluster-autoscaler-clusterapi-cluster-autoscaler -n "$CLS_NS" -o jsonpath='  autoscaler deploy now = {.spec.replicas}/{.status.readyReplicas}{"\n"}' || true
  [ -n "$MD" ] && kc get machinedeployment "$MD" -n "$CLS_NS" -o jsonpath='  MachineDeployment replicas (now pinned) = {.spec.replicas}{"\n"}' || true
  echo ""
}
do_unpin() {
  [ -z "$AS_RT" ] && { echo "=== VSP: unpin autoscaler -- SKIPPED (no cluster-autoscaler ReleaseTemplate discovered) ==="; return 0; }
  echo "=== VSP: UNPIN autoscaler -- ReleaseTemplate ${AS_RT} replicaCount -> 1 ==="
  kc patch releasetemplate "$AS_RT" -n "$CLS_NS" --type=merge -p '{"spec":{"helm":{"values":{"replicaCount":1}}}}' >/dev/null
  echo "  restored; vmsp-operator will bring the autoscaler back within ~1 min."
  echo ""
}

do_consolidate() {
  local node="$ARG"; [ -z "$node" ] && { echo "ERROR: --consolidate needs a node name"; exit 1; }
  echo "=== VSP: consolidate (cordon+drain+remove) ${node} ==="
  [ "$node" = "$CP_NODE" ] && { echo "REFUSING: that is the control-plane node."; exit 1; }
  if [ "$FORCE" != 1 ]; then
    local crit
    crit="$({ printf 'N=%q\n' "$node"; cat <<'C'; } | node_run "$VSP_CP_IP"
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
$KC get pods -A --field-selector spec.nodeName="$N" --no-headers 2>/dev/null | grep -E 'seaweedfs-master|vmsp-etcd-0|seaweedfs-filer' | awk '{print $2}'
C
)"
    [ -n "$crit" ] && { echo "REFUSING: $node hosts critical singleton(s): $crit"; echo "  (moving these = brief storage/etcd downtime). Re-run with --force to override."; exit 1; }
  fi
  { printf 'N=%q\n' "$node"; cat <<'D'; } | node_run "$VSP_CP_IP"
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
echo "  cordon $N";  $KC cordon "$N" 2>&1 | sed 's/^/    /'
echo "  drain $N (CSI PVCs reattach elsewhere)"
$KC drain "$N" --ignore-daemonsets --delete-emptydir-data --force --timeout=300s --skip-wait-for-delete-timeout=20 2>&1 | grep -vE 'Waited before sending' | tail -6 | sed 's/^/    /'
D
  echo "  removing drained node's Machine..."
  { printf 'N=%q\nMD=%q\nAS_RT=%q\nNS=%q\n' "$node" "$MD" "$AS_RT" "$CLS_NS"; cat <<'X'; } | node_run "$VSP_CP_IP"
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
rt=$([ -n "$AS_RT" ] && $KC get releasetemplate "$AS_RT" -n "$NS" -o jsonpath='{.spec.helm.values.replicaCount}' 2>/dev/null)
m=$($KC get machine -n "$NS" -o jsonpath="{range .items[?(@.status.nodeRef.name==\"$N\")]}{.metadata.name}{end}" 2>/dev/null)
[ -z "$m" ] && { echo "    could not find Machine for node $N"; exit 1; }
if [ "$rt" = "0" ]; then
  cur=$($KC get machinedeployment "$MD" -n "$NS" -o jsonpath='{.spec.replicas}')
  $KC annotate machine "$m" -n "$NS" cluster.x-k8s.io/delete-machine="" --overwrite >/dev/null 2>&1
  $KC scale machinedeployment "$MD" -n "$NS" --replicas=$((cur-1)) >/dev/null 2>&1
  echo "    autoscaler pinned off: marked $m for deletion, MD replicas ${cur}->$((cur-1))"
else
  echo "    autoscaler ACTIVE: leave removal to it (a cordoned empty node ages out ~10 min),"
  echo "    OR run --disable-autoscaler first for deterministic manual removal."
fi
X
  echo "=== consolidate issued; verify with --status in a few minutes ==="
}

# Remove ONE worker deterministically: cordon (stop churn landing) -> drain
# (CSI PVCs reattach elsewhere; safe-to-evict collectors move) -> delete its
# Machine + decrement the MachineDeployment (so nothing recreates it) -> wait
# until the node object is gone. Assumes the autoscaler is pinned off.
_remove_one_worker() {  # <node>
  local node="$1"
  echo "  ---- removing worker ${node} ----"
  { printf 'N=%q\nMD=%q\nNS=%q\n' "$node" "$MD" "$CLS_NS"; cat <<'R'; } | node_run "$VSP_CP_IP"
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=30s"
echo "    cordon $N"; $KC cordon "$N" 2>&1 | sed 's/^/      /'
echo "    drain $N"
$KC drain "$N" --ignore-daemonsets --delete-emptydir-data --force --timeout=300s --skip-wait-for-delete-timeout=20 2>&1 | grep -vE 'Waited before sending' | tail -8 | sed 's/^/      /'
# find the Machine backing this node -- retry: nodeRef can lag right after drain
m=""
for t in 1 2 3 4 5; do
  m=$($KC get machine -n "$NS" -o jsonpath="{range .items[?(@.status.nodeRef.name==\"$N\")]}{.metadata.name}{end}" 2>/dev/null)
  [ -n "$m" ] && break
  sleep 4
done
[ -z "$m" ] && { echo "    ERROR: no Machine found for node $N after retries -- not scaling MD."; exit 1; }
# IDEMPOTENCY GUARD: if this Machine is already marked/terminating (e.g. a prior
# round handled it but the node lingered), do NOT annotate+scale again -- that would
# double-decrement the MachineDeployment. Just report and let the wait proceed.
already=$($KC get machine "$m" -n "$NS" -o jsonpath='{.metadata.annotations.cluster\.x-k8s\.io/delete-machine}{.metadata.deletionTimestamp}' 2>/dev/null)
if [ -n "$already" ]; then echo "    Machine $m already marked for deletion -- NOT re-scaling (idempotent)."; exit 0; fi
cur=$($KC get machinedeployment "$MD" -n "$NS" -o jsonpath='{.spec.replicas}')
$KC annotate machine "$m" -n "$NS" cluster.x-k8s.io/delete-machine="" --overwrite >/dev/null 2>&1
$KC scale machinedeployment "$MD" -n "$NS" --replicas=$((cur-1)) >/dev/null 2>&1
echo "    marked Machine $m for deletion; MD replicas ${cur}->$((cur-1))"
R
  local j gone=""
  for j in $(seq 1 30); do
    if kc get node "$node" 2>&1 | grep -q 'NotFound\|not found'; then gone=1; echo "    node ${node} removed."; break; fi
    sleep 10
  done
  [ -n "$gone" ] || { echo "    (node ${node} still terminating after ~5m -- proceeding; verify later)"; }
  return 0
}

# Auto-consolidate the worker MachineDeployment down to CONSOLIDATE_TARGET.
# Self-selects the lowest-CPU-requested DRAINABLE worker each round (never the CP;
# never a node hosting a critical single-replica component); cordon-first so VCF's
# lifecycle-Job churn cannot keep resetting the autoscaler's unneeded-timer. Pins
# the autoscaler off first so the deterministic MD scale-down sticks.
do_auto_consolidate() {
  local target="${CONSOLIDATE_TARGET:-4}"
  echo "=== VSP: auto-consolidate workers -> ${target} (self-selecting; cordon-first; deterministic) ==="
  if [ -n "$AS_RT" ]; then
    local rt; rt="$(kc get releasetemplate "$AS_RT" -n "$CLS_NS" -o jsonpath='{.spec.helm.values.replicaCount}' 2>/dev/null)"
    if [ "$rt" != "0" ]; then echo "  autoscaler not pinned -- pinning off first (so MD scale-down is not contested)."; do_pin; fi
  else
    echo "  (autoscaler RT not discovered -- proceeding, but MD scale-down could be contested if the autoscaler is live)"
  fi
  local iter
  for iter in 1 2 3 4 5 6 7 8; do
    local sel count cand
    sel="$({ cat <<'Q'; } | node_run "$VSP_CP_IP" 2>/dev/null || true
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s"
W=$($KC get nodes -l '!node-role.kubernetes.io/control-plane' --no-headers 2>/dev/null | awk '{print $1}')
echo "COUNT=$(printf '%s\n' "$W" | grep -c .)"
best=""; bestreq=99999999
for n in $W; do
  if $KC get pods -A --field-selector spec.nodeName="$n" --no-headers 2>/dev/null | grep -qE 'seaweedfs-master|vmsp-etcd-0|seaweedfs-filer'; then continue; fi
  req=$($KC describe node "$n" 2>/dev/null | awk '/Allocated resources/{f=1} f&&/^  cpu/{gsub(/[^0-9]/,"",$2);print $2;exit}'); [ -z "$req" ] && req=0
  if [ "$req" -lt "$bestreq" ]; then bestreq="$req"; best="$n"; fi
done
echo "CANDIDATE=$best"
Q
)"
    count="$(echo "$sel" | sed -n 's/^COUNT=//p')"
    cand="$(echo "$sel" | sed -n 's/^CANDIDATE=//p')"
    echo "  [round $iter] workers=${count:-?} target=${target} next-candidate=${cand:-none}"
    [ -z "$count" ] && { echo "  ERROR: could not read worker count via the CP -- aborting consolidation."; return 1; }
    if [ "$count" -le "$target" ]; then echo "  workers=${count} <= target=${target} -- consolidation complete."; return 0; fi
    if [ -z "$cand" ]; then echo "  no drainable candidate left (remaining ${count} workers host critical singletons) -- stopping."; return 0; fi
    _remove_one_worker "$cand" || { echo "  removal did not complete -- stopping consolidation."; return 1; }
  done
  echo "  reached the 8-round safety cap -- stopping."
}

# ═══════════ VSP node resize (govc power-cycle, pause-guarded) ════════════════
# govc runs on the manager (survives a CP power-off). Guard verifies BOTH cpu+mem
# before any power-on. CP readiness uses the FIXED path (see header / task #3).

vm_field() {  # <vmname> <CPU:|Memory:|Power>
  case "$2" in
    CPU:)    govc "vm.info ${VM_FOLDER}/$1" | awk -F': +' '/CPU:/{print $2}'    | grep -o '^[0-9]*';;
    Memory:) govc "vm.info ${VM_FOLDER}/$1" | awk -F': +' '/Memory:/{print $2}' | grep -o '^[0-9]*';;
    Power)   govc "vm.info ${VM_FOLDER}/$1" | awk -F': +' '/Power state/{print $2}';;
  esac
}

resize_vm() {  # <vmname> <cpu> <memMiB or ->  ; returns 3 on guard failure (VM left OFF)
  local vm="$1" cpu="$2" mem="$3" st="" i
  echo ">> ${vm}: graceful shutdown"
  govc "vm.power -s ${VM_FOLDER}/${vm}" >/dev/null 2>&1 || echo "   (soft shutdown failed; will force)"
  for i in $(seq 1 30); do st="$(vm_field "$vm" Power)"; [ "$st" = poweredOff ] && break; sleep 5; done
  [ "$st" = poweredOff ] || { echo "   forcing power off"; govc "vm.power -off -force ${VM_FOLDER}/${vm}" >/dev/null; sleep 5; }
  if [ "$mem" = "-" ]; then govc "vm.change -vm ${VM_FOLDER}/${vm} -c ${cpu}" >/dev/null
  else                     govc "vm.change -vm ${VM_FOLDER}/${vm} -c ${cpu} -m ${mem}" >/dev/null; fi
  local gc gm want_mem; gc="$(vm_field "$vm" CPU:)"; gm="$(vm_field "$vm" Memory:)"
  want_mem="$mem"; [ "$mem" = "-" ] && want_mem="$gm"
  echo "   guard: cpu=${gc} mem=${gm} (want cpu=${cpu} mem=${want_mem})"
  if [ "$gc" != "$cpu" ] || [ "$gm" != "$want_mem" ]; then
    echo "   !!! GUARD FAILED for ${vm} -- NOT powering on. Investigate."; return 3
  fi
  echo "   guard passed -- powering on"; govc "vm.power -on ${VM_FOLDER}/${vm}" >/dev/null
}

# THE FIX: health-probe the CP via its REAL node IP against the LOCAL apiserver
# (https://127.0.0.1:6443), independent of the kube-vip VIP. Verifies identity
# (hostname==CP node + etcd/apiserver static manifests present) and Ready at the
# new vCPU capacity. Any SSH/connection failure => "not up yet", never "down".
_cp_health_probe() {  # <realip> <cpnode-hostname> <want_cpu>
  local rip="$1" vm="$2" wcpu="$3" out
  out="$({ printf 'VM=%q\nWCPU=%q\n' "$vm" "$wcpu"; cat <<'PROBE'; } | node_run "$rip" 2>/dev/null || true
if command -v curl >/dev/null 2>&1; then
  curl -sk --max-time 10 https://127.0.0.1:6443/readyz >/dev/null 2>&1 || { echo "APISERVER_DOWN"; exit 0; }
fi
H="$(hostname)"
[ "$H" = "$VM" ] || { echo "IDENTITY_MISMATCH host=$H want=$VM"; exit 0; }
{ [ -f /etc/kubernetes/manifests/etcd.yaml ] && [ -f /etc/kubernetes/manifests/kube-apiserver.yaml ]; } || { echo "STATIC_PODS_MISSING"; exit 0; }
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf --server=https://127.0.0.1:6443 --insecure-skip-tls-verify=true --request-timeout=10s"
rd="$($KC get node "$H" -o jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status}{end}' 2>/dev/null)"
cpu="$($KC get node "$H" -o jsonpath='{.status.capacity.cpu}' 2>/dev/null)"
[ "$rd" = "True" ] || { echo "NOT_READY ready=${rd:-none}"; exit 0; }
[ "$cpu" = "$WCPU" ] || { echo "CAP_CPU_MISMATCH cpu=${cpu:-none} want=$WCPU"; exit 0; }
echo "OK ready=$rd cpu=$cpu"
PROBE
)"
  echo "    probe@${rip}: ${out:-<no response - node/SSH still down, retrying>}"
  case "$out" in *OK*) return 0;; *) return 1;; esac
}

wait_cp_ready() {  # <cpnode> <realip> <want_cpu>
  local vm="$1" rip="$2" wcpu="$3" gip="" deadline
  deadline=$(( $(date +%s) + CP_READY_WINDOW ))
  echo "  Waiting for guest boot via govc guest IP (VM ${vm})..."
  while [ "$(date +%s)" -lt "$deadline" ]; do
    gip="$(govc "vm.ip -wait=45s ${VM_FOLDER}/${vm}" 2>/dev/null | tr -d '[:space:]' || true)"
    [ -n "$gip" ] && { echo "  guest reports IP ${gip} (booting)."; break; }
    echo "  ...guest not reporting an IP yet; retrying"
  done
  echo "  Polling REAL node IP ${rip} against its LOCAL apiserver https://127.0.0.1:6443 (NOT the kube-vip VIP)."
  echo "  Window ${CP_READY_WINDOW}s. 'No route to host'/connection-refused/TLS errors are EXPECTED during reboot and are retried."
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if _cp_health_probe "$rip" "$vm" "$wcpu"; then
      echo "  CP CONFIRMED healthy at ${wcpu} vCPU (real IP + local apiserver + identity + Ready verified)."
      return 0
    fi
    sleep 15
  done
  return 1
}

do_cp_mem_watch() {  # <realip>
  local rip="$1"
  echo "  CP memory watch (single-node etcd OOM-risk dimension):"
  node_run "$rip" <<'M' || true
for r in 1 2 3; do
  free -m | awk '/Mem:/{printf "    Mem total=%sMB used=%sMB AVAILABLE=%sMB\n",$2,$3,$7}'
  a=$(free -m | awk '/Mem:/{print $7}'); [ "${a:-9999}" -lt 1500 ] && echo "    *** WARNING: available memory < 1.5GB -- consider a higher CP memory ***"
  ps -eo rss,comm --sort=-rss | awk 'NR>=2 && /etcd|kube-apiserver/{printf "    rss=%.0fMB %s\n",$1/1024,$2}' | head -2
  [ $r -lt 3 ] && sleep 30
done
M
}

# ═══════════ Physical/nested-host layer: AMD Zen4/5 entropy workaround (govc; NEVER reboots) ═════
# AMD Zen4/5 (EPYC 9004/9005) CPUs generate randomness via RDSEED extremely slowly (~50x slower
# than Zen3, higher failure rate), so the ESXi VMkernel repeatedly burns host CPU refreshing
# entropy -- logged as "NRandomHwrng: NNN: Out of entropy, refreshing" in vmkernel.log, worse with
# more 12-24 vCPU VMs. Fix: esxcli system settings kernel set -s entropySources -v 2 (RDSEED->
# RDRAND). Confirmed present via this exact log signature on this lab's nested ESXi hosts
# (esx-01a/02a/03a, all AMD EPYC 9655P). Ref:
# https://williamlam.com/2026/04/quick-tip-high-cpu-utilization-on-esx-due-to-slow-entropy-from-amd-zen-4-cpus.html
#
# INTENTIONALLY DOES NOT REBOOT: the Configured value only takes effect after a host reboot, which
# is host-wide (affects every VM on that host, not just this lab) and disruptive enough that it
# must stay a deliberate, separately-scheduled human action -- never bundled into an idempotent,
# unattended remediation pass. This function only sets+verifies the Configured value.
do_entropy_fix() {
  if ! govc_ok; then
    echo "=== entropy-fix: FAILED -- govc is still unavailable on the manager after attempting to auto-stage it"
    echo "    (${GOVC_ENV} / ${GOVC} not usable, or vCenter auth failed). This is a REQUIRED step, not a"
    echo "    skippable one -- fix manually: check manager egress (curl -sI https://github.com), or set"
    echo "    GOVC_URL/GOVC_USERNAME/GOVC_PASSWORD if this pod's nested vCenter creds differ from the"
    echo "    lab-standard pattern, then re-run --entropy-fix. ==="
    return 1
  fi
  local hosts h fqdn cpumodel cur new rc=0
  # Search from absolute root, not a datacenter path derived from VM_FOLDER -- VM_FOLDER's
  # datacenter component is a per-lab default (matches 10.138.150.5's "dc-mgmt-a") that does NOT
  # generalize: confirmed live on 10.138.150.21, whose actual datacenter is named "dc-a". Root-
  # relative `find` works regardless of the datacenter's name and however many datacenters exist.
  hosts="$(govc find / -type h 2>/dev/null)"
  if [ -z "$hosts" ]; then
    echo "=== entropy-fix: SKIPPED -- no ESXi hosts discovered via govc find / -type h ==="
    return 0
  fi
  echo "=== entropy-fix: esxcli entropySources on every nested ESXi host (target=${ENTROPY_TARGET}, RDRAND) ==="
  echo "    host list is govc-derived (authoritative), not guessed. config-only -- NEVER reboots; Runtime"
  echo "    stays at the old value (fix is NOT live) until each host is rebooted manually."
  local nhosts=0
  while IFS= read -r h; do
    [ -z "$h" ] && continue
    nhosts=$((nhosts+1))
    fqdn="$(basename "$h")"
    # Only explicitly exclude clear non-AMD (Intel) hardware. Don't require a full AMD EPYC 9xxx/8004
    # match here -- the exact "Processor type" string format varies across vCenter/ESXi builds (seen
    # firsthand: newer ESXi's esxcli/proc surfaces no clean model string, only bare Family/Brand), so
    # treating "didn't parse as expected" as "not applicable" would silently skip real Zen4/5 hosts.
    # entropySources=2 (RDRAND) is a harmless, well-understood config value on any AMD generation even
    # when this fix isn't strictly needed -- the asymmetric risk favors applying over false-skipping.
    cpumodel="$(govc host.info -host.dns="$fqdn" 2>/dev/null | grep -m1 'Processor type:' | sed 's/^ *Processor type: *//' || true)"
    if echo "$cpumodel" | grep -qi 'intel'; then
      echo "  ${fqdn}: processor '${cpumodel:-unknown}' is Intel -- entropySources fix is AMD Zen4/5-specific, not applicable, skipping"
      continue
    fi
    cur="$(govc host.esxcli -host.dns="$fqdn" system settings kernel list -o entropySources 2>/dev/null | awk 'NR==3{print $3}')"
    if [ -z "$cur" ]; then
      echo "  ${fqdn}: could not read entropySources (host unreachable via govc?) -- skipping"
      rc=1
      continue
    fi
    if [ "$cur" = "$ENTROPY_TARGET" ]; then
      echo "  ${fqdn}: already Configured=${ENTROPY_TARGET} -- no change"
      continue
    fi
    echo "  ${fqdn}: Configured=${cur} -> setting ${ENTROPY_TARGET}..."
    govc host.esxcli -host.dns="$fqdn" system settings kernel set -s entropySources -v "$ENTROPY_TARGET" >/dev/null 2>&1
    new="$(govc host.esxcli -host.dns="$fqdn" system settings kernel list -o entropySources 2>/dev/null | awk 'NR==3{print $3}')"
    if [ "$new" = "$ENTROPY_TARGET" ]; then
      echo "  ${fqdn}: Configured now ${new} (confirmed) -- REBOOT REQUIRED before this takes effect (not done by this script)"
    else
      echo "  ${fqdn}: WARNING -- set attempted but Configured reads '${new}' (expected ${ENTROPY_TARGET}) -- verify manually"
      rc=1
    fi
  done <<EOF
$hosts
EOF
  echo "    (enumerated ${nhosts} host(s) via govc find / -type h)"
  return $rc
}

do_cp_resize() {
  if ! govc_ok; then
    echo "=== CP resize: SKIPPED -- govc is unavailable on the manager (${GOVC_ENV} / ${GOVC} not usable, or vCenter auth failed)."
    echo "    Node resize needs govc; refusing to pause the cluster for a resize we can't perform. Stage govc on the manager to enable it. ==="
    return 0
  fi
  if [ -z "$CP_NODE" ] || [ -z "$CP_REAL_IP" ]; then
    echo "=== CP resize: SKIPPED (VSP discovery incomplete -- refusing to guess the CP VM/IP) ==="; return 0
  fi
  local cpu mem
  if [ -n "$ARG" ]; then
    if [[ "$ARG" == */* ]]; then cpu="${ARG%%/*}"; mem="${ARG##*/}"; else cpu="$ARG"; mem="$CP_TMPL_MEM"; fi
  else cpu="$CP_TMPL_CPU"; mem="$CP_TMPL_MEM"; fi
  if [ -z "$cpu" ]; then echo "=== CP resize: SKIPPED (no target vCPU discovered/given) ==="; return 0; fi
  [ -z "$mem" ] && mem="-"
  local gc gm want_mem; gc="$(vm_field "$CP_NODE" CPU:)"; gm="$(vm_field "$CP_NODE" Memory:)"
  want_mem="$mem"; [ "$mem" = "-" ] && want_mem="$gm"
  if [ "$gc" = "$cpu" ] && [ "$gm" = "$want_mem" ]; then
    echo "=== CP resize: ${CP_NODE} already ${cpu} vCPU / ${gm} MiB -- no drift, skipping power-cycle ==="; return 0
  fi
  echo "=== CP resize: ${CP_NODE} ${gc}vCPU/${gm}MiB -> ${cpu}vCPU/${want_mem}MiB (pause-guarded; FIXED CP-readiness) ==="
  # never assume: confirm the VM we are about to change maps to the CP guest we discovered
  local gip; gip="$(govc "vm.ip -wait=30s ${VM_FOLDER}/${CP_NODE}" 2>/dev/null | tr -d '[:space:]' || true)"
  if [ -n "$gip" ] && [ -n "$CP_REAL_IP" ] && [ "$gip" != "$CP_REAL_IP" ]; then
    echo "  NOTE: govc guest IP (${gip}) != discovered CP InternalIP (${CP_REAL_IP}); readiness will poll the discovered node InternalIP (stable across an in-place resize)."
  fi
  do_pause || { echo "  ERROR: could not pause the cluster -- ABORTING (refuse to power-cycle a CAPI node while unpaused)."; return 1; }
  if ! resize_vm "$CP_NODE" "$cpu" "$mem"; then
    echo "  GUARD FAILED -- CP VM is powered OFF and was NOT powered on (vm.change produced an unexpected result)."
    echo "  The control plane is DOWN. Leaving Cluster PAUSED for inspection; power it on manually at the correct sizes."
    CP_VERIFIED_DOWN=1; return 3
  fi
  if wait_cp_ready "$CP_NODE" "$CP_REAL_IP" "$cpu"; then
    do_cp_mem_watch "$CP_REAL_IP"
    echo "=== CP resize OK -- cluster will be UNPAUSED by the exit-safety ==="; return 0
  fi
  echo "  !!! CP real node IP ${CP_REAL_IP} was UNREACHABLE/UNHEALTHY for the ENTIRE ${CP_READY_WINDOW}s window."
  echo "  Concluding the CP is genuinely DOWN. Leaving Cluster PAUSED (correct + safe). Investigate before unpausing."
  CP_VERIFIED_DOWN=1; return 1
}

wait_worker_ready() {  # <node> <want_cpu>   (cluster stays UP -> poll via CP apiserver)
  local node="$1" want="$2" i out
  for i in $(seq 1 48); do
    out="$(kc get node "$node" -o jsonpath='{range .status.conditions[?(@.type=="Ready")]}{.status}{end} {.status.capacity.cpu}' 2>/dev/null || true)"
    echo "    [$i] $node ready/cpu: ${out:-<no api>}"
    [ "$out" = "True $want" ] && { echo "    READY_OK"; return 0; }
    sleep 10
  done
  return 1
}

do_worker_resize() {
  if ! govc_ok; then
    echo "=== worker resize: SKIPPED -- govc unavailable on the manager (node resize needs vCenter; not pausing for a resize we can't do). ==="
    return 0
  fi
  local cpu="${W_TMPL_CPU}"; [ -n "$ARG" ] && cpu="$ARG"
  if [ -z "$cpu" ]; then echo "=== worker resize: SKIPPED (no target vCPU discovered/given) ==="; return 0; fi
  echo "=== VSP worker resize -> ${cpu} vCPU each (memory left at template ${W_TMPL_MEM}MiB), ONE AT A TIME ==="
  local workers
  workers="$(kc get nodes -o custom-columns='N:.metadata.name,CP:.metadata.labels.node-role\.kubernetes\.io/control-plane,C:.status.capacity.cpu' --no-headers 2>/dev/null | awk -v t="$cpu" '$2=="<none>" && $3!=t {print $1}')"
  if [ -z "$workers" ]; then echo "  No drifted workers -- all already at ${cpu} vCPU. Nothing to do."; return 0; fi
  echo "  Drifted workers: $workers"
  do_pause || { echo "  ERROR: could not pause -- ABORTING worker resize."; return 1; }
  local w
  for w in $workers; do
    echo "  ---- $w ----"
    if ! resize_vm "$w" "$cpu" "-"; then
      echo "  GUARD FAILED on $w -- worker left powered OFF. CP is healthy, so the exit-safety WILL unpause."
      echo "  WARNING: with the cluster unpaused, CAPI/MHC may remediate the powered-off worker. Investigate $w."
      return 3
    fi
    if ! wait_worker_ready "$w" "$cpu"; then
      echo "  $w did NOT reach Ready@${cpu} in the window. CP is healthy, so the exit-safety WILL unpause."
      echo "  WARNING: investigate $w; CAPI/MHC may remediate it once unpaused."
      return 1
    fi
  done
  echo "=== worker resize complete -- cluster will be UNPAUSED by the exit-safety ==="
}

do_kcp_patch() {  # <ip> <role>
  local ip="$1" role="$2" info name ns
  echo "=== ${role} (${ip}) -- KubeadmControlPlane lease patch (PRINT ONLY, never applied) ==="
  reachable "$ip" || { echo "  cannot reach ${ip}"; return 2; }
  info="$(node_run "$ip" <<'K'
kubectl --kubeconfig=/etc/kubernetes/admin.conf --request-timeout=20s get kubeadmcontrolplane -A --no-headers 2>&1
K
)"
  echo "$info"
  name="$(echo "$info" | awk '{print $2; exit}')"; ns="$(echo "$info" | awk '{print $1; exit}')"
  echo ""
  echo "Applying the following triggers a KCP rollout that REPLACES the CP node (brief outage on a single-CP cluster). NOT applied here:"
  cat <<PATCH
kubectl --kubeconfig=/etc/kubernetes/admin.conf patch kubeadmcontrolplane ${name:-<name>} -n ${ns:-vmsp-platform} --type merge -p '
spec:
  kubeadmConfigSpec:
    clusterConfiguration:
      controllerManager:
        extraArgs:
          leader-elect-lease-duration: "${LEASE_DURATION}"
          leader-elect-renew-deadline: "${RENEW_DEADLINE}"
          leader-elect-retry-period: "${RETRY_PERIOD}"
      scheduler:
        extraArgs:
          leader-elect-lease-duration: "${LEASE_DURATION}"
          leader-elect-renew-deadline: "${RENEW_DEADLINE}"
          leader-elect-retry-period: "${RETRY_PERIOD}"
'
PATCH
  echo ""
}

# ═══════════ STATUS (read-only, both targets) ════════════════════════════════
do_status() {
  reach_manager
  if [ "$DO_VSP" = 1 ] && reachable "$VSP_CP_IP"; then
    discover_vsp || true
    echo ""
    echo "==================== VSP fleet cluster (${CLS_NAME}) ===================="
    printf '  paused=%s\n' "${PAUSED_STATE:-false}"
    { printf 'MD=%q\nASRT=%q\nNS=%q\n' "${MD:-vsp-01a-default-l746w}" "${AS_RT:-cluster-autoscaler-9.5.1-3}" "$CLS_NS"; cat <<'S'; } | node_run "$VSP_CP_IP"
KC="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
rt=$($KC get releasetemplate "$ASRT" -n "$NS" -o jsonpath='{.spec.helm.values.replicaCount}' 2>/dev/null)
dep=$($KC get deploy cluster-autoscaler-clusterapi-cluster-autoscaler -n "$NS" -o jsonpath='{.spec.replicas}/{.status.readyReplicas}' 2>/dev/null)
echo "  autoscaler: RT.replicaCount=[$rt]  deploy=${dep}  ($([ "$rt" = "0" ] && echo PINNED-OFF || echo active/templated))"
echo "  MachineDeployment replicas = $($KC get machinedeployment "$MD" -n "$NS" -o jsonpath='{.spec.replicas}' 2>/dev/null)"
echo "--- nodes: role / capacity vCPU / memory / unschedulable ---"
$KC get nodes -o custom-columns='NAME:.metadata.name,ROLE:.metadata.labels.node-role\.kubernetes\.io/control-plane,CPU:.status.capacity.cpu,MEM:.status.capacity.memory,SCHED:.spec.unschedulable' --no-headers 2>/dev/null
echo "--- template-declared sizes (drift = live != these) ---"
$KC get vspheremachinetemplate -n "$NS" -o custom-columns='NAME:.metadata.name,CPU:.spec.template.spec.numCPUs,MEMMiB:.spec.template.spec.memoryMiB' --no-headers 2>/dev/null | grep -E 'vsp-01a-d6s9z|vsp-01a-default|d6s9z|default' || $KC get vspheremachinetemplate -n "$NS" -o custom-columns='NAME:.metadata.name,CPU:.spec.template.spec.numCPUs,MEMMiB:.spec.template.spec.memoryMiB' --no-headers 2>/dev/null
echo "--- heaviest pod CPU requests (right-size candidates) ---"
$KC get pods -A -o custom-columns='NS:.metadata.namespace,POD:.metadata.name,CPUREQ:.spec.containers[*].resources.requests.cpu' --no-headers 2>/dev/null | grep -E 'chi-vcf-obs-vcf-obs-0-0-0|log-store-0|log-processor-0|ops-logs-gateway|vcf-obs-(esx|vc|data-query|collector-controller|netops)' | head
echo "--- HA (replicas>1) leader-election controllers ---"
$KC get deploy -A -o custom-columns='NS:.metadata.namespace,NAME:.metadata.name,REPL:.spec.replicas' --no-headers 2>/dev/null | awk '$3>1'
echo "--- crash cascade: CAPI/CAPV leader-election state (want =false at replicas=1) ---"
for d in capi-controller-manager capi-ipam-in-cluster-controller-manager capi-kubeadm-bootstrap-controller-manager capi-kubeadm-control-plane-controller-manager capv-controller-manager; do
  le=$($KC get deploy "$d" -n "$NS" -o jsonpath="{range .spec.template.spec.containers[*].args[*]}{@}{'\n'}{end}" 2>/dev/null | grep -m1 leader-elect=)
  [ -z "$le" ] && le="--leader-elect(=true implied)"
  echo "    $d : $le"
done
echo "--- CrashLoopBackOff pods now ---"
$KC get pods -A --no-headers 2>/dev/null | awk '/CrashLoop/{print "    "$1"/"$2" restarts="$5}'
$KC get pods -A --no-headers 2>/dev/null | awk '/CrashLoop/{c++} END{print "    total CrashLoopBackOff="c+0}'
S
    echo ""
    echo "-- VSP CP: Family A keeper + Family B (lease/etcd/kube-vip) + Family C (read-only) --"
    printf 'echo "  keeper timer: $(systemctl --no-pager is-active vsp-fleet-depot-keeper.timer 2>/dev/null)"\n' | node_run "$VSP_CP_IP" || true
    build_remote_scripts
    famb_run "$VSP_CP_IP" "VSP fleet CP" status
    famb_run "$VSP_CP_IP" "VSP fleet CP" kyverno-status
    famb_run "$VSP_CP_IP" "VSP fleet CP" envoy-status
  fi
  if [ "$DO_VCFA" = 1 ] && reachable "$AUTOA_IP"; then
    echo ""
    echo "==================== VCFA (auto-a ${AUTOA_IP}) ===================="
    node_run "$AUTOA_IP" <<'V' || true
K="kubectl --kubeconfig=/etc/kubernetes/admin.conf"
echo "-- Family A keepers --"
systemctl --no-pager is-active vcfa-eg-mem-keeper.timer vcfa-vip-watchdog.service vcfa-support-bundle-keeper.timer vcfa-prelude-le-keeper.timer 2>/dev/null || true
echo "-- prelude (VCF Automation) leader-election state (the provider-login fix; want disabled at replicas=1) --"
for d in resource-manager-server vcfa-service-manager encryption-manager intent-server; do
  repl=$($K -n prelude get deploy "$d" -o jsonpath='{.spec.replicas}' 2>/dev/null)
  env=$($K -n prelude get deploy "$d" -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="ENABLE_LEADER_ELECTION")].value}' 2>/dev/null)
  args=$($K -n prelude get deploy "$d" -o jsonpath="{range .spec.template.spec.containers[*].args[*]}{@}{' '}{end}" 2>/dev/null | tr ' ' '\n' | grep -iE 'leader' | tr '\n' ' ')
  echo "    $d : replicas=${repl:-?} ENABLE_LEADER_ELECTION=${env:-unset} args[leader]=${args:-none}"
done
echo "-- prelude CrashLoopBackOff / recent restarts --"
$K -n prelude get pods --no-headers 2>/dev/null | awk '{print "    "$1" "$3" restarts="$4}' | grep -E 'resource-manager|vcfa-service-manager|encryption-manager|intent-server' || true
V
    build_remote_scripts
    famb_run "$AUTOA_IP" "VCFA (auto-a)" status
    famb_run "$AUTOA_IP" "VCFA (auto-a)" kyverno-status
    famb_run "$AUTOA_IP" "VCFA (auto-a)" envoy-status
  fi
}

# ═══════════ FULL (no-arg) ORCHESTRATION ═════════════════════════════════════
run_full() {
  echo "########## remediate-lab.sh -- FULL, IDEMPOTENT remediation ##########"
  echo "(each step runs only if discovery shows it is needed; preflight health-gates every node)"
  reach_manager

  # Preflight BOTH targets symmetrically (role + health), then discover VSP objects.
  if [ "$DO_VSP" = 1 ]; then
    if node_preflight "$VSP_CP_IP" "VSP fleet CP"; then VSP_OK=1; discover_vsp || true; else VSP_OK=0; fi
  fi
  if [ "$DO_VCFA" = 1 ]; then
    if node_preflight "$AUTOA_IP" "VCFA (auto-a)"; then VCFA_OK=1; else VCFA_OK=0; fi
  fi

  # PHASE 0 -- physical/nested-host layer (govc; config-only, NEVER reboots -- see do_entropy_fix)
  echo ""; echo "########## PHASE 0: nested-host entropySources check (AMD Zen4/5 RDSEED workaround) ##########"
  do_entropy_fix || echo "  (entropy-fix reported an issue -- see messages above)"

  build_remote_scripts

  # PHASE 1 -- VSP footprint & stability (kubectl, non-disruptive)
  if [ "$DO_VSP" = 1 ] && [ "$VSP_OK" = 1 ]; then
    echo ""; echo "########## PHASE 1: VSP footprint & stability (kubectl, non-disruptive) ##########"
    do_right_size       || echo "  (right-size reported an error -- continuing)"
    do_safe_evict       || echo "  (safe-to-evict reported an error -- continuing)"
    do_reduce_ha        || echo "  (reduce-ha reported an error -- continuing)"
    do_disable_capi_le  || echo "  (disable-capi-le reported an error -- continuing)"
    # (autoscaler pin happens in Phase 3, AFTER consolidation reaches the target)
  fi

  # PHASE 2 -- per-node Families A/B/C (static-manifest edits + drift keepers)
  echo ""; echo "########## PHASE 2: per-node Families A/B/C ##########"
  if [ "$DO_VSP" = 1 ] && [ "$VSP_OK" = 1 ]; then
    install_vsp_keeper "$VSP_CP_IP"                         || echo "  (VSP keeper install error)"
    famb_run "$VSP_CP_IP" "VSP fleet CP" apply-lease        || true
    famb_run "$VSP_CP_IP" "VSP fleet CP" etcd-compaction    || true
    famb_run "$VSP_CP_IP" "VSP fleet CP" kube-vip-apply     || true
    famb_run "$VSP_CP_IP" "VSP fleet CP" kyverno-resync-relax || true
    famb_run "$VSP_CP_IP" "VSP fleet CP" envoy-gateway-fix    || true
  fi
  if [ "$DO_VCFA" = 1 ] && [ "$VCFA_OK" = 1 ]; then
    install_vcfa_keepers "$AUTOA_IP"                        || echo "  (VCFA keeper install error)"
    famb_run "$AUTOA_IP" "VCFA (auto-a)" apply-lease        || true
    famb_run "$AUTOA_IP" "VCFA (auto-a)" etcd-compaction    || true
    famb_run "$AUTOA_IP" "VCFA (auto-a)" kube-vip-apply     || true
    famb_run "$AUTOA_IP" "VCFA (auto-a)" kyverno-resync-relax || true
    famb_run "$AUTOA_IP" "VCFA (auto-a)" envoy-gateway-fix    || true
    # VCFA auto-a CPU-storm mitigation (companion script), 'apply' subset. Still excludes the
    # two opt-in levers ('logging' = cell restart, 'disable-le'), which stay behind
    # --vcfa-stabilize. NOTE: 'apply' is no longer purely non-disruptive -- two of its levers
    # roll pods ONCE each (observed zero-downtime, maxSurge brings the new pod up first):
    #   * harden-gateway -> re-renders the 2 data-plane Envoy proxies (fixes the 5-6 min
    #     ":443 Unable to connect" outages)
    #   * harden-uitier  -> rolls the 7 user-facing UI deployments out of BestEffort (halves UI
    #     latency; PARTIAL -- a node-wide transient still leaves an occasional ~10 s tail).
    #     A brief transient 500 was seen during each rollout.
    materialise_companion
    if [ -f "$VCFA_MIT" ]; then
      echo "-- VCFA storm mitigation: footprint + prelude probe-relax + kube-vip (validity+preserve)"
      echo "   + data-plane Envoy gateway hardening + UI-tier QoS  [the last two roll pods once] --"
      node_run_file "$AUTOA_IP" "$VCFA_MIT" apply || echo "  (vcfa-stabilize apply error -- continuing)"
    else
      echo "  (companion vcfa-storm-mitigation.sh not found at \$VCFA_MIT -- skipping VCFA storm mitigation)"
    fi
  fi

  # PHASE 3 -- VSP node-size drift (disruptive power-cycle, pause-guarded)
  if [ "$DO_VSP" = 1 ] && [ "$VSP_OK" = 1 ]; then
    echo ""; echo "########## PHASE 3: VSP worker consolidation + node-size drift ##########"
    # 3a. consolidate the worker COUNT to target (pins the autoscaler off as part of it)
    do_auto_consolidate || echo "  (auto-consolidate returned non-zero -- see messages above)"
    # 3b. node-SIZE drift (govc/vCenter needed; skipped cleanly if govc is unavailable)
    do_cp_resize || echo "  (CP resize returned non-zero -- see messages above)"
    if [ "$CP_VERIFIED_DOWN" = 1 ]; then
      echo "  Skipping worker resize -- CP is verifiably down (cluster intentionally left paused)."
    else
      do_worker_resize || echo "  (worker resize returned non-zero -- see messages above)"
    fi
  fi

  # POST-remediation health RE-VERIFY (both targets, symmetric with preflight)
  echo ""; echo "########## POST-REMEDIATION HEALTH RE-VERIFY ##########"
  echo "  (settling ~45s so self-inflicted etcd-defrag/Family-C cascade blips clear before re-verify)"
  sleep 45
  local vsp_actually_ready=1
  if [ "$DO_VSP" = 1 ]; then
    if [ "$CP_VERIFIED_DOWN" = 1 ]; then echo "  VSP CP: intentionally left down/paused -- skipping re-verify."
    elif node_preflight "$VSP_CP_IP" "VSP fleet CP (post)"; then
      verify_vsp_ready "$VSP_CP_IP" || vsp_actually_ready=0
    else
      echo "  (VSP CP not healthy post-run -- INVESTIGATE)"; vsp_actually_ready=0
    fi
  fi
  local vcfa_actually_ready=1
  if [ "$DO_VCFA" = 1 ]; then
    if node_preflight "$AUTOA_IP" "VCFA (auto-a) (post)"; then
      verify_vcfa_ready "$AUTOA_IP" || vcfa_actually_ready=0
    else
      echo "  (VCFA not healthy post-run -- INVESTIGATE)"; vcfa_actually_ready=0
    fi
  fi

  echo ""
  if { [ "$DO_VSP" = 1 ] && [ "$vsp_actually_ready" = 0 ]; } || { [ "$DO_VCFA" = 1 ] && [ "$vcfa_actually_ready" = 0 ]; }; then
    echo "FULL remediation pass applied, but NOT everything has been confirmed actually ready (see"
    echo "the WARNING(s) above) -- do not hand this lab back yet. Re-run --status, or just this"
    echo "script again, once the counters above look healthy."
  else
    echo "FULL remediation pass complete AND VERIFIED READY (control planes healthy and"
    echo "consistently responsive, VCFA login page reachable, no CrashLoopBackOff pods anywhere)."
    echo "Idempotent -- safe to re-run. Use --status to inspect."
  fi
}

# helper: preflight + discovery for a VSP-cluster action; aborts if unhealthy
vsp_gate() {
  reach_manager
  node_preflight "$VSP_CP_IP" "VSP fleet CP" || { echo "ABORT: VSP CP failed preflight -- refusing to act."; exit 2; }
  discover_vsp || { echo "ABORT: VSP discovery incomplete -- refusing to act."; exit 2; }
}
# helper: run a Family B/C action on the in-scope node(s)
fam_action_on_scope() {  # <action>
  reach_manager
  build_remote_scripts
  local rc=0
  if [ "$DO_VCFA" = 1 ]; then
    if node_preflight "$AUTOA_IP" "VCFA (auto-a)"; then famb_run "$AUTOA_IP" "VCFA (auto-a)" "$1" || rc=$?; fi
  fi
  if [ "$DO_VSP" = 1 ]; then
    if node_preflight "$VSP_CP_IP" "VSP fleet CP"; then famb_run "$VSP_CP_IP" "VSP fleet CP" "$1" || rc=$?; fi
  fi
  return "$rc"
}

# ═══════════ DISPATCH ═════════════════════════════════════════════════════════
case "$ACTION" in
  full) run_full ;;
  status) do_status ;;
  right-size)      vsp_gate; build_remote_scripts; do_right_size ;;
  reduce-ha)       vsp_gate; do_reduce_ha ;;
  safe-evict)      vsp_gate; do_safe_evict ;;
  disable-capi-le) vsp_gate; do_disable_capi_le ;;
  pin)             vsp_gate; do_pin ;;
  unpin)           vsp_gate; do_unpin ;;
  consolidate)     vsp_gate; if [ -n "$ARG" ]; then do_consolidate; else do_auto_consolidate; fi ;;
  cp-resize)       vsp_gate; do_cp_resize ;;
  worker-resize)   vsp_gate; do_worker_resize ;;
  pause)           vsp_gate; do_pause; PAUSED_BY_US=0 ;;   # explicit pause: don't let the exit-trap undo it
  unpause)         vsp_gate; do_unpause ;;
  entropy-fix)     reach_manager; [ "$DO_VSP" = 1 ] && node_preflight "$VSP_CP_IP" "VSP fleet CP" >/dev/null 2>&1; do_entropy_fix ;;
  keepers)
    reach_manager
    [ "$DO_VSP" = 1 ]  && { node_preflight "$VSP_CP_IP" "VSP fleet CP" && install_vsp_keeper "$VSP_CP_IP" || true; }
    [ "$DO_VCFA" = 1 ] && { node_preflight "$AUTOA_IP" "VCFA (auto-a)" && install_vcfa_keepers "$AUTOA_IP" || true; }
    ;;
  apply-lease|revert-lease|etcd-compaction|kube-vip-status|kube-vip-apply|kube-vip-cluster-patch|kyverno-resync-relax|envoy-gateway-fix|static-pod-hygiene)
    fam_action_on_scope "$ACTION" ;;
  kubelet-reload)
    # Deliberately NOT part of the default run: it restarts kubelet, which re-instantiates every
    # static pod and blips the apiserver ~60-90s. Everything else in this script is safe to re-run
    # unattended, and that property is worth protecting.
    echo "### --kubelet-reload restarts kubelet on the in-scope node(s). Static pods are"
    echo "### re-instantiated and the apiserver is briefly unavailable (~60-90s measured on 2701)."
    fam_action_on_scope "$ACTION" ;;
  vcfa-stabilize)
    reach_manager
    materialise_companion
    [ -f "$VCFA_MIT" ] || { echo "ERROR: companion script unavailable (set VCFA_MIT=/path/to/vcfa-storm-mitigation.sh)"; exit 2; }
    if [ "$DO_VCFA" = 1 ] && node_preflight "$AUTOA_IP" "VCFA (auto-a)"; then
      echo "== VCFA storm mitigation ('${ARG:-apply}') on auto-a ${AUTOA_IP} =="
      node_run_file "$AUTOA_IP" "$VCFA_MIT" "${ARG:-apply}"
      # 'apply' patches ReleaseTemplates (kube-vip, kyverno) that can trigger the same
      # vmsp-operator restart cascade run_full() waits out below -- see verify_vcfa_ready's
      # incident note. The lighter sub-actions (harden-gateway/harden-uitier/status/etc.) don't
      # touch a ReleaseTemplate, so they're not gated here.
      [ "${ARG:-apply}" = "apply" ] && { verify_vcfa_ready "$AUTOA_IP" || echo "  (see WARNING above -- do not treat this as done yet)"; }
    else echo "VCFA (auto-a) not available -- skipping vcfa-stabilize."; fi
    ;;
  kcp-patch)
    reach_manager
    [ "$DO_VCFA" = 1 ] && do_kcp_patch "$AUTOA_IP"  "VCFA (auto-a)" || true
    [ "$DO_VSP" = 1 ]  && do_kcp_patch "$VSP_CP_IP" "VSP fleet CP"  || true
    ;;
  remove) reach_manager; do_remove ;;
  *) echo "no action (internal error: ACTION=$ACTION)"; exit 1 ;;
esac
