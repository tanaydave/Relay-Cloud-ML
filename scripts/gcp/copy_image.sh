#!/usr/bin/env bash
# Copy the pinned vLLM image from Docker Hub to Artifact Registry with crane (registry to registry).
# Image streaming only works for Artifact Registry images. Safe to rerun: skips if already copied.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/copy_image.sh

Creates Artifact Registry repo AR_REPO in GCP_REGION if missing, configures docker auth for it,
and crane copies docker.io/vllm/vllm-openai:VLLM_TAG to VLLM_IMAGE.
USAGE
}
handle_help "$@"
load_config
require_cmd gcloud crane

if gcloud artifacts repositories describe "$AR_REPO" --location "$GCP_REGION" >/dev/null 2>&1; then
  log "Artifact Registry repo ${AR_REPO} exists"
else
  log "creating Artifact Registry repo ${AR_REPO}"
  gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location="$GCP_REGION"
fi
gcloud auth configure-docker "${GCP_REGION}-docker.pkg.dev" --quiet

src="docker.io/vllm/vllm-openai:${VLLM_TAG}"
if crane digest "$VLLM_IMAGE" >/dev/null 2>&1; then
  log "image already present: ${VLLM_IMAGE}"
else
  log "copying ${src} -> ${VLLM_IMAGE} (several GB, a few minutes)"
  crane copy "$src" "$VLLM_IMAGE"
fi
echo "source digest: $(crane digest "$src")"
echo "target digest: $(crane digest "$VLLM_IMAGE")"
log "done. The two digests above should match."
