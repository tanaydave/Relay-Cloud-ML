# File index

Every file, 2 to 3 lines each. Claude Code keeps this updated.

- `CLAUDE.md`: project rules for Claude Code. Scope, layout, experiment spec, conventions, safety rules.
- `README.md`: overview and where to start.
- `config/relay.env.example`: every setting for GCP and Torch. Copy to `config/relay.env`, which is never committed.
- `k8s/vllm.yaml`: vLLM Deployment and Service on L4 GPU nodes. A template filled with envsubst.
- `k8s/vllm-servicemonitor.yaml`: tells Prometheus to scrape vLLM metrics every 15 s.
- `k8s/strategy-a-hpa.yaml`: Strategy A, HPA on CPU use.
- `k8s/strategy-b-keda.yaml`: Strategy B, KEDA on waiting requests per replica.
- `k8s/loadgen.yaml`: Locust pod inside the cluster; runs start with kubectl exec.
- `hpc/vllm_replica.sbatch`: one vLLM replica as a Torch GPU job; registers host and port for the router.
- `hpc/download_model.sh`: one time pull of the vLLM SIF and model weights into /scratch.
- `docs/RUN_ON_GCP.md`: GCP run guide from zero to teardown.
- `docs/RUN_ON_NYU_HPC.md`: Torch run guide and how the HPC version differs.
- `docs/PROMPTS_STEP_BY_STEP.md`: Claude Code prompts, one per build step.
- `docs/PROMPT_ALL_AT_ONCE.md`: one Claude Code prompt that builds everything.
- `requirements.txt`: pinned Python packages for Locust, analysis, the HPC router and autoscaler. Locust version matches `LOCUST_TAG`.

### scripts/gcp/
- `scripts/gcp/common.sh`: sourced by every GCP script. Loads and checks `config/relay.env`, and holds helpers for --help, GPU node counts, pool resize and bounds, port-forward, removing autoscalers, refreshing the loadgen pod.
- `scripts/gcp/create_cluster.sh`: enables APIs, creates the cluster, the multi zone L4 pool (0 nodes, autoscaling 0 to `GPU_POOL_MAX_NODES`), sets optimize-utilization, gets credentials. Skips what exists.
- `scripts/gcp/gpu_proof.sh`: resizes the GPU pool to 1 node per zone, waits up to 15 min for all Ready, prints nodes and zones for the screenshot, then resizes to 0 (even on Ctrl-C).
- `scripts/gcp/copy_image.sh`: creates the Artifact Registry repo if missing and crane copies the pinned vLLM image into it, so GKE image streaming works. Prints both digests.
- `scripts/gcp/scale_gpu_zero.sh`: end of session. Deletes HPA and ScaledObject, scales vllm to 0, sets GPU pool min to 0 and resizes it to 0.
- `scripts/gcp/reset_run.sh <baseline>`: before each run. Removes autoscalers, 1 vllm replica, sets GPU pool min to baseline, uses temporary gpu-warmup pause pods to pull in a warm spare, waits until state is stable 60 s.
- `scripts/gcp/teardown.sh`: asks you to type the cluster name, then deletes the cluster and Artifact Registry repo.
- `scripts/gcp/install_monitoring.sh`: helm upgrade --install of kube-prometheus-stack (values in `monitoring/kps-values.yaml`) and KEDA. Chart versions from `relay.env`.
- `scripts/gcp/deploy_serving.sh`: applies the vLLM Deployment, Service and ServiceMonitor, waits for Ready, port-forwards and runs `loadtest/smoke_test.py` to print an answer and TTFT.
- `scripts/gcp/check_metrics.sh`: port-forwards vLLM and prints the queue, running, TTFT and CPU metric lines. Warns, and exits 1, if a metric name differs from CLAUDE.md.
- `scripts/gcp/import_dashboard.sh`: reads the Grafana admin password from its secret and uploads `monitoring/relay-dashboard.json` through the Grafana API.

### monitoring/
- `monitoring/kps-values.yaml`: kube-prometheus-stack settings. 15 s scrape, discover all ServiceMonitors, and export the GKE accelerator node label so the GPU node count query works.
- `monitoring/relay-dashboard.json`: Grafana dashboard, uid `relay`. Panels: replicas ready, waiting, running, p95 TTFT with a 2 s line, GPU node count, vLLM CPU cores.

### loadtest/
- `loadtest/smoke_test.py`: stdlib only. Sends one streaming chat request and prints the answer, TTFT and latency. Used by `deploy_serving.sh`; also works on Torch.
- `scripts/gcp/prom.py`: stdlib Prometheus client. `query_range` and `mean` functions for other scripts, plus a CLI that prints a mean or a time,value CSV.
- `scripts/gcp/use_strategy.sh <a|b|none> [threshold]`: switches the autoscaler. Deletes the other one first, applies the HPA or KEDA template, and refuses to leave both active.
- `scripts/gcp/calibrate.sh`: no autoscaler, 1 replica, Locust at 4, 16, 32, 60 users for 120 s. Writes `results/gcp/calibration.csv` with CPU percent of request, waiting, running, TTFT and SLO share per level. `--help` explains how to pick both thresholds.
