# Verifying Relay, step by step

Work top to bottom. Part 1 is free. Part 2 uses GCP and costs money from step 2.1 on; each step says roughly how much.
Stop at the first step whose success check fails, fix it, and only then go on.
Part 3 lists what could not be tested without real infrastructure, so you know what to look at closely.

---

## Part 1. Local, free (about 5 minutes)

```bash
python3.11 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt
scripts/check_local.sh
```

**Success:** every line says `PASS` and the last line is `ALL CHECKS PASSED`.

Then look at the synthetic charts in `analysis/figures/synthetic/`. They come from a toy model, so the numbers mean
nothing, but each chart should be readable from the back of a room: labeled axes with units, a legend, no overlaps.
Also open `analysis/relay_analysis.ipynb`, set `PLATFORM = "synthetic"`, and run all cells.

Optional: `scripts/gcp/run_study.sh main --dry-run` prints the 18 planned runs in their order without touching anything.

---

## Part 2. GCP, in order

Before step 2.1: copy `config/relay.env.example` to `config/relay.env` and fill in `GCP_PROJECT_ID`, a pinned
`VLLM_TAG` (a release tag from hub.docker.com/r/vllm/vllm-openai, never `latest`), `KPS_CHART_VERSION` and
`KEDA_CHART_VERSION`. Check that GPU quota is approved (README, Prerequisites): for the default A100 plan the
Quotas page must show at least 1 for both `GPUs (all regions)` and `NVIDIA A100 GPUs` in your region.
Check the A100 is offered in your zone: `gcloud compute accelerator-types list --filter="name=nvidia-tesla-a100 AND zone~us-central1"`,
and put one of those zones in both `GCP_ZONE` and `GPU_ZONES`.

### 2.1 Cluster. About 10 minutes; the CPU nodes bill from now on
```bash
scripts/gcp/create_cluster.sh
```
**Success:** ends with `kubectl get nodes` showing the Ready CPU node(s) and `done. Next: scripts/gcp/gpu_proof.sh`.
Rerun it once: every step should say `already exists, skipping`.

### 2.2 Three GPU proof. About 10 minutes, GPU node(s) briefly
```bash
scripts/gcp/gpu_proof.sh
```
**Success:** `SUCCESS: 3 GPUs available for 3 replicas`. With the A100 the tables show 1 node, partition size
`2g.10gb`, and `GPUS_FOR_PODS` = 3. With L4 they show 3 nodes in 3 zones, 1 GPU each. Screenshot it for the professor.
Then `kubectl get nodes` should show the GPU node(s) going away within a few minutes.
**If it fails:** no capacity in that zone (try another zone that offers the GPU), or with MIG the slices are not
ready yet: rerun with `--keep`, wait 5 minutes, check `GPUS_FOR_PODS` again, then run `scale_gpu_zero.sh`.

### 2.3 Image copy. About 5 minutes, free apart from storage
```bash
scripts/gcp/copy_image.sh
```
**Success:** the source and target digests printed at the end are identical.

### 2.4 Monitoring. About 5 minutes
```bash
scripts/gcp/install_monitoring.sh
```
**Success:** `helm list -A` shows `monitoring` and `keda` as deployed; all pods in namespaces `monitoring` and `keda` are Running.

### 2.5 Serving. 10 to 20 minutes, 1 L4 node from here until you scale to zero
```bash
scripts/gcp/deploy_serving.sh
scripts/gcp/check_metrics.sh
```
**Success:** `deploy_serving.sh` prints an `answer:` in English, `ttft_s:` below about 0.5, and `OK`.
`check_metrics.sh` prints `found vllm:num_requests_waiting`, `found vllm:num_requests_running` and
`found vllm:time_to_first_token_seconds_bucket`, and exits 0.
**If a metric is missing:** the pinned vLLM version renamed it. Update the name in `k8s/strategy-b-keda.yaml`,
`scripts/gcp/export_metrics.py`, `monitoring/relay-dashboard.json` and `CLAUDE.md`.
Also check the startup log for FP16: `kubectl logs deploy/vllm | grep -i dtype` should mention `float16`.

