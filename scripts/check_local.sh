#!/usr/bin/env bash
# Every check that needs no cloud account and costs nothing. Run before each commit and before GCP work.
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage: scripts/check_local.sh [--quick]

Runs, without touching any cloud resource:
  1. bash -n on every shell script, and --help on every scripts/gcp script
  2. python compile of every Python file
  3. renders every k8s template with config/relay.env.example and checks no ${VAR} is left
     (and parses the YAML if PyYAML is installed)
  4. checks that config/relay.env is not tracked by git
  5. mock vLLM server + smoke test + 30 s Locust run, checks requests.csv     (skipped by --quick)
  6. synthetic results + coldstart.py + make_charts.py, for L4 (6 PNGs) and A100 MIG (5 PNGs)  (skipped by --quick)
Steps 5 and 6 need: pip install -r requirements.txt
Prints PASS or FAIL per check and exits 1 if anything failed.
USAGE
}
for a in "$@"; do [[ "$a" == "-h" || "$a" == "--help" ]] && { usage; exit 0; }; done
quick=0
[[ "${1:-}" == "--quick" ]] && quick=1

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python3}"
TMP="$(mktemp -d)"
MOCK_PID=""
trap '[[ -n "$MOCK_PID" ]] && kill "$MOCK_PID" 2>/dev/null; rm -rf "$TMP"' EXIT
failures=0
pass() { printf '  PASS  %s\n' "$1"; }
fail() { printf '  FAIL  %s\n' "$1"; failures=$((failures + 1)); }
skip() { printf '  SKIP  %s\n' "$1"; }

echo "1. shell scripts"
while IFS= read -r f; do
  if bash -n "$f" 2>"$TMP/err"; then pass "bash -n $f"; else fail "bash -n $f: $(cat "$TMP/err")"; fi
done < <(find scripts hpc -name '*.sh' -o -name '*.sbatch' | sort)
for f in scripts/gcp/*.sh; do
  [[ "$f" == scripts/gcp/common.sh ]] && continue
  # --help exits before any cloud command; an empty PATH entry for cloud CLIs makes that certain
  if PATH="/usr/bin:/bin" "$f" --help >/dev/null 2>&1; then pass "--help $f"; else fail "--help $f"; fi
done

echo "2. python"
while IFS= read -r f; do
  if "$PY" -m py_compile "$f" 2>"$TMP/err"; then pass "compile $f"; else fail "compile $f: $(cat "$TMP/err")"; fi
done < <(find scripts analysis loadtest hpc -name '*.py' | sort)
if "$PY" -c "import json; json.load(open('analysis/relay_analysis.ipynb')); json.load(open('monitoring/relay-dashboard.json')); json.load(open('loadtest/prompts.json'))"; then
  pass "notebook, dashboard and prompts are valid JSON"
else
  fail "notebook, dashboard or prompts JSON"
fi

echo "3. kubernetes templates"
(
  set -a
  # shellcheck disable=SC1091
  source config/relay.env.example
  set +a
  export VLLM_TAG="v0.0.0-check" GCP_PROJECT_ID="check-project"
  export VLLM_IMAGE="${GCP_REGION}-docker.pkg.dev/${GCP_PROJECT_ID}/${AR_REPO}/vllm-openai:${VLLM_TAG}"
  for f in k8s/*.yaml; do
    "$PY" - "$f" > "$TMP/$(basename "$f")" <<'PY'
import os, re, sys
text = open(sys.argv[1]).read()
print(re.sub(r"\$\{(\w+)\}", lambda m: os.environ.get(m.group(1), m.group(0)), text), end="")
PY
  done
)
for f in "$TMP"/*.yaml; do
  name="k8s/$(basename "$f")"
  if grep -v '^\s*#' "$f" | grep -q '\${'; then fail "$name has an unset \${VAR}"; continue; fi
  if "$PY" -c "import yaml" 2>/dev/null; then
    if "$PY" -c "import sys, yaml; list(yaml.safe_load_all(open(sys.argv[1])))" "$f" 2>"$TMP/err"; then
      pass "$name renders and parses"
    else
      fail "$name YAML: $(tail -1 "$TMP/err")"
    fi
  else
    pass "$name renders (PyYAML not installed, parse skipped)"
  fi
done

echo "4. secrets"
if git ls-files --error-unmatch config/relay.env >/dev/null 2>&1; then
  fail "config/relay.env is tracked by git; run: git rm --cached config/relay.env"
else
  pass "config/relay.env is not tracked"
fi

if [[ "$quick" -eq 1 ]]; then
  skip "load test and analysis (--quick)"
else
  echo "5. load test against the mock server"
  if "$PY" -c "import locust" 2>/dev/null; then
    port=18765
    "$PY" loadtest/mock_server.py --port "$port" --max-seqs 8 >/dev/null 2>&1 &
    MOCK_PID=$!
    sleep 1
    if "$PY" loadtest/smoke_test.py --url "http://127.0.0.1:$port" --model "Qwen/Qwen2.5-1.5B-Instruct" >/dev/null; then
      pass "smoke test"
    else
      fail "smoke test"
    fi
    SHAPE=calibrate USERS=12 DURATION=30 RESULTS_DIR="$TMP/load" \
      "$PY" -m locust -f loadtest/locustfile.py --headless --host "http://127.0.0.1:$port" --only-summary \
      >"$TMP/locust.log" 2>&1 || true
    if "$PY" - "$TMP/load/requests.csv" <<'PY'
import csv, sys
rows = list(csv.DictReader(open(sys.argv[1])))
assert list(rows[0].keys()) == ["start", "ttft_s", "latency_s", "ok"], rows[0].keys()
ok = sum(r["ok"] == "1" for r in rows)
assert len(rows) > 50 and ok == len(rows), (len(rows), ok)
print(f"  {len(rows)} requests, all ok")
PY
    then pass "30 s Locust run wrote a correct requests.csv"; else fail "Locust run, see output above"; fi
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
    MOCK_PID=""
  else
    skip "locust not installed (pip install -r requirements.txt)"
  fi

  echo "6. analysis on synthetic data"
  if "$PY" -c "import pandas, matplotlib" 2>/dev/null; then
    if "$PY" analysis/make_synthetic.py >/dev/null && "$PY" analysis/coldstart.py --platform synthetic >/dev/null \
       && "$PY" analysis/make_charts.py --platform synthetic >/dev/null; then
      n=$(ls analysis/figures/synthetic/*.png 2>/dev/null | wc -l | tr -d ' ')
      if [[ "$n" -eq 6 ]]; then pass "6 charts in analysis/figures/synthetic/"; else fail "expected 6 charts, found $n"; fi
    else
      fail "synthetic analysis pipeline"
    fi
    rm -f analysis/figures/synthetic/*.png
    if "$PY" analysis/make_synthetic.py --mig >/dev/null && "$PY" analysis/make_charts.py --platform synthetic >/dev/null \
       && grep -q "gpu_slice_hours" analysis/per_run_metrics_synthetic.csv; then
      n=$(ls analysis/figures/synthetic/*.png 2>/dev/null | wc -l | tr -d ' ')
      if [[ "$n" -eq 5 ]]; then pass "A100 MIG synthetic: 5 charts (no warm spare)"; else fail "MIG: expected 5 charts, found $n"; fi
    else
      fail "A100 MIG synthetic analysis pipeline"
    fi
  else
    skip "pandas or matplotlib not installed (pip install -r requirements.txt)"
  fi
fi

echo
if [[ "$failures" -eq 0 ]]; then echo "ALL CHECKS PASSED"; else echo "$failures CHECK(S) FAILED"; exit 1; fi
