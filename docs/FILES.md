# File index

Every file in the repo, 2 to 3 lines each, so every teammate can answer questions about it. Keep it updated.

## Top level
- `CLAUDE.md`: project rules for Claude Code and the team. Scope, layout, experiment spec, conventions, safety rules. The source of truth when docs disagree.
- `README.md`: what Relay is, architecture, prerequisites, how to run on GCP from zero, how to reproduce the charts, cost and teardown.
- `requirements.txt`: pinned Python packages for the load test and analysis. The Locust version must match `LOCUST_TAG` in `relay.env`.
- `.gitignore`: keeps `config/relay.env`, keys, Python caches, job logs and all synthetic test output out of git.

## config/
- `config/relay.env.example`: every setting (model, vLLM tag, strategy thresholds, GCP project and zones, pinned versions). The GPU block defaults to one A100 split into 3 MIG slices; a commented block switches to one L4 per replica. Copy to `config/relay.env`, which is never committed.

## k8s/ (templates, filled with envsubst by the scripts)
- `k8s/vllm.yaml`: vLLM Deployment and Service. One GPU per pod (an A100 MIG slice or an L4), FP16, `--max-num-seqs` from config, a 2 CPU request (needed for Strategy A), readiness on `/health`.
- `k8s/vllm-servicemonitor.yaml`: tells Prometheus to scrape every vLLM pod's `/metrics` every 15 s.
- `k8s/strategy-a-hpa.yaml`: Strategy A. HPA `vllm-cpu` on average CPU use against the request, 1 to 3 replicas, 120 s scale down window.
- `k8s/strategy-b-keda.yaml`: Strategy B. KEDA ScaledObject `vllm-queue` on `sum(vllm:num_requests_waiting)`; KEDA divides by replicas, so the threshold is per replica.
- `k8s/loadgen.yaml`: Locust pod on the CPU pool with the load test files mounted from a ConfigMap. Runs are started inside it, so laptop networks never affect results.

## scripts/
- `scripts/check_local.sh`: every check that needs no cloud: bash and Python syntax, `--help` on every script, renders and parses the manifests, a 30 s Locust run on the mock server, and the synthetic analysis. Prints PASS or FAIL.

### scripts/gcp/
- `scripts/gcp/common.sh`: sourced by every GCP script. Loads and checks `config/relay.env`, and holds helpers for --help, GPU node counts, pool resize and bounds, port-forward, removing autoscalers, refreshing the loadgen pod.
- `scripts/gcp/create_cluster.sh`: enables APIs, creates the cluster and the GPU pool (0 nodes, autoscaling 0 to `GPU_POOL_MAX_NODES`, MIG slices if `GPU_PARTITION_SIZE` is set), sets optimize-utilization, gets credentials. Skips what exists; stops if an existing pool has a different GPU.
- `scripts/gcp/gpu_proof.sh`: resizes the GPU pool to 1 node per zone, waits up to 15 min until 3 GPUs are offered to pods (3 A100 slices on 1 node, or 3 L4 nodes), prints nodes, zones and GPU counts for the screenshot, then resizes to 0 (even on Ctrl-C).
- `scripts/gcp/copy_image.sh`: creates the Artifact Registry repo if missing and crane copies the pinned vLLM image into it, so GKE image streaming works. Prints both digests.
- `scripts/gcp/install_monitoring.sh`: helm upgrade --install of kube-prometheus-stack (values in `monitoring/kps-values.yaml`) and KEDA. Chart versions from `relay.env`.
- `scripts/gcp/deploy_serving.sh`: applies the vLLM Deployment, Service and ServiceMonitor, waits for Ready, port-forwards and runs `loadtest/smoke_test.py` to print an answer and TTFT.
- `scripts/gcp/check_metrics.sh`: port-forwards vLLM and prints the queue, running, TTFT and CPU metric lines. Warns, and exits 1, if a metric name differs from CLAUDE.md.
- `scripts/gcp/import_dashboard.sh`: reads the Grafana admin password from its secret and uploads `monitoring/relay-dashboard.json` through the Grafana API.
- `scripts/gcp/use_strategy.sh <a|b|none> [threshold]`: switches the autoscaler. Deletes the other one first, applies the HPA or KEDA template, and refuses to leave both active.
- `scripts/gcp/calibrate.sh`: no autoscaler, 1 replica, Locust at 4, 16, 32, 60 users for 120 s. Writes `results/gcp/calibration.csv` with CPU percent of request, waiting, running, TTFT and SLO share per level. `--help` explains how to pick both thresholds.
- `scripts/gcp/prom.py`: stdlib Prometheus client. `query_range` and `mean` functions for other scripts, plus a CLI that prints a mean or a time,value CSV.
- `scripts/gcp/export_metrics.py`: queries Prometheus at a 15 s step for one run's time window and writes `timeseries.csv` (time, replicas_ready, waiting, running, cpu as percent of request, gpu_nodes, p95_ttft, gpu_slices).
- `scripts/gcp/reset_run.sh <baseline>`: before each run. Removes autoscalers, 1 vllm replica, sets GPU pool min to baseline, uses temporary gpu-warmup pause pods to pull in a warm spare, waits until state is stable 60 s.
- `scripts/gcp/run_experiment.sh <study> <strategy> <pattern> <repeat> [--spare] [--threshold]`: one full run. Resets, picks the strategy, runs Locust detached in the loadgen pod, waits 180 s, then collects requests, metrics, events and meta into the run folder. Watches pods and nodes so scaled down pods still appear in `events.json`.
- `scripts/gcp/run_study.sh <main|warm|sweep> [--dry-run]`: runs a whole study in counterbalanced order, skips finished runs (folders with meta.json), stops at partial ones, and prints estimated time left. Refuses warm with MIG slices.
- `scripts/gcp/scale_gpu_zero.sh`: end of session. Deletes HPA and ScaledObject, scales vllm to 0, sets GPU pool min to 0 and resizes it to 0.
- `scripts/gcp/teardown.sh`: asks you to type the cluster name, then deletes the cluster and Artifact Registry repo.

