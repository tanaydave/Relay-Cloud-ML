#!/bin/bash
# Control Job on CPU partition for Relay HPC Experiments.
# Runs router.py, autoscaler.py, and Locust together.
#
# Usage:
#   sbatch --account=csci_ga_3033_085-2026fa hpc/control.sbatch <study> <strategy> <pattern> <repeat> [threshold]
# Example:
#   sbatch --account=csci_ga_3033_085-2026fa hpc/control.sbatch main b spike 1
#
#SBATCH --job-name=relay-ctrl
#SBATCH --account=csci_ga_3033_085-2026fa
#SBATCH --partition=n2c48m24
#SBATCH --cpus-per-task=8
#SBATCH --mem=32GB
#SBATCH --time=01:30:00
#SBATCH --output=logs/control-%j.out

set -euo pipefail
source config/relay.env

STUDY="${1:-main}"
STRATEGY="${2:-b}"
PATTERN="${3:-spike}"
REPEAT="${4:-1}"
THRESHOLD="${5:-}"

TIMESTAMP_START=$(date -Is)
COMMIT_HASH=$(git rev-parse HEAD 2>/dev/null || echo "unknown")

RESULTS_DIR="results/hpc/${STUDY}/${STRATEGY}_${PATTERN}_r${REPEAT}"
if [ -n "$THRESHOLD" ]; then
  RESULTS_DIR="${RESULTS_DIR}_t${THRESHOLD}"
fi
mkdir -p "$RESULTS_DIR" logs

echo "=========================================================="
echo "Starting Relay HPC Experiment Run"
echo "Study:     $STUDY"
echo "Strategy:  $STRATEGY"
echo "Pattern:   $PATTERN"
echo "Repeat:    $REPEAT"
echo "Results:   $RESULTS_DIR"
echo "Time:      $TIMESTAMP_START"
echo "=========================================================="

ROUTER_PORT=$((15000 + RANDOM % 10000))
REGISTRY="${RELAY_SCRATCH}/registry"
mkdir -p "$REGISTRY"

# Ensure environment has locust, aiohttp, requests
module load python/3.11 2>/dev/null || true

# 1. Start Router in background
echo "[Control] Starting router on port $ROUTER_PORT..."
python3 hpc/router.py --port "$ROUTER_PORT" --registry "$REGISTRY" > "logs/router-${SLURM_JOB_ID}.log" 2>&1 &
ROUTER_PID=$!

# Cleanup trap
cleanup() {
  echo "[Control] Cleaning up background processes and replicas..."
  kill -TERM "$ROUTER_PID" 2>/dev/null || true
  kill -TERM "$AUTOSCALER_PID" 2>/dev/null || true
  scancel -u "$USER" -n relay-vllm 2>/dev/null || true
  rm -f "${REGISTRY}/*" 2>/dev/null || true
}
trap cleanup EXIT TERM INT

# 2. Start initial baseline replica (1 GPU)
echo "[Control] Submitting initial baseline replica..."
sbatch hpc/vllm_replica.sbatch

# Wait for 1 healthy replica in router
echo "[Control] Waiting for 1 replica to be healthy in router..."
while true; do
  STATUS=$(curl -s "http://127.0.0.1:${ROUTER_PORT}/router/status" || echo '{"count": 0}')
  COUNT=$(echo "$STATUS" | python3 -c "import sys, json; print(json.load(sys.stdin).get('count', 0))" 2>/dev/null || echo 0)
  if [ "$COUNT" -ge 1 ]; then
    echo "[Control] 1 replica is healthy and registered!"
    break
  fi
  sleep 5
done

# 3. Start Autoscaler in background
echo "[Control] Starting autoscaler for Strategy $STRATEGY..."
AUTOSCALER_ARGS="--strategy $STRATEGY --results-dir $RESULTS_DIR"
if [ -n "$THRESHOLD" ]; then
  AUTOSCALER_ARGS="$AUTOSCALER_ARGS --threshold $THRESHOLD"
fi
python3 hpc/autoscaler.py $AUTOSCALER_ARGS > "logs/autoscaler-${SLURM_JOB_ID}.log" 2>&1 &
AUTOSCALER_PID=$!

# 4. Run Locust Load Test
echo "[Control] Launching Locust with shape: $PATTERN..."
export RESULTS_DIR="$RESULTS_DIR"
export SHAPE="$PATTERN"
export MODEL_ID="$MODEL_ID"

locust -f loadtest/locustfile.py \
  --headless \
  --host "http://127.0.0.1:${ROUTER_PORT}" \
  --run-time 12m \
  --csv "${RESULTS_DIR}/locust" \
  --only-summary

echo "[Control] Locust finished. Waiting 180s stabilization window..."
sleep 180

# Stop Autoscaler cleanly to write timeseries.csv and events.json
kill -TERM "$AUTOSCALER_PID" 2>/dev/null || true
wait "$AUTOSCALER_PID" 2>/dev/null || true

TIMESTAMP_END=$(date -Is)

# Write meta.json
python3 -c "
import json
meta = {
    'platform': 'nyu_hpc_burst',
    'study': '${STUDY}',
    'strategy': '${STRATEGY}',
    'pattern': '${PATTERN}',
    'repeat': int('${REPEAT}'),
    'threshold': '${THRESHOLD}',
    'model_id': '${MODEL_ID}',
    'start_time': '${TIMESTAMP_START}',
    'end_time': '${TIMESTAMP_END}',
    'git_commit': '${COMMIT_HASH}'
}
with open('${RESULTS_DIR}/meta.json', 'w') as f:
    json.dump(meta, f, indent=2)
"

echo "=========================================================="
echo "Run Complete! Results saved to $RESULTS_DIR"
echo "=========================================================="
