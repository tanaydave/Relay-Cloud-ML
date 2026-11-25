#!/usr/bin/env bash
# One experiment run on GKE, end to end. Uses 1 to 4 GPU nodes for about 20 to 30 minutes: COSTS MONEY.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/run_experiment.sh <study> <strategy> <pattern> <repeat> [--spare 0|1] [--threshold N]

  study     main | warm | sweep | practice (any lowercase name works; it becomes the folder)
  strategy  a | b | none
  pattern   steady | ramp | spike
  repeat    1, 2, 3 ...
  --spare N       warm spare GPU nodes (0 or 1); adds _spareN to the folder name
  --threshold N   strategy b waiting per replica; adds _tN to the folder name

Steps, stopping with an error if any fails:
  1. reset_run.sh with baseline 1 + spare     5. wait 180 s with the autoscaler still on (scale down)
  2. use_strategy.sh <strategy> [threshold]   6. copy requests.csv and Locust CSVs out of the pod
  3. refresh the locust-files ConfigMap       7. export_metrics.py -> timeseries.csv
  4. run Locust headless in the loadgen pod   8. events, pod and node JSON -> events.json; meta.json
     with SHAPE=<pattern>, against http://vllm.default.svc:8000

Output: results/gcp/<study>/<strategy>_<pattern>_r<repeat>[_<variant>]/
A run is complete when its folder has meta.json (written last). The script refuses to touch a complete
or partial folder; delete it to rerun. A failed run leaves a FAILED file with the time and exit code.
USAGE
}
handle_help "$@"
[[ $# -ge 4 ]] || { usage; exit 1; }
study="$1" strategy="$2" pattern="$3" repeat="$4"; shift 4
spare="" threshold=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --spare) spare="$2"; shift 2 ;;
    --threshold) threshold="$2"; shift 2 ;;
    *) die "unknown argument $1 (see --help)" ;;
  esac
done
[[ "$study" =~ ^[a-z0-9_-]+$ ]] || die "study must be lowercase letters, digits, _ or -"
[[ "$strategy" =~ ^(a|b|none)$ ]] || die "strategy must be a, b or none"
[[ "$pattern" =~ ^(steady|ramp|spike)$ ]] || die "pattern must be steady, ramp or spike"
[[ "$repeat" =~ ^[1-9][0-9]*$ ]] || die "repeat must be a positive integer"
[[ -z "$spare" || "$spare" =~ ^[01]$ ]] || die "--spare must be 0 or 1"
[[ -z "$threshold" || "$strategy" == "b" ]] || die "--threshold only applies to strategy b"
load_config
require_cmd kubectl gcloud envsubst python3 curl git

variant=""
[[ -n "$spare" ]] && variant="spare${spare}"
[[ -n "$threshold" ]] && variant="${variant:+${variant}_}t${threshold}"
run_id="${strategy}_${pattern}_r${repeat}${variant:+_${variant}}"
out="${REPO_ROOT}/results/gcp/${study}/${run_id}"
[[ -f "${out}/meta.json" && ! -f "${out}/FAILED" ]] && die "${out} is already complete. Delete the folder to rerun it."
if [[ -d "$out" && -n "$(ls -A "$out")" ]]; then
  die "${out} has a partial or failed run. Look at it, delete the folder, then rerun."
fi
mkdir -p "$out"
effective_threshold="${threshold:-$QUEUE_THRESHOLD}"
baseline=$(( 1 + ${spare:-0} ))