## loadtest/
- `loadtest/prompts.json`: 50 varied, realistic user prompts, 107 to 137 words each (about 150 to 200 tokens), so input length is similar across requests.
- `loadtest/locustfile.py`: closed loop Locust users posting streaming chat requests (128 tokens, temperature 0, ignore_eos). Records TTFT at the first chunk with text, fires a `ttft` event, writes `RESULTS_DIR/requests.csv` on stop. `SHAPE` env picks steady, ramp, spike or calibrate.
- `loadtest/smoke_test.py`: stdlib only. Sends one streaming chat request and prints the answer, TTFT and latency. Used by `deploy_serving.sh`.
- `loadtest/mock_server.py`: stdlib fake vLLM with streaming, `/health` and `/metrics`. A `--max-seqs` limit makes `vllm:num_requests_waiting` rise under load, for testing without a GPU.

## monitoring/
- `monitoring/kps-values.yaml`: kube-prometheus-stack settings. 15 s scrape, discover all ServiceMonitors, and export the GKE accelerator and MIG partition node labels so the GPU node count query works.
- `monitoring/relay-dashboard.json`: Grafana dashboard, uid `relay`. Panels: replicas ready, waiting, running, p95 TTFT with a 2 s line, GPU node count, vLLM CPU cores, GPUs or MIG slices held by vLLM.

## analysis/
- `analysis/coldstart.py --platform gcp`: for every scale up in every run, splits the time to a ready replica into decision, node start, image pull, model load and other. Writes `results/<platform>/coldstart.csv`.
- `analysis/relay_data.py`: loads run folders and computes per run metrics (SLO violation rate, p50 and p95 TTFT and latency, GPU hours, GPU slice hours, replica hours, time to scale). Its docstring is the single definition of every metric, including which cost measure the charts use.
- `analysis/make_charts.py`: writes the 6 slide ready PNGs to `analysis/figures/`, plus `analysis/summary_table.csv` and `analysis/per_run_metrics.csv`. Skips charts whose study has no runs yet.
- `analysis/relay_analysis.ipynb`: notebook view of the same tables and charts; set `PLATFORM` in the first cell.
- `analysis/make_synthetic.py [--mig]`: writes FAKE runs from a toy simulation to `results/synthetic/` (git ignored) so the analysis can be tested before real runs: 30 runs with one L4 per replica, or 24 with `--mig` (one A100, 3 slices, no warm spare). Never report these numbers.

## docs/
- `docs/VERIFY.md`: step by step verification: free local checks first, then each GCP step with its command, cost and what success looks like, then the list of assumptions to confirm on real infrastructure.
- `docs/RUN_ON_GCP.md`: GCP run guide from zero to teardown, including the practice run.
- `docs/RUN_ON_NYU_HPC.md`: the original Torch plan. Only single replica testing is built; see the note at its top.
- `docs/PROMPTS_STEP_BY_STEP.md`: the Claude Code prompts used to build the repo, one per step.
- `docs/PROMPT_ALL_AT_ONCE.md`: one Claude Code prompt that builds everything.
- `docs/FILES.md`: this file.

## hpc/ (single replica testing on NYU Torch only)
- `hpc/download_model.sh`: one time pull of the vLLM SIF and model weights into /scratch.
- `hpc/vllm_replica.sbatch`: one vLLM replica as a Torch GPU job, FP16; registers host and port in `$RELAY_SCRATCH/registry`.

## Empty folders kept with .gitkeep
- `results/`: one folder per run, `results/gcp/<study>/<run>/`, committed. `results/synthetic/` is ignored.
- `report/`: report and slides material.
- `logs/`: Slurm job logs, ignored except the placeholder.
