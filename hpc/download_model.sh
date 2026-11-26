#!/bin/bash
# One time setup on NYU HPC: pull the vLLM image as a SIF and download model weights to /scratch.
# Run from a login or data transfer node inside the repo folder:   bash hpc/download_model.sh
set -euo pipefail
source config/relay.env

# Load Apptainer or Singularity module if not already in PATH
if ! command -v apptainer &>/dev/null && ! command -v singularity &>/dev/null; then
  module load apptainer 2>/dev/null || module load singularity 2>/dev/null || true
fi

# Detect whether command is apptainer or singularity
if command -v apptainer &>/dev/null; then
  CONTAINER_CMD="apptainer"
elif command -v singularity &>/dev/null; then
  CONTAINER_CMD="singularity"
else
  echo "Error: Neither apptainer nor singularity found in PATH or modules."
  exit 1
fi

mkdir -p "$RELAY_SCRATCH" "$(dirname "$HPC_MODEL_DIR")" "/scratch/${NETID}/hf_cache" "/scratch/${NETID}/apptainer_cache" "/scratch/${NETID}/tmp"
export HF_HOME="/scratch/${NETID}/hf_cache"
export APPTAINER_CACHEDIR="/scratch/${NETID}/apptainer_cache"
export APPTAINER_TMPDIR="/scratch/${NETID}/tmp"
export SINGULARITY_CACHEDIR="/scratch/${NETID}/apptainer_cache"
export SINGULARITY_TMPDIR="/scratch/${NETID}/tmp"

if [ ! -f "$HPC_SIF" ]; then
  echo "Pulling container image with $CONTAINER_CMD to $HPC_SIF..."
  $CONTAINER_CMD pull "$HPC_SIF" "docker://vllm/vllm-openai:${VLLM_TAG}"
fi

echo "Downloading model weights for $MODEL_ID to $HPC_MODEL_DIR..."
$CONTAINER_CMD exec --bind "/scratch/${NETID}" "$HPC_SIF" python3 -c "
from huggingface_hub import snapshot_download
snapshot_download('${MODEL_ID}', local_dir='${HPC_MODEL_DIR}')
print('model ready at ${HPC_MODEL_DIR}')
"