### 2.6 Dashboard. 2 minutes
```bash
scripts/gcp/import_dashboard.sh
kubectl port-forward -n monitoring svc/monitoring-grafana 3000:80   # then open http://127.0.0.1:3000/d/relay
```
**Success:** the import prints `"status":"success"`. All 7 panels show data. **GPU node count** should read 1, and
**GPUs or MIG slices held by vLLM** should read 1 with one replica.
If it is empty, kube-state-metrics is not exporting the node label: check `monitoring/kps-values.yaml` was applied.

### 2.7 Calibration. About 15 minutes, 1 L4 node
```bash
scripts/gcp/calibrate.sh
```
**Success:** `results/gcp/calibration.csv` has 4 rows (4, 16, 32, 60 users) with numbers in every column.
Read the `--help` text to pick the thresholds, then write them into `config/relay.env` and note why in the report.
Two things to look for:
- `avg_waiting` should rise with users. **If it stays 0 even at 60 users, Strategy B can never scale.** Lower
  `MAX_NUM_SEQS` (try 16 or 32), rerun `deploy_serving.sh` and calibrate again. Record the value used.
- `cpu_pct_of_request` should rise with users. If it is flat, or already high at 4 users, CPU is a poor signal for this
  workload. That is a real finding for Strategy A, but choose the target carefully and say so in the report.

