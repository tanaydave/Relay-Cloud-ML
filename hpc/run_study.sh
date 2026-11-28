#!/bin/bash
# Runs every experiment for a study in sequence on NYU HPC.
# Usage:
#   bash hpc/run_study.sh <main|sweep|warm>
#
set -euo pipefail
source config/relay.env

STUDY="${1:-main}"

echo "=========================================================="
echo "Relay HPC Study Runner: $STUDY"
echo "=========================================================="

run_one() {
  local strat="$1"
  local pat="$2"
  local rep="$3"
  local thresh="${4:-}"

  local target_dir="results/hpc/${STUDY}/${strat}_${pat}_r${rep}"
  if [ -n "$thresh" ]; then
    target_dir="${target_dir}_t${thresh}"
  fi

  if [ -f "${target_dir}/requests.csv" ]; then
    echo "[Skip] Run ${strat}_${pat}_r${rep} already completed in ${target_dir}"
    return 0
  fi

  echo ">>> Submitting Job: Study=$STUDY Strategy=$strat Pattern=$pat Repeat=$rep Threshold=$thresh"
  local job_id=$(sbatch --parsable hpc/control.sbatch "$STUDY" "$strat" "$pat" "$rep" "$thresh")
  echo "Job ID: $job_id queued. Waiting for completion..."

  while squeue -j "$job_id" 2>/dev/null | grep -q "$job_id"; do
    sleep 30
  done
  echo "Job $job_id finished!"
  sleep 60
}

if [ "$STUDY" == "main" ]; then
  # 18 runs: strategies a and b, patterns steady ramp spike, 3 repeats each
  for rep in 1 2 3; do
    for pat in steady ramp spike; do
      # Alternate strategies A and B
      run_one "a" "$pat" "$rep"
      run_one "b" "$pat" "$rep"
    done
  done

elif [ "$STUDY" == "sweep" ]; then
  # Threshold sweep: strategy b, spike, thresholds 2, 5, 10, 2 repeats each
  for rep in 1 2; do
    for thresh in 2 5 10; do
      run_one "b" "spike" "$rep" "$thresh"
    done
  done

elif [ "$STUDY" == "warm" ]; then
  # Warm spare comparison: strategy b, spike, 3 repeats
  for rep in 1 2 3; do
    run_one "b" "spike" "$rep"
  done

else
  echo "Unknown study: $STUDY. Expected 'main', 'sweep', or 'warm'."
  exit 1
fi

echo "=========================================================="
echo "Study $STUDY completed successfully!"
echo "=========================================================="
