#!/usr/bin/env bash
# The 3 GPU proof: bring up 1 GPU node per zone, show them Ready, then resize back to 0.
# COSTS MONEY while nodes are up (a few minutes of 3 L4 nodes).
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/gpu_proof.sh [--keep]

Resizes GPU_POOL to 1 node per zone (one per zone in GPU_ZONES), waits up to 15 minutes until
they are all Ready, prints node names and zones, then resizes back to 0.
  --keep   leave the nodes up afterwards (remember scripts/gcp/scale_gpu_zero.sh)
USAGE
}
handle_help "$@"
load_config
require_cmd gcloud kubectl

keep=0
[[ "${1:-}" == "--keep" ]] && keep=1
want=$(gpu_zone_count)

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
  log "GPU nodes Ready: ${ready}/${want}"
  [[ "$ready" -ge "$want" ]] && break
  [[ $(date +%s) -lt $deadline ]] || { warn "timed out after 15 minutes; some zones may be out of L4 capacity"; break; }
  sleep 20
done

echo
kubectl get nodes -l "cloud.google.com/gke-accelerator=${GPU_TYPE}" \
  -L topology.kubernetes.io/zone -L cloud.google.com/gke-accelerator
echo
if [[ "$(gpu_node_count ready)" -ge "$want" ]]; then
  log "SUCCESS: ${want} GPU nodes Ready. Screenshot the table above."
else
  log "FAILED: fewer than ${want} GPU nodes Ready. Drop zones without capacity from GPU_ZONES or retry later."
fi
[[ "$keep" -eq 1 ]] && log "--keep given: nodes left up"
