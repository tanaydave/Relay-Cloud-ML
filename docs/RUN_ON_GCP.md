# Running Relay on GCP

GCP is the main platform. The graded autoscaling results come from here, because HPA and KEDA need Kubernetes.

## 0. Tools

Easiest: use **Google Cloud Shell** from the GCP console. It already has gcloud, kubectl, helm, docker and git.

On your own laptop instead, install:

| Tool | Install |
| --- | --- |
| gcloud CLI | cloud.google.com/sdk/docs/install |
| kubectl and GKE auth plugin | `gcloud components install kubectl gke-gcloud-auth-plugin` |
| Helm 3 | helm.sh/docs/intro/install |
| envsubst | part of `gettext`; `brew install gettext` on Mac, preinstalled on most Linux |
| crane | `go install github.com/google/go-containerregistry/cmd/crane@latest` or the release binary |
| Python 3.11 | python.org |
| Claude Code | `curl -fsSL https://claude.ai/install.sh \| bash` |

## 1. One time setup, Person 1

```bash
git clone <your repo url> && cd relay-autoscaling
cp config/relay.env.example config/relay.env      # fill in project ID and VLLM_TAG
set -a; source config/relay.env; set +a

gcloud auth login
gcloud config set project "$GCP_PROJECT_ID"
gcloud services enable container.googleapis.com artifactregistry.googleapis.com containerfilesystem.googleapis.com
```

Then in the console:

1. Billing, then Budgets: alerts at 50 and 90 percent.
2. Billing: a free trial account cannot use GPUs. Click Activate full account; the credits stay.
3. IAM and Admin, then Quotas. Default A100 plan: `GPUs (all regions)` at least 1 and `NVIDIA A100 GPUs` in your
   region at least 1. L4 plan: 4 of each.

Check which zones have the GPU, and put one of them in `GCP_ZONE` and `GPU_ZONES` (A100), or all of them in `GPU_ZONES` (L4):

```bash
gcloud compute accelerator-types list --filter="name=nvidia-tesla-a100 AND zone~us-central1"
gcloud compute accelerator-types list --filter="name=nvidia-l4 AND zone~us-central1"
```

The commands in sections 2 and 3 show the L4 version; `scripts/gcp/create_cluster.sh` and `scripts/gcp/gpu_proof.sh`
do the same with whatever GPU `relay.env` selects, including the A100 MIG slices.

## 2. Cluster and GPU pool

```bash
gcloud container clusters create "$CLUSTER_NAME" \
  --zone "$GCP_ZONE" --machine-type e2-standard-4 --num-nodes 2 \
  --enable-image-streaming --image-type COS_CONTAINERD

gcloud container node-pools create "$GPU_POOL" --cluster "$CLUSTER_NAME" --zone "$GCP_ZONE" \
  --node-locations "$GPU_ZONES" \
  --machine-type "$GPU_MACHINE" \
  --accelerator type=$GPU_TYPE,count=1,gpu-driver-version=latest \
  --num-nodes 0 --enable-autoscaling --total-min-nodes 0 --total-max-nodes 4

gcloud container clusters update "$CLUSTER_NAME" --zone "$GCP_ZONE" --autoscaling-profile optimize-utilization
gcloud container clusters get-credentials "$CLUSTER_NAME" --zone "$GCP_ZONE"
```

## 3. The 3 GPU proof, do this before anything else

```bash
# num-nodes is per zone: 1 per zone across 3 zones = 3 GPU nodes
gcloud container clusters resize "$CLUSTER_NAME" --node-pool "$GPU_POOL" --num-nodes 1 --zone "$GCP_ZONE"
kubectl get nodes -l cloud.google.com/gke-accelerator=$GPU_TYPE
# screenshot 3 Ready nodes for the professor, then:
gcloud container clusters resize "$CLUSTER_NAME" --node-pool "$GPU_POOL" --num-nodes 0 --zone "$GCP_ZONE"
```

## 4. Copy the vLLM image to Artifact Registry

Image streaming only works for images in Artifact Registry. crane copies registry to registry, so the large image never touches your disk.

```bash
gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location="$GCP_REGION"
gcloud auth configure-docker "${GCP_REGION}-docker.pkg.dev"
crane copy "docker.io/vllm/vllm-openai:${VLLM_TAG}" "$VLLM_IMAGE"
```

## 5. Serving and monitoring

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add kedacore https://kedacore.github.io/charts
helm repo update
helm install monitoring prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.scrapeInterval=15s
helm install keda kedacore/keda -n keda --create-namespace

envsubst < k8s/vllm.yaml | kubectl apply -f -
kubectl apply -f k8s/vllm-servicemonitor.yaml
kubectl get pods -w          # wait for vllm 1/1 Ready; first start takes several minutes

kubectl port-forward svc/vllm 8000:8000 &
curl localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}],\"max_tokens\":32}"
curl -s localhost:8000/metrics | grep -E "num_requests_waiting|time_to_first_token"
```

Grafana: `kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80`, user `admin`, password from
`kubectl get secret -n monitoring monitoring-grafana -o jsonpath="{.data.admin-password}" | base64 -d`.

## 6. Load generator

```bash
kubectl create configmap locust-files --from-file=loadtest/ --dry-run=client -o yaml | kubectl apply -f -
envsubst < k8s/loadgen.yaml | kubectl apply -f -
```

## 7. Run experiments

### 7a. One practice run first (about 25 minutes, 1 to 3 GPU nodes)

Do this once before any graded study. It goes into `results/gcp/practice/`, which the main analysis ignores.

```bash
scripts/gcp/reset_run.sh 1                          # optional: check the reset alone first, ends with "stable for 60 s"
scripts/gcp/run_experiment.sh practice b spike 1    # the whole run; prints progress every 30 s
python3 analysis/coldstart.py --platform gcp        # cold start rows for every finished run
```

Success looks like:
- the progress lines show `vllm ready` rising from 1 to 2 or 3 during the spike (strategy b), then falling after it
- the last lines print `requests N, failed 0, SLO violation rate ...` and `=== done: .../practice/b_spike_r1 ===`
- the folder holds `meta.json`, `requests.csv`, `timeseries.csv`, `events.json`, plus Locust's own CSVs and log
- `timeseries.csv` has about 60 rows and `replicas_ready`, `waiting` and `gpu_nodes` are not all 0
- `results/gcp/coldstart.csv` has one row per scale up, with node_start_s and model_load_s filled in

If `vllm ready` never rises above 1, look at `waiting` in `timeseries.csv`. If it stays at 0, Strategy B has
nothing to react to: lower `MAX_NUM_SEQS` in `relay.env`, rerun `scripts/gcp/deploy_serving.sh`, and recalibrate.

A failed run leaves a `FAILED` file. Look at the cluster, delete the folder, and rerun.

### 7b. The studies

```bash
scripts/gcp/run_study.sh main --dry-run   # prints the plan and what is already done, touches nothing
scripts/gcp/run_study.sh main             # 18 runs, about 9 to 10 hours; safe to stop and restart
scripts/gcp/run_study.sh warm             # 6 runs
scripts/gcp/run_study.sh sweep            # 6 runs
```

Run long studies inside `tmux` or Cloud Shell so a closed laptop does not stop them. Locust itself runs inside
the loadgen pod, so a dropped connection only stops the progress printing; rerun the study and finish that run by hand.

## 8. End of every work session

```bash
scripts/gcp/scale_gpu_zero.sh
```

## 9. After Dec 18

```bash
scripts/gcp/teardown.sh     # deletes the cluster and Artifact Registry repo
```
