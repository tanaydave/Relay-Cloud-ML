#!/usr/bin/env bash
# Switch the active autoscaler: Strategy A (HPA on CPU), Strategy B (KEDA on waiting requests), or none.
# Never leaves both active.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/use_strategy.sh <a|b|none> [threshold]

  a     delete KEDA ScaledObject vllm-queue, apply k8s/strategy-a-hpa.yaml (HPA vllm-cpu,
        target CPU_TARGET_PERCENT of the CPU request)
  b     delete HPA vllm-cpu, apply k8s/strategy-b-keda.yaml (ScaledObject vllm-queue,
        threshold waiting requests per replica; default QUEUE_THRESHOLD)
  none  delete both
Ends by printing kubectl get hpa. With b you will see keda-hpa-vllm-queue, which KEDA manages.
USAGE
}
handle_help "$@"
[[ $# -ge 1 ]] || { usage; exit 1; }
strategy="$1"
threshold="${2:-}"
load_config
require_cmd kubectl envsubst

case "$strategy" in
  a)
    [[ -z "$threshold" ]] || warn "threshold ignored for strategy a"
    delete_autoscalers
    envsubst < "${REPO_ROOT}/k8s/strategy-a-hpa.yaml" | kubectl apply -f -
    ;;
  b)
    if [[ -n "$threshold" ]]; then
      [[ "$threshold" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "threshold must be a number"
      export QUEUE_THRESHOLD="$threshold"
    fi
    delete_autoscalers
    envsubst < "${REPO_ROOT}/k8s/strategy-b-keda.yaml" | kubectl apply -f -
    log "strategy b threshold: ${QUEUE_THRESHOLD} waiting per replica"
    ;;
  none)
    delete_autoscalers
    ;;
  *)
    usage
    exit 1
    ;;
esac

# Safety check: exactly one or zero autoscalers.
has_a=0; has_b=0
kubectl get hpa vllm-cpu >/dev/null 2>&1 && has_a=1
kubectl get scaledobject vllm-queue >/dev/null 2>&1 && has_b=1
if [[ "$has_a" -eq 1 && "$has_b" -eq 1 ]]; then
  delete_autoscalers
  die "both autoscalers were active; deleted both. Rerun use_strategy.sh"
fi

kubectl get hpa
[[ "$has_b" -eq 1 ]] && kubectl get scaledobject vllm-queue
log "active strategy: ${strategy}"
