#!/usr/bin/env bash
# Create the GKE cluster and the multi zone L4 GPU pool, then fetch credentials.
# Skips anything that already exists, so it is safe to rerun. COSTS MONEY: the CPU nodes bill from creation.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/create_cluster.sh

Creates, if missing:
  1. the required Google APIs
  2. cluster CLUSTER_NAME in GCP_ZONE with CPU_NODES x CPU_MACHINE, image streaming on
  3. GPU pool GPU_POOL across GPU_ZONES, 1 GPU_TYPE per node, 0 nodes, autoscaling 0..GPU_POOL_MAX_NODES total
  4. the optimize-utilization autoscaling profile
  5. kubectl credentials
All settings come from config/relay.env.
USAGE
}
handle_help "$@"
load_config
require_cmd gcloud kubectl

gcloud config set project "$GCP_PROJECT_ID" --quiet
log "enabling APIs (no-op if already enabled)"
gcloud services enable container.googleapis.com artifactregistry.googleapis.com containerfilesystem.googleapis.com

if gcloud container clusters describe "$CLUSTER_NAME" --zone "$GCP_ZONE" >/dev/null 2>&1; then
  log "cluster ${CLUSTER_NAME} already exists, skipping create"
else
  log "creating cluster ${CLUSTER_NAME} in ${GCP_ZONE}"
  gcloud container clusters create "$CLUSTER_NAME" \
    --zone "$GCP_ZONE" --machine-type "$CPU_MACHINE" --num-nodes "$CPU_NODES" \
    --enable-image-streaming --image-type COS_CONTAINERD
fi

if gcloud container node-pools describe "$GPU_POOL" --cluster "$CLUSTER_NAME" --zone "$GCP_ZONE" >/dev/null 2>&1; then
  log "node pool ${GPU_POOL} already exists, skipping create"
else
  log "creating GPU pool ${GPU_POOL} in ${GPU_ZONES}"
  gcloud container node-pools create "$GPU_POOL" --cluster "$CLUSTER_NAME" --zone "$GCP_ZONE" \
    --node-locations "$GPU_ZONES" \
    --machine-type "$GPU_MACHINE" \
    --accelerator "type=${GPU_TYPE},count=1,gpu-driver-version=latest" \
    --num-nodes 0 --enable-autoscaling --total-min-nodes 0 --total-max-nodes "$GPU_POOL_MAX_NODES"
fi

profile=$(gcloud container clusters describe "$CLUSTER_NAME" --zone "$GCP_ZONE" \
  --format='value(autoscaling.autoscalingProfile)' 2>/dev/null || true)
if [[ "$profile" == "OPTIMIZE_UTILIZATION" ]]; then
  log "autoscaling profile already optimize-utilization"
else
  log "setting autoscaling profile optimize-utilization"
  gcloud container clusters update "$CLUSTER_NAME" --zone "$GCP_ZONE" --autoscaling-profile optimize-utilization --quiet
fi

gcloud container clusters get-credentials "$CLUSTER_NAME" --zone "$GCP_ZONE"
kubectl get nodes -o wide
log "done. Next: scripts/gcp/gpu_proof.sh"
