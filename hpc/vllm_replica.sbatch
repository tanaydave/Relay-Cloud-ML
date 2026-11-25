#!/bin/bash
# One vLLM replica on one Torch GPU. The HPC autoscaler submits and cancels these jobs.
# Submit manually to test:   sbatch --account="$SLURM_ACCOUNT" hpc/vllm_replica.sbatch
#SBATCH --job-name=relay-vllm
#SBATCH --gres=gpu:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32GB
#SBATCH --time=04:00:00
#SBATCH --output=logs/vllm-%j.out

set -euo pipefail
source config/relay.env

# GPU type: pass --constraint on the sbatch command line if needed, e.g. --constraint=l40s
REGISTRY="${RELAY_SCRATCH}/registry"
mkdir -p "$REGISTRY" logs
PORT=$((20000 + RANDOM % 20000))
HOST=$(hostname)

export HF_HOME="/scratch/${NETID}/hf_cache"
export VLLM_CACHE_ROOT="/scratch/${NETID}/vllm_cache"
export HF_HUB_OFFLINE=1   # weights are pre-downloaded by hpc/download_model.sh

# Register this replica so the router can find it; the router also health checks it.
echo "${HOST}:${PORT}" > "${REGISTRY}/${SLURM_JOB_ID}"
cleanup() { rm -f "${REGISTRY}/${SLURM_JOB_ID}"; }
trap cleanup EXIT TERM INT

echo "job ${SLURM_JOB_ID} serving on ${HOST}:${PORT} at $(date -Is)"
apptainer exec --nv --bind "/scratch/${NETID}" "$HPC_SIF" \
  vllm serve "$HPC_MODEL_DIR" \
    --served-model-name "$MODEL_ID" \
    --host 0.0.0.0 --port "$PORT" \
    --max-model-len "$MAX_MODEL_LEN"
