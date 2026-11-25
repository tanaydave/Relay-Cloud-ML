#!/usr/bin/env bash
# Print the vLLM metrics Relay depends on and warn if a name differs from CLAUDE.md.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/check_metrics.sh [--local-port PORT]

Port-forwards svc/vllm (one replica answers) and prints every /metrics line containing
num_requests_waiting, num_requests_running, time_to_first_token or process_cpu.
Warns if vllm:num_requests_waiting, vllm:num_requests_running or
vllm:time_to_first_token_seconds_bucket is missing. Default local port 8000.
USAGE
}
handle_help "$@"
local_port=8000
[[ "${1:-}" == "--local-port" ]] && local_port="$2"
load_config
require_cmd kubectl curl

trap stop_port_forward EXIT
port_forward svc/vllm "${local_port}:8000" default /health
metrics=$(curl -s --max-time 10 "http://127.0.0.1:${local_port}/metrics")

echo "$metrics" | grep -E 'num_requests_waiting|num_requests_running|time_to_first_token|process_cpu' \
  | grep -v '^# HELP' | grep -vE '_bucket\{.*le="[^+]' || true   # histogram buckets: only le="+Inf" shown
echo

status=0
for name in 'vllm:num_requests_waiting' 'vllm:num_requests_running' 'vllm:time_to_first_token_seconds_bucket'; do
  if echo "$metrics" | grep -q "^${name}"; then
    log "found ${name}"
  else
    warn "${name} NOT found. CLAUDE.md, the KEDA query and the dashboard assume it. Update them to the real name."
    status=1
  fi
done
if ! echo "$metrics" | grep -q '^process_cpu_seconds_total'; then
  warn "process_cpu_seconds_total not exported by vLLM. Fine on GCP (HPA uses cAdvisor CPU); the HPC autoscaler must use sstat."
fi
exit "$status"
