#!/usr/bin/env bash
# The GPU proof: bring GPU nodes up, show the GPUs Kubernetes can hand to replicas, then resize back to 0.
# COSTS MONEY while nodes are up (a few minutes).
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/gpu_proof.sh [--keep]

Resizes GPU_POOL to 1 node per zone in GPU_ZONES, waits up to 15 minutes until they are Ready,
prints the nodes, their zones and how many GPUs each offers, then resizes back to 0.
  A100 with MIG (GPU_PARTITION_SIZE set): success is 1 node offering at least MAX_REPLICAS GPU slices.
  L4 (no partition): success is one Ready node per zone, at least MAX_REPLICAS GPUs in total.
  --keep   leave the nodes up afterwards (remember scripts/gcp/scale_gpu_zero.sh)
USAGE
}
handle_help "$@"
load_config
require_cmd gcloud kubectl

keep=0
[[ "${1:-}" == "--keep" ]] && keep=1
want_nodes=$(gpu_zone_count)

cleanup() {
  if [[ "$keep" -eq 0 ]]; then
    resize_gpu_pool_per_zone 0 || warn "resize to 0 failed; run scripts/gcp/scale_gpu_zero.sh NOW"
  fi
}
trap cleanup EXIT

resize_gpu_pool_per_zone 1
deadline=$(( $(date +%s) + 900 ))
while true; do
  ready=$(gpu_node_count ready)
  gpus=$(gpu_allocatable)
  log "GPU nodes Ready: ${ready}/${want_nodes}, GPUs offered to pods: ${gpus}/${MAX_REPLICAS}"
  [[ "$ready" -ge "$want_nodes" && "$gpus" -ge "$MAX_REPLICAS" ]] && break
  [[ $(date +%s) -lt $deadline ]] || { warn "timed out after 15 minutes"; break; }
  sleep 20
done

echo
kubectl get nodes -l "cloud.google.com/gke-accelerator=${GPU_TYPE}" \
  -L topology.kubernetes.io/zone -L cloud.google.com/gke-accelerator -L cloud.google.com/gke-gpu-partition-size
echo
kubectl get nodes -l "cloud.google.com/gke-accelerator=${GPU_TYPE}" \
  -o custom-columns='NODE:.metadata.name,GPUS_FOR_PODS:.status.allocatable.nvidia\.com/gpu'
echo
if [[ "$(gpu_node_count ready)" -ge "$want_nodes" && "$(gpu_allocatable)" -ge "$MAX_REPLICAS" ]]; then
  log "SUCCESS: ${MAX_REPLICAS} GPUs available for ${MAX_REPLICAS} replicas. Screenshot the tables above."
else
  log "FAILED. Out of capacity in a zone: try another zone in GPU_ZONES or retry later."
  log "Ready node but too few GPUs with MIG: the driver may still be partitioning; rerun with --keep and check again in 5 minutes."
fi
[[ "$keep" -eq 1 ]] && log "--keep given: nodes left up"
