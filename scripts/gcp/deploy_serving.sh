#!/usr/bin/env bash
# Deploy vLLM and its ServiceMonitor, wait until Ready, then send one test request.
# A GPU node is created on demand if none is free: COSTS MONEY until scripts/gcp/scale_gpu_zero.sh.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/deploy_serving.sh [--local-port PORT]

  1. envsubst k8s/vllm.yaml | kubectl apply
  2. kubectl apply k8s/vllm-servicemonitor.yaml
  3. wait for the vllm rollout (up to 25 min: GPU node start, image pull, model download)
  4. port-forward svc/vllm and run loadtest/smoke_test.py, which prints the answer and TTFT
Default local port 8000.
USAGE
}
handle_help "$@"
local_port=8000
[[ "${1:-}" == "--local-port" ]] && local_port="$2"
load_config
require_cmd kubectl envsubst python3 curl

envsubst < "${REPO_ROOT}/k8s/vllm.yaml" | kubectl apply -f -
kubectl apply -f "${REPO_ROOT}/k8s/vllm-servicemonitor.yaml"

if [[ "$(kubectl get deployment vllm -o jsonpath='{.spec.replicas}')" == "0" ]]; then
  log "vllm was scaled to 0, scaling to 1"
  kubectl scale deployment vllm --replicas=1
fi

log "waiting for vllm to be Ready (first start takes several minutes)"
kubectl rollout status deployment/vllm --timeout=25m
kubectl get pods -l app=vllm -o wide

trap stop_port_forward EXIT
port_forward svc/vllm "${local_port}:8000" default /health
python3 "${REPO_ROOT}/loadtest/smoke_test.py" --url "http://127.0.0.1:${local_port}" --model "$MODEL_ID"
log "serving works. Next: scripts/gcp/check_metrics.sh"
