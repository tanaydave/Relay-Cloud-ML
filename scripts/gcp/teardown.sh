#!/usr/bin/env bash
# After the project ends: delete the GKE cluster and the Artifact Registry repo. Irreversible.
source "$(dirname "$0")/common.sh"

usage() {
  cat <<'USAGE'
Usage: scripts/gcp/teardown.sh [--yes]

Deletes cluster CLUSTER_NAME (all node pools, Prometheus data, Grafana) and
Artifact Registry repo AR_REPO. Asks you to type the cluster name unless --yes is given.
Copy anything you need out of the cluster first; results/ in git is not touched.
USAGE
}
handle_help "$@"
load_config
require_cmd gcloud

if [[ "${1:-}" != "--yes" ]]; then
  echo "This permanently deletes cluster '${CLUSTER_NAME}' and repo '${AR_REPO}' in project '${GCP_PROJECT_ID}'."
  read -r -p "Type the cluster name to confirm: " answer
  [[ "$answer" == "$CLUSTER_NAME" ]] || die "confirmation did not match, nothing deleted"
fi

if gcloud container clusters describe "$CLUSTER_NAME" --zone "$GCP_ZONE" >/dev/null 2>&1; then
  log "deleting cluster ${CLUSTER_NAME}"
  gcloud container clusters delete "$CLUSTER_NAME" --zone "$GCP_ZONE" --quiet
else
  log "cluster ${CLUSTER_NAME} not found, skipping"
fi

if gcloud artifacts repositories describe "$AR_REPO" --location "$GCP_REGION" >/dev/null 2>&1; then
  log "deleting Artifact Registry repo ${AR_REPO}"
  gcloud artifacts repositories delete "$AR_REPO" --location "$GCP_REGION" --quiet
else
  log "repo ${AR_REPO} not found, skipping"
fi
log "teardown complete. Check the Billing page in a day to confirm spend has stopped."