WATCH_PIDS=()
cleanup() {
  local pid
  for pid in "${WATCH_PIDS[@]:-}"; do [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true; done
  stop_port_forward
}
on_exit() {
  local rc=$?
  cleanup
  if [[ "$rc" -ne 0 ]]; then
    echo "failed at $(date -u +%FT%TZ), exit code ${rc}" > "${out}/FAILED"
    warn "run ${run_id} FAILED. Partial output in ${out}. Check the cluster, then delete the folder and rerun."
    warn "If you are stopping for the day: scripts/gcp/scale_gpu_zero.sh"
  fi
}
trap on_exit EXIT

log "=== run ${study}/${run_id}: baseline ${baseline} GPU node(s) ==="
"${GCP_SCRIPTS}/reset_run.sh" "$baseline"
"${GCP_SCRIPTS}/use_strategy.sh" "$strategy" ${threshold:+"$threshold"}
sync_loadgen
pod=$(loadgen_pod)
[[ -n "$pod" ]] || die "no running loadgen pod"

# Watch vllm pods and GPU nodes for the whole run. Pods and nodes removed by scale down disappear
# from kubectl get afterwards, and coldstart.py needs their timestamps.
kubectl get pods -l app=vllm --watch --output-watch-events -o json > "${out}/.pod_watch.json" 2>/dev/null &
WATCH_PIDS+=($!)
kubectl get nodes -l "cloud.google.com/gke-accelerator=${GPU_TYPE}" --watch --output-watch-events -o json \
  > "${out}/.node_watch.json" 2>/dev/null &
WATCH_PIDS+=($!)

autoscaler_json=$(kubectl get hpa -o json)
remote="/results/${run_id}"
start=$(date +%s)
log "starting Locust SHAPE=${pattern} in ${pod}"
# Run Locust detached inside the pod, so a dropped laptop connection cannot kill the run.
kubectl exec "$pod" -- sh -c "rm -rf ${remote} && mkdir -p ${remote} && cd ${remote} && \
  (env SHAPE=${pattern} MODEL_ID='${MODEL_ID}' RESULTS_DIR=${remote} \
     locust -f /app/locustfile.py --headless --host http://vllm.default.svc:8000 \
       --only-summary --csv ${remote}/locust > ${remote}/locust.log 2>&1; \
   echo \$? > ${remote}/exit_code) > /dev/null 2>&1 &"

case "$pattern" in steady|ramp) expected=600 ;; spike) expected=720 ;; esac
while ! kubectl exec "$pod" -- test -f "${remote}/exit_code" 2>/dev/null; do
  elapsed=$(( $(date +%s) - start ))
  (( elapsed > expected + 300 )) && die "Locust still running after $((expected + 300)) s; see ${remote}/locust.log in ${pod}"
  replicas=$(kubectl get deployment vllm -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)
  log "t=${elapsed}s/${expected}s  vllm ready ${replicas:-0}  GPU nodes $(gpu_node_count)"
  sleep 30
done
locust_end=$(date +%s)
locust_exit=$(kubectl exec "$pod" -- cat "${remote}/exit_code" | tr -d '[:space:]')
[[ "$locust_exit" == "0" ]] || warn "Locust exit code ${locust_exit} (non zero means some requests failed; that is data, not an error)"

log "Locust done. Waiting 180 s with the autoscaler on, to record scale down"
sleep 180
end=$(date +%s)

log "copying results out of ${pod}"
for f in requests.csv locust.log locust_stats.csv locust_stats_history.csv locust_failures.csv; do
  kubectl cp "${pod}:${remote}/${f}" "${out}/${f}" >/dev/null 2>&1 || warn "could not copy ${f}"
done
[[ -s "${out}/requests.csv" ]] || die "requests.csv missing or empty; see ${out}/locust.log"

cleanup  # stop watchers before reading their files
WATCH_PIDS=()

log "exporting Prometheus metrics"
port_forward svc/monitoring-kube-prometheus-prometheus 9090:9090 monitoring /-/ready
python3 "${GCP_SCRIPTS}/export_metrics.py" --start "$start" --end "$end" --out "${out}/timeseries.csv" \
  --prom-url http://127.0.0.1:9090 --gpu-type "$GPU_TYPE"
stop_port_forward

log "saving events, pods and nodes"
kubectl get events -A -o json > "${out}/.events.json"
kubectl get pods -l app=vllm -o json > "${out}/.pods.json"
kubectl get nodes -o json > "${out}/.nodes.json"
python3 - "$out" <<'PY'
import json, sys
from pathlib import Path

out = Path(sys.argv[1])

