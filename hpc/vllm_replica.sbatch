#!/bin/bash
# One vLLM replica node on NYU HPC Burst (A100 or L4 GPU).
# Submit command:
#   sbatch --account=csci_ga_3033_085-2026fa --partition=c12m85-a100-1 --gres=gpu:a100:1 hpc/vllm_replica.sbatch
#SBATCH --job-name=relay-vllm
#SBATCH --account=csci_ga_3033_085-2026fa
#SBATCH --partition=c12m85-a100-1
#SBATCH --gres=gpu:a100:1
#SBATCH --cpus-per-task=4
#SBATCH --mem=32GB
#SBATCH --time=02:00:00
#SBATCH --output=logs/vllm-%j.out

set -euo pipefail
source config/relay.env

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

# Load container runtime if not already in PATH
if ! command -v singularity &>/dev/null && ! command -v apptainer &>/dev/null; then
  module load singularity 2>/dev/null || module load apptainer 2>/dev/null || true
fi
CONTAINER_CMD=$(command -v singularity || command -v apptainer)

# Launch vLLM server
$CONTAINER_CMD exec --nv --bind "/scratch/${NETID}" "$HPC_SIF" \
  vllm serve "$HPC_MODEL_DIR" \
    --served-model-name "$MODEL_ID" \
    --host 0.0.0.0 --port "$PORT" \
    --max-model-len "$MAX_MODEL_LEN"
