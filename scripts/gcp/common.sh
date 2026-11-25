#!/usr/bin/env bash
# Shared helpers for every scripts/gcp/*.sh script. Source it, do not run it.
#   source "$(dirname "$0")/common.sh"
#   handle_help "$@"     # prints the caller's usage() on -h/--help and exits
#   load_config          # sources config/relay.env and checks required settings
# shellcheck disable=SC2034  # variables here are used by the scripts that source this file

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONFIG_FILE="${RELAY_CONFIG:-${REPO_ROOT}/config/relay.env}"
GCP_SCRIPTS="${REPO_ROOT}/scripts/gcp"

log()  { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
warn() { printf '[%s] WARNING: %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die()  { printf '[%s] ERROR: %s\n' "$(date +%H:%M:%S)" "$*" >&2; exit 1; }

# Print the caller's usage() and exit when -h or --help is anywhere in the args.
handle_help() {
  local arg
  for arg in "$@"; do
    if [[ "$arg" == "-h" || "$arg" == "--help" ]]; then
      usage
      exit 0
    fi
  done
}

require_cmd() {
  local c
  for c in "$@"; do
    command -v "$c" >/dev/null 2>&1 || die "'$c' is not installed. See docs/RUN_ON_GCP.md section 0."
  done
}

# Source config/relay.env with every variable exported (envsubst needs them exported).
load_config() {
  [[ -f "$CONFIG_FILE" ]] || die "missing $CONFIG_FILE. Run: cp config/relay.env.example config/relay.env and fill it in."
  set -a
  # shellcheck disable=SC1090
  source "$CONFIG_FILE"
  set +a
  local v
  for v in MODEL_ID VLLM_TAG GCP_PROJECT_ID GCP_REGION GCP_ZONE GPU_ZONES CLUSTER_NAME GPU_POOL \
           GPU_TYPE AR_REPO VLLM_IMAGE MIN_REPLICAS MAX_REPLICAS CHECK_INTERVAL_S SCALE_DOWN_WINDOW_S; do
    [[ -n "${!v:-}" ]] || die "$v is empty in $CONFIG_FILE"
    [[ "${!v}" != *REPLACE* ]] || die "$v still says REPLACE in $CONFIG_FILE"
  done
  [[ "$VLLM_TAG" != "latest" ]] || die "VLLM_TAG must be a pinned version, not latest"
  # Defaults for settings added after the first template, so older relay.env files keep working.
  : "${GPU_POOL_MAX_NODES:=4}" "${CPU_MACHINE:=e2-standard-4}" "${CPU_NODES:=2}"
  : "${MAX_NUM_SEQS:=256}" "${LOCUST_TAG:=2.32.3}" "${MAX_MODEL_LEN:=4096}"
  : "${KPS_CHART_VERSION:=}" "${KEDA_CHART_VERSION:=}" "${GPU_PARTITION_SIZE:=}"
  export GPU_POOL_MAX_NODES CPU_MACHINE CPU_NODES MAX_NUM_SEQS LOCUST_TAG MAX_MODEL_LEN GPU_PARTITION_SIZE
}

# The --accelerator value for the GPU node pool, with MIG slices when GPU_PARTITION_SIZE is set.
gpu_accelerator_flag() {
  local flag="type=${GPU_TYPE},count=1,gpu-driver-version=latest"
  [[ -n "$GPU_PARTITION_SIZE" ]] && flag="${flag},gpu-partition-size=${GPU_PARTITION_SIZE}"
  echo "$flag"
}

# Total nvidia.com/gpu the GPU nodes offer to pods: 1 per L4 node, or the MIG slice count per A100 node.
gpu_allocatable() {
  kubectl get nodes -l "cloud.google.com/gke-accelerator=${GPU_TYPE}" \
    -o jsonpath='{range .items[*]}{.status.allocatable.nvidia\.com/gpu}{"\n"}{end}' 2>/dev/null \
    | awk '{s += $1} END {print s + 0}'
}

# Number of zones in GPU_ZONES. gcloud resize --num-nodes is PER ZONE for multi zone pools.
gpu_zone_count() {
  local IFS=','
  # shellcheck disable=SC2206
  local zones=($GPU_ZONES)
  echo "${#zones[@]}"
}

# Count GPU nodes, optionally only Ready ones.
gpu_node_count() {
  local mode="${1:-all}"
  if [[ "$mode" == "ready" ]]; then
    kubectl get nodes -l "cloud.google.com/gke-accelerator=${GPU_TYPE}" --no-headers 2>/dev/null \
      | awk '$2 == "Ready"' | wc -l | tr -d ' '
  else
    kubectl get nodes -l "cloud.google.com/gke-accelerator=${GPU_TYPE}" --no-headers 2>/dev/null \
      | wc -l | tr -d ' '
  fi
}

# Resize the GPU pool to N nodes PER ZONE. Total nodes = N * gpu_zone_count.
resize_gpu_pool_per_zone() {
  local per_zone="$1"
  log "resizing ${GPU_POOL} to ${per_zone} node(s) per zone ($(gpu_zone_count) zones)"
  gcloud container clusters resize "$CLUSTER_NAME" --node-pool "$GPU_POOL" \
    --num-nodes "$per_zone" --zone "$GCP_ZONE" --quiet
}

# Set cluster autoscaler bounds for the GPU pool, as totals across all zones.
set_gpu_pool_bounds() {
  local min_total="$1" max_total="$2"
  log "GPU pool autoscaling bounds: total min ${min_total}, total max ${max_total}"
  gcloud container clusters update "$CLUSTER_NAME" --zone "$GCP_ZONE" --node-pool "$GPU_POOL" \
    --enable-autoscaling --total-min-nodes "$min_total" --total-max-nodes "$max_total" --quiet
}

# Remove both autoscalers. Safe if neither exists.
delete_autoscalers() {
  kubectl delete hpa vllm-cpu --ignore-not-found
  if kubectl api-resources --api-group=keda.sh -o name 2>/dev/null | grep -q '^scaledobjects'; then
    kubectl delete scaledobject vllm-queue --ignore-not-found
  fi
  # KEDA creates its own HPA named keda-hpa-<scaledobject>; it goes away with the ScaledObject.
}

# Start kubectl port-forward in the background and wait until the local port answers.
# Sets PF_PID. Caller should: trap 'stop_port_forward' EXIT
port_forward() {
  local target="$1" ports="$2" namespace="${3:-default}" check_path="${4:-/}"
  local local_port="${ports%%:*}"
  kubectl port-forward -n "$namespace" "$target" "$ports" >/dev/null 2>&1 &
  PF_PID=$!
  local i
  for i in $(seq 1 30); do
    if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:${local_port}${check_path}"; then
      return 0
    fi
    kill -0 "$PF_PID" 2>/dev/null || die "port-forward to ${target} exited. Is local port ${local_port} already in use?"
    sleep 1
  done
  die "port-forward to ${target} on ${local_port} did not become reachable"
}

stop_port_forward() {
  if [[ -n "${PF_PID:-}" ]]; then
    kill "$PF_PID" 2>/dev/null || true
    wait "$PF_PID" 2>/dev/null || true
    PF_PID=""
  fi
}

# Refresh the locust-files ConfigMap from loadtest/ and restart loadgen so the new files are mounted.
sync_loadgen() {
  log "refreshing locust-files ConfigMap and loadgen pod"
  kubectl create configmap locust-files --from-file="${REPO_ROOT}/loadtest/" \
    --dry-run=client -o yaml | kubectl apply -f -
  envsubst < "${REPO_ROOT}/k8s/loadgen.yaml" | kubectl apply -f -
  kubectl rollout restart deployment/loadgen
  kubectl rollout status deployment/loadgen --timeout=5m
}

loadgen_pod() {
  kubectl get pods -l app=loadgen --field-selector=status.phase=Running \
    -o jsonpath='{.items[0].metadata.name}'
}
