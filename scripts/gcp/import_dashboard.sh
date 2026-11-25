#!/usr/bin/env bash
# Load monitoring/relay-dashboard.json into the cluster's Grafana through its HTTP API. Safe to rerun.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/import_dashboard.sh [--local-port PORT]

Reads the Grafana admin password from secret monitoring/monitoring-grafana, port-forwards
svc/monitoring-grafana, and POSTs the dashboard to /api/dashboards/db with overwrite true.
Then open http://127.0.0.1:3000/d/relay after: kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80
Default local port 3000.
USAGE
}
handle_help "$@"
local_port=3000
[[ "${1:-}" == "--local-port" ]] && local_port="$2"
load_config
require_cmd kubectl curl python3

password=$(kubectl get secret -n monitoring monitoring-grafana -o jsonpath='{.data.admin-password}' | base64 --decode)
trap stop_port_forward EXIT
port_forward svc/monitoring-grafana "${local_port}:80" monitoring /api/health

payload=$(python3 -c '
import json, sys
print(json.dumps({"dashboard": json.load(open(sys.argv[1])), "overwrite": True, "folderId": 0}))
' "${REPO_ROOT}/monitoring/relay-dashboard.json")

response=$(curl -s -u "admin:${password}" -H 'Content-Type: application/json' \
  -X POST "http://127.0.0.1:${local_port}/api/dashboards/db" -d "$payload")
echo "$response"
echo "$response" | grep -q '"status":"success"' || die "Grafana did not accept the dashboard"
log "imported. URL path: /d/relay"
