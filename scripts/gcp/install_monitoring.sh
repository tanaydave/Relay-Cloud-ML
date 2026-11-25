#!/usr/bin/env bash
# Install kube-prometheus-stack (Prometheus, Grafana, kube-state-metrics) and KEDA. Idempotent.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/install_monitoring.sh

helm upgrade --install of:
  monitoring  prometheus-community/kube-prometheus-stack  in namespace monitoring, values monitoring/kps-values.yaml
  keda        kedacore/keda                               in namespace keda
Chart versions come from KPS_CHART_VERSION and KEDA_CHART_VERSION in config/relay.env (empty = newest, with a warning).
USAGE
}
handle_help "$@"
load_config
require_cmd helm kubectl

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update
helm repo add kedacore https://kedacore.github.io/charts --force-update
helm repo update

version_flag() {
  local name="$1" version="$2"
  if [[ -n "$version" ]]; then
    printf -- '--version=%s' "$version"
  else
    warn "${name} chart version not pinned; using newest. Record it with: helm list -A"
  fi
}

log "installing kube-prometheus-stack"
# shellcheck disable=SC2046
helm upgrade --install monitoring prometheus-community/kube-prometheus-stack \
  -n monitoring --create-namespace \
  -f "${REPO_ROOT}/monitoring/kps-values.yaml" \
  $(version_flag kube-prometheus-stack "$KPS_CHART_VERSION") \
  --wait --timeout 10m

log "installing KEDA"
# shellcheck disable=SC2046
helm upgrade --install keda kedacore/keda -n keda --create-namespace \
  $(version_flag keda "$KEDA_CHART_VERSION") \
  --wait --timeout 10m

helm list -A
kubectl get pods -n monitoring
kubectl get pods -n keda
log "done. Prometheus service should be reachable in cluster at ${PROM_URL_IN_CLUSTER}"
kubectl get svc -n monitoring monitoring-kube-prometheus-prometheus >/dev/null \
  || warn "service monitoring-kube-prometheus-prometheus not found; fix PROM_URL_IN_CLUSTER"