### 2.8 Strategy switch. 2 minutes
```bash
scripts/gcp/use_strategy.sh a && scripts/gcp/use_strategy.sh b && scripts/gcp/use_strategy.sh none
```
**Success:** after `a`, `kubectl get hpa` lists only `vllm-cpu`; after `b`, only `keda-hpa-vllm-queue`
(KEDA's own HPA) and `kubectl get scaledobject` shows `vllm-queue` READY True; after `none`, nothing.
With `b`, `kubectl get hpa` should show a TARGETS value like `0/5 (avg)`, not `<unknown>`.

### 2.9 Reset. 5 to 15 minutes
```bash
scripts/gcp/reset_run.sh 1
scripts/gcp/reset_run.sh 2     # warm spare; then run reset_run.sh 1 again
```
**Success:** ends with `stable for 60 s`. The second command (warm spare) is only for L4; skip it with the A100.
With 2, `kubectl get nodes` shows 2 GPU nodes and no `gpu-warmup` pod.
Extra nodes from a previous run are removed by the cluster autoscaler, which can take 10 minutes or more.

### 2.10 Practice run. About 25 minutes, 1 to 3 L4 nodes
```bash
scripts/gcp/run_experiment.sh practice b spike 1
python3 analysis/coldstart.py --platform gcp
```
**Success:**
- the progress lines show `vllm ready` rising above 1 during the spike, then falling after it
- it ends with `requests N, failed 0, SLO violation rate ...` and `=== done ===`
- `results/gcp/practice/b_spike_r1/` has `meta.json`, `requests.csv`, `timeseries.csv`, `events.json`
- `timeseries.csv`: about 60 rows; `replicas_ready`, `waiting` and `gpu_nodes` are not all 0
- `events.json` contains `pod_watch` and `node_watch` entries (open it and search for `"pod_watch"`)
- `results/gcp/coldstart.csv` has one row per scale up with `model_load_s` filled (`node_start_s` is a few seconds
  with the A100, since the slice is already there; about 2 minutes with L4)
- `timeseries.csv` column `gpu_slices` follows the replica count

Repeat once with strategy `a` (`run_experiment.sh practice a spike 1`).

### 2.11 Studies. About 17 hours in total, 1 to 3 L4 nodes
```bash
scripts/gcp/run_study.sh main     # 18 runs, about 9 to 10 hours
scripts/gcp/run_study.sh sweep    # 6 runs
scripts/gcp/run_study.sh warm     # 6 runs, L4 only; with the A100 it refuses, which is expected
python3 analysis/make_charts.py
```
**Success:** every run folder has `meta.json` and no `FAILED`. `analysis/figures/` has the 6 charts.
Run inside `tmux` or Cloud Shell. If a run fails, look at the `FAILED` file and the cluster, delete that folder, and rerun the study.

### 2.12 End of every session
```bash
scripts/gcp/scale_gpu_zero.sh
```
**Success:** `kubectl get nodes -l cloud.google.com/gke-accelerator=nvidia-l4` shows no nodes after a few minutes.

---

## Part 3. Assumptions to confirm on real infrastructure

These were designed carefully but could only be tested on fake data or not at all.

| # | Assumption | Where it is checked |
| --- | --- | --- |
| 1 | The pinned vLLM version exports `vllm:num_requests_waiting`, `vllm:num_requests_running`, `vllm:time_to_first_token_seconds` | 2.5 `check_metrics.sh` |
| 2 | 60 users make requests wait at the default `MAX_NUM_SEQS=256`. **This is the biggest risk:** if nothing waits, Strategy B never scales | 2.7 calibration |
| 3 | vLLM's CPU use tracks load. The vLLM engine may keep a core busy even when idle, which would make Strategy A scale up at once or never | 2.7 calibration |
| 4 | `--dtype float16` is accepted and the model fits easily on an L4 | 2.5 logs |
| 5 | kube-state-metrics exports `label_cloud_google_com_gke_accelerator` with the allowlist in `kps-values.yaml` | 2.6 dashboard |
| 6 | Setting the GPU pool's total minimum plus pause pods gives exactly 1 or 2 GPU nodes | 2.9 reset |
| 7 | `kubectl get --watch --output-watch-events -o json` output parses into `pod_watch` and `node_watch` | 2.10 events.json |
| 8 | KEDA's scaling shows up as `SuccessfulRescale` events on its HPA, which `coldstart.py` uses as the decision time | 2.10 coldstart.csv |
| 9 | Kubernetes keeps events for about 1 hour. Fine, because each run saves them at its end; do not export events later | design |
| 10 | The `locustio/locust:2.32.3` image exists and has `sh` | 2.7, first loadgen start |
| 11 | Each new vLLM pod downloads the 3 GB of weights from Hugging Face at start, so model load includes the download. Anonymous downloads may be rate limited | 2.10 model_load_s |
| 12 | HPA's own CPU reading (metrics-server) is close to the `cpu` column (Prometheus cAdvisor). They are computed differently | compare `kubectl get hpa` with `timeseries.csv` |
| 13 | When the spike drops at 420 s, Locust stops users mid request and those requests are not recorded. Small bias, the same for both strategies | known limit, mention in report |
| 14 | `p95_ttft` uses a 1 minute rate window, so the timeline lags the true TTFT by up to a minute. Use `requests.csv` for the headline numbers | known limit |
| 15 | HPA checks every 15 s on GKE by default and this cannot be changed; KEDA's `pollingInterval` is set to 15 s to match | design |
| 16 | GKE creates the A100 node with 3 `2g.10gb` MIG slices and offers them as `nvidia.com/gpu: 3` | 2.2 `gpu_proof.sh` |
| 17 | vLLM runs on a 10 GB MIG slice with its default memory setting (model about 3 GB, the rest for KV cache) | 2.5 logs |
| 18 | The 3 slices give independent capacity, so a second replica really adds throughput | 2.7 and 2.10: TTFT drops when replicas rise |
| 19 | One `e2-standard-4` CPU node fits Prometheus, Grafana, KEDA and Locust. If pods stay Pending, set `CPU_NODES=2` | 2.4 |

## Choices made while building, which differ slightly from the original prompts

- vLLM is forced to FP16 (`--dtype float16`); otherwise it would load Qwen in BF16, which is not what CLAUDE.md specifies.
- `MAX_NUM_SEQS`, `LOCUST_TAG`, chart versions and GPU pool size were added to `relay.env`, so nothing is hard coded or unpinned.
- Chart 1 overlays Strategy A and B in each of 4 rows (users, waiting, replicas, p95 TTFT) instead of drawing A's
  panels above B's. Same data, easier to compare on a slide.
- The warm spare is created with the pool's total minimum plus temporary pause pods, because `gcloud resize` works
  per zone and cannot target 1 or 2 nodes in total.
- The NYU HPC autoscaling version was not built (GCP only, the team's decision).
- After L4 quota was denied, the default GPU became one A100 split into 3 MIG slices. With it, the cold start has
  no node start or image pull, cost is GPU slice hours, and the warm spare study is skipped. State this in the report.
