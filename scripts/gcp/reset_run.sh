#!/usr/bin/env bash
# Put the cluster in a known state before every experiment run.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/reset_run.sh <baseline_gpu_nodes> [--timeout SECONDS]

  baseline_gpu_nodes  1 for a normal run, 2 for a run with 1 warm spare GPU node

Steps:
  1. delete the HPA and KEDA ScaledObject
  2. scale deployment vllm to 1
  3. set GPU pool autoscaling min to <baseline> (total), so the cluster autoscaler keeps the spare
  4. if baseline > 1, run short lived gpu-warmup pods that each request a GPU, so the autoscaler
     adds the spare node(s), then delete them once the nodes are Ready
  5. wait until vllm is Ready and exactly <baseline> GPU nodes exist, stable for 60 s
     (extra nodes left from an earlier run are removed by the cluster autoscaler, which can take minutes)
Default timeout is 1800 s.

Why not gcloud resize: --num-nodes is per zone in a multi zone pool, so it cannot target 1 or 2 total.
USAGE
}
handle_help "$@"
[[ $# -ge 1 ]] || { usage; exit 1; }
baseline="$1"; shift
[[ "$baseline" =~ ^[1-9][0-9]*$ ]] || die "baseline_gpu_nodes must be a positive integer"
timeout_s=1800
if [[ "${1:-}" == "--timeout" ]]; then timeout_s="$2"; fi
load_config
require_cmd gcloud kubectl envsubst

delete_autoscalers
kubectl scale deployment vllm --replicas=1
set_gpu_pool_bounds "$baseline" "$GPU_POOL_MAX_NODES"

spares=$(( baseline - 1 ))
kubectl delete deployment gpu-warmup --ignore-not-found
if [[ "$spares" -gt 0 ]]; then
  log "starting ${spares} gpu-warmup pod(s) to pull in spare node(s)"
  cat <<YAML | kubectl apply -f -
apiVersion: apps/v1
kind: Deployment
metadata: {name: gpu-warmup, labels: {app: gpu-warmup}}
spec:
  replicas: ${spares}
  selector: {matchLabels: {app: gpu-warmup}}
  template:
    metadata: {labels: {app: gpu-warmup}}
    spec:
      nodeSelector: {cloud.google.com/gke-accelerator: ${GPU_TYPE}}
      affinity:
        podAntiAffinity:
          requiredDuringSchedulingIgnoredDuringExecution:
          - labelSelector: {matchExpressions: [{key: app, operator: In, values: [vllm, gpu-warmup]}]}
            topologyKey: kubernetes.io/hostname
      containers:
      - name: pause
        image: registry.k8s.io/pause:3.9
        resources: {limits: {nvidia.com/gpu: 1}}
YAML
fi

deadline=$(( $(date +%s) + timeout_s ))
stable_since=0
warmup_deleted=0
while true; do
  nodes=$(gpu_node_count ready)
  total=$(gpu_node_count)
  ready_vllm=$(kubectl get deployment vllm -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)
  ready_vllm=${ready_vllm:-0}
  log "vllm ready ${ready_vllm}/1, GPU nodes ready ${nodes} (total ${total}), want ${baseline}"

  if [[ "$spares" -gt 0 && "$warmup_deleted" -eq 0 && "$nodes" -ge "$baseline" ]]; then
    log "spare node(s) are up, removing gpu-warmup"
    kubectl delete deployment gpu-warmup --ignore-not-found
    warmup_deleted=1
  fi

  if [[ "$ready_vllm" -eq 1 && "$nodes" -eq "$baseline" && "$total" -eq "$baseline" \
        && ( "$spares" -eq 0 || "$warmup_deleted" -eq 1 ) ]]; then
    [[ "$stable_since" -eq 0 ]] && stable_since=$(date +%s)
    if (( $(date +%s) - stable_since >= 60 )); then
      log "stable for 60 s: 1 vllm replica Ready on ${baseline} GPU node(s)"
      exit 0
    fi
  else
    stable_since=0
  fi
  [[ $(date +%s) -lt $deadline ]] || die "not stable after ${timeout_s} s. Check: kubectl get pods,nodes"
  sleep 10
done
