#!/usr/bin/env bash
# Calibrate Strategy A and B: 1 replica, no autoscaler, Locust at 4, 16, 32, 60 users for 120 s each.
# Writes results/gcp/calibration.csv. Uses 1 GPU node for about 12 minutes.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/calibrate.sh [--levels "4 16 32 60"] [--duration 120] [--prom-port 9090]

Needs: vllm deployed (deploy_serving.sh) and monitoring installed. The script removes any
autoscaler, pins vllm to 1 replica, refreshes the loadgen pod, then for each user level:
  runs Locust in the loadgen pod with SHAPE=calibrate, copies its requests.csv out, and queries
  Prometheus over the level (skipping the first 30 s) for:
    cpu_pct_of_request  100 * vLLM container CPU / CPU request   (what HPA compares to its target)
    avg_waiting         sum(vllm:num_requests_waiting)           (what KEDA divides by replicas)
    avg_running         sum(vllm:num_requests_running)
  plus p95 TTFT, SLO violation share and requests per second from requests.csv.
Output: results/gcp/calibration.csv and results/gcp/calibration/requests_<users>.csv

Picking thresholds from the CSV:
  CPU_TARGET_PERCENT  the cpu_pct_of_request at the first level where p95_ttft_s nears SLO_TTFT_S
                      (or slightly below it). If CPU barely moves between levels, that is a finding:
                      CPU is a weak signal for GPU serving, which is the point of the comparison.
  QUEUE_THRESHOLD     about avg_waiting at that same level, so one replica scales before TTFT breaks
                      the SLO. If avg_waiting is 0 at every level, lower MAX_NUM_SEQS and redeploy.
USAGE
}
handle_help "$@"
levels="4 16 32 60"
duration=120
prom_port=9090
while [[ $# -gt 0 ]]; do
  case "$1" in
    --levels) levels="$2"; shift 2 ;;
    --duration) duration="$2"; shift 2 ;;
    --prom-port) prom_port="$2"; shift 2 ;;
    *) die "unknown argument $1 (see --help)" ;;
  esac
done
load_config
require_cmd kubectl envsubst python3 curl

out_dir="${REPO_ROOT}/results/gcp"
raw_dir="${out_dir}/calibration"
mkdir -p "$raw_dir"
csv="${out_dir}/calibration.csv"

delete_autoscalers
kubectl scale deployment vllm --replicas=1
kubectl rollout status deployment/vllm --timeout=25m
sync_loadgen
pod=$(loadgen_pod)
[[ -n "$pod" ]] || die "no running loadgen pod"

trap stop_port_forward EXIT
port_forward svc/monitoring-kube-prometheus-prometheus "${prom_port}:9090" monitoring /-/ready
prom="http://127.0.0.1:${prom_port}"

cpu_q='100 * sum(rate(container_cpu_usage_seconds_total{namespace="default",container="vllm"}[1m])) / sum(kube_pod_container_resource_requests{namespace="default",container="vllm",resource="cpu"})'
wait_q='sum(vllm:num_requests_waiting)'
run_q='sum(vllm:num_requests_running)'

echo "users,start,end,cpu_pct_of_request,avg_waiting,avg_running,requests,rps,p50_ttft_s,p95_ttft_s,slo_violation_rate,failed" > "$csv"
for users in $levels; do
  log "level ${users} users for ${duration} s"
  start=$(date +%s)
  kubectl exec "$pod" -- env SHAPE=calibrate USERS="$users" DURATION="$duration" \
    MODEL_ID="$MODEL_ID" RESULTS_DIR="/results/calib_${users}" \
    locust -f /app/locustfile.py --headless --host http://vllm.default.svc:8000 --only-summary \
    || warn "locust exited non zero at ${users} users (some requests failed); continuing"
  end=$(date +%s)
  sleep 20  # let Prometheus scrape the last interval
  kubectl cp "${pod}:/results/calib_${users}/requests.csv" "${raw_dir}/requests_${users}.csv"

  from=$(( start + 30 ))
  cpu=$(python3 "${GCP_SCRIPTS}/prom.py" mean --url "$prom" --query "$cpu_q" --start "$from" --end "$end")
  waiting=$(python3 "${GCP_SCRIPTS}/prom.py" mean --url "$prom" --query "$wait_q" --start "$from" --end "$end")
  running=$(python3 "${GCP_SCRIPTS}/prom.py" mean --url "$prom" --query "$run_q" --start "$from" --end "$end")
  stats=$(python3 - "${raw_dir}/requests_${users}.csv" "$from" "$end" "${SLO_TTFT_S:-2}" <<'PY'
import csv, sys
path, t0, t1, slo = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]), float(sys.argv[4])
rows = [r for r in csv.DictReader(open(path)) if t0 <= float(r["start"]) <= t1]
ok = [r for r in rows if r["ok"] == "1"]
ttft = sorted(float(r["ttft_s"]) for r in ok)
def pct(xs, q):
    return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else float("nan")
viol = sum(1 for r in rows if r["ok"] != "1" or float(r["ttft_s"]) > slo)
n = len(rows)
print(f"{n},{len(ok)/(t1-t0):.3f},{pct(ttft,.5):.3f},{pct(ttft,.95):.3f},{(viol/n if n else float('nan')):.4f},{n-len(ok)}")
PY
)
  echo "${users},${start},${end},${cpu},${waiting},${running},${stats}" >> "$csv"
  log "users ${users}: cpu ${cpu}% of request, waiting ${waiting}, running ${running}"
  sleep 30  # drain before the next level
done

echo
column -s, -t < "$csv" || cat "$csv"
log "wrote ${csv}. See --help for how to pick CPU_TARGET_PERCENT and QUEUE_THRESHOLD."
