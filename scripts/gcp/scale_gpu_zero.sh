#!/usr/bin/env bash
# End of every session: stop all GPU spend. Removes autoscalers, scales vLLM to 0, GPU pool to 0.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/scale_gpu_zero.sh

Deletes the HPA and KEDA ScaledObject, scales deployment vllm to 0, sets the GPU pool
autoscaling minimum to 0 and resizes the GPU pool to 0. Run at the end of EVERY work session.
The CPU nodes keep running (cheap); scripts/gcp/teardown.sh removes everything.
USAGE
}
handle_help "$@"
load_config
require_cmd gcloud kubectl

delete_autoscalers
kubectl delete deployment gpu-warmup --ignore-not-found
if kubectl get deployment vllm >/dev/null 2>&1; then
  kubectl scale deployment vllm --replicas=0
fi
set_gpu_pool_bounds 0 "$GPU_POOL_MAX_NODES"
resize_gpu_pool_per_zone 0
log "GPU nodes now: $(gpu_node_count) (may take a minute to reach 0)"
kubectl get nodes -l "cloud.google.com/gke-accelerator=${GPU_TYPE}" || true
