#!/bin/bash
# One time setup on Torch: pull the vLLM image as a SIF and download model weights to /scratch.
# Run from a login or data transfer node inside the repo folder:   bash hpc/download_model.sh
set -euo pipefail
source config/relay.env

mkdir -p "$RELAY_SCRATCH" "$(dirname "$HPC_MODEL_DIR")" "/scratch/${NETID}/hf_cache"
export HF_HOME="/scratch/${NETID}/hf_cache"
export APPTAINER_CACHEDIR="/scratch/${NETID}/apptainer_cache"

if [ ! -f "$HPC_SIF" ]; then
  apptainer pull "$HPC_SIF" "docker://vllm/vllm-openai:${VLLM_TAG}"
fi

apptainer exec "$HPC_SIF" python3 -c "
from huggingface_hub import snapshot_download
snapshot_download('${MODEL_ID}', local_dir='${HPC_MODEL_DIR}')
print('model ready at ${HPC_MODEL_DIR}')
"