def watch_stream(path: Path) -> list:
    """kubectl --watch -o json prints concatenated JSON objects, not a list."""
    text, dec, items, i = path.read_text(), json.JSONDecoder(), [], 0
    while i < len(text):
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            break
        try:
            obj, i = dec.raw_decode(text, i)
        except json.JSONDecodeError:
            break  # watch was killed mid object
        items.append(obj)
    return items

doc = {
    "platform": "gcp",
    "events": json.loads((out / ".events.json").read_text())["items"],
    "pods_at_end": json.loads((out / ".pods.json").read_text())["items"],
    "nodes_at_end": json.loads((out / ".nodes.json").read_text())["items"],
    "pod_watch": watch_stream(out / ".pod_watch.json"),
    "node_watch": watch_stream(out / ".node_watch.json"),
}
(out / "events.json").write_text(json.dumps(doc))
for f in (".events.json", ".pods.json", ".nodes.json", ".pod_watch.json", ".node_watch.json"):
    (out / f).unlink(missing_ok=True)
print(f"events.json: {len(doc['events'])} events, {len(doc['pod_watch'])} pod and {len(doc['node_watch'])} node watch updates")
PY

log "writing meta.json"
STUDY="$study" STRATEGY="$strategy" PATTERN="$pattern" REPEAT="$repeat" VARIANT="$variant" \
SPARE="${spare:-0}" THRESHOLD="$effective_threshold" RUN_START="$start" RUN_END="$end" \
LOCUST_END="$locust_end" LOCUST_EXIT="$locust_exit" AUTOSCALER_JSON="$autoscaler_json" \
GIT_COMMIT="$(git -C "$REPO_ROOT" rev-parse HEAD)" \
GIT_DIRTY="$(git -C "$REPO_ROOT" status --porcelain -- k8s scripts loadtest config/relay.env.example | wc -l | tr -d ' ')" \
python3 - "${out}/meta.json" <<'PY'
import json, os, sys
e = os.environ
meta = {
    "platform": "gcp",
    "study": e["STUDY"], "strategy": e["STRATEGY"], "pattern": e["PATTERN"],
    "repeat": int(e["REPEAT"]), "variant": e["VARIANT"],
    "spare_gpu_nodes": int(e["SPARE"]),
    "queue_threshold": float(e["THRESHOLD"]) if e["STRATEGY"] == "b" else None,
    "cpu_target_percent": float(e["CPU_TARGET_PERCENT"]) if e["STRATEGY"] == "a" else None,
    "start": int(e["RUN_START"]), "locust_end": int(e["LOCUST_END"]), "end": int(e["RUN_END"]),
    "locust_exit_code": int(e["LOCUST_EXIT"] or -1),
    "git_commit": e["GIT_COMMIT"], "git_dirty_files": int(e["GIT_DIRTY"]),
    "settings": {k: e.get(k) for k in [
        "MODEL_ID", "VLLM_TAG", "VLLM_IMAGE", "MAX_MODEL_LEN", "MAX_NUM_SEQS", "MIN_REPLICAS", "MAX_REPLICAS",
        "CPU_TARGET_PERCENT", "QUEUE_THRESHOLD", "CHECK_INTERVAL_S", "SCALE_DOWN_WINDOW_S", "SLO_TTFT_S",
        "LOCUST_TAG", "GCP_REGION", "GCP_ZONE", "GPU_ZONES", "CLUSTER_NAME", "GPU_POOL", "GPU_MACHINE",
        "GPU_TYPE", "GPU_PARTITION_SIZE", "GPU_POOL_MAX_NODES"]},
    "autoscaler": json.loads(e["AUTOSCALER_JSON"]).get("items", []),
}
with open(sys.argv[1], "w") as f:
    json.dump(meta, f, indent=2)
PY

python3 - "${out}/requests.csv" "${SLO_TTFT_S:-2}" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
slo = float(sys.argv[2])
bad = sum(1 for r in rows if r["ok"] != "1" or float(r["ttft_s"]) > slo)
print(f"requests {len(rows)}, failed {sum(r['ok'] != '1' for r in rows)}, SLO violation rate {bad / max(len(rows), 1):.3f}")
PY
log "=== done: ${out} ==="
