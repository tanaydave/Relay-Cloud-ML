# Relay: autoscaling LLM inference on GKE

Relay serves one language model, **Qwen/Qwen2.5-1.5B-Instruct in FP16**, with vLLM on Google Kubernetes Engine,
and compares two ways to autoscale it under changing traffic:

- **Strategy A, resource based:** Kubernetes HPA on the average CPU use of the vLLM pods, target 60 percent of the CPU request.
- **Strategy B, load based:** KEDA on `sum(vllm:num_requests_waiting)` divided by replicas, threshold 5 waiting per replica.

Both run 1 to 3 replicas with a 15 s check interval and a 120 s scale down window. By default the replicas share
**one NVIDIA A100 40GB split into 3 MIG slices**, one slice per replica, because that needs a quota of only 1 GPU.
One L4 per replica, the original design, works by switching the GPU block in `config/relay.env`.

**Research question:** does scaling on waiting requests keep response times low with fewer GPU hours than scaling on CPU use?

Course project for Cloud and Machine Learning, NYU, Fall 2026. Presentation Dec 10, report and code Dec 18.

## Architecture

```
 loadgen pod (Locust, CPU node)
        |  streaming chat requests, 128 tokens each
        v
 Service vllm  ------------------>  vllm pods: 1 to 3, one A100 MIG slice each (or one L4 each)
                                     |  /metrics every 15 s
 Prometheus  <-- ServiceMonitor -----+
   |   CPU use (cAdvisor)            |  vllm:num_requests_waiting
   v                                 v
 Strategy A: HPA vllm-cpu      Strategy B: KEDA ScaledObject vllm-queue
          \                       /
           scale Deployment vllm (only one strategy is active at a time)
                     |
 A100: all 3 slices live on 1 node, so a new replica only loads the model.
 L4: a pending pod makes the cluster autoscaler add a GPU node (pool 0 to 4 nodes, 3 zones).
```

Each run writes one folder: `results/gcp/<study>/<strategy>_<pattern>_r<repeat>[_<variant>]/` with
`meta.json`, `requests.csv`, `timeseries.csv` and `events.json` (format in `CLAUDE.md`).

## Prerequisites

| Need | Notes |
| --- | --- |
| GCP project with billing | Budget alerts at 50 and 90 percent |
| A paid billing account | Free trial accounts cannot use GPUs. Billing, then Activate full account; the free credits stay |
| GPU quota | A100 (default): `GPUs (all regions)` 1 and `NVIDIA A100 GPUs` in your region 1. L4: 4 of each. Request early: approval can take days |
| gcloud, kubectl, gke-gcloud-auth-plugin, helm 3, envsubst, crane | Google Cloud Shell has all but crane; see `docs/RUN_ON_GCP.md` section 0 |
| Python 3.11 | `python3.11 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt` |

## Verify without spending anything

```bash
scripts/check_local.sh
```

It syntax checks every script, renders and parses every manifest, runs Locust for 30 s against a fake vLLM server,
and runs the whole analysis on synthetic data. It should end with `ALL CHECKS PASSED`.
`docs/VERIFY.md` then walks through checking each GCP step on real infrastructure.

## Run on GCP from zero

Full details, expected output and troubleshooting: `docs/RUN_ON_GCP.md` and `docs/VERIFY.md`.

```bash
cp config/relay.env.example config/relay.env      # fill GCP_PROJECT_ID, VLLM_TAG (a pinned tag), chart versions
scripts/gcp/create_cluster.sh                     # cluster, L4 pool at 0 nodes, credentials
scripts/gcp/gpu_proof.sh                          # 3 GPUs for pods (3 A100 slices or 3 L4 nodes), then back to 0
scripts/gcp/copy_image.sh                         # vLLM image into Artifact Registry
scripts/gcp/install_monitoring.sh                 # Prometheus, Grafana, KEDA
scripts/gcp/deploy_serving.sh                     # vLLM up, prints one answer and its TTFT
scripts/gcp/check_metrics.sh                      # confirms the metric names the strategies use
scripts/gcp/import_dashboard.sh                   # Grafana dashboard "Relay autoscaling"
scripts/gcp/calibrate.sh                          # pick CPU_TARGET_PERCENT and QUEUE_THRESHOLD, edit relay.env
scripts/gcp/run_experiment.sh practice b spike 1  # one practice run, about 25 minutes
scripts/gcp/run_study.sh main                     # 18 runs; then sweep (6 runs); warm (6 runs) only with L4
scripts/gcp/scale_gpu_zero.sh                     # END OF EVERY SESSION
```

Every script has `--help`, reads settings only from `config/relay.env`, and is safe to rerun.
`run_study.sh` skips finished runs, so a study can be spread over several sessions.

## Reproduce every chart

```bash
python3 analysis/coldstart.py --platform gcp     # results/gcp/coldstart.csv
python3 analysis/make_charts.py                  # analysis/figures/*.png and analysis/summary_table.csv
jupyter notebook analysis/relay_analysis.ipynb   # the same tables and charts, interactively
```

| Chart | Shows |
| --- | --- |
| `1_spike_timeline.png` | one spike run per strategy: users, waiting, replicas, p95 TTFT on a shared time axis |
| `2_slo_violations.png` | share of requests with TTFT over 2 s (or failed), by strategy and pattern |
| `3_gpu_hours.png` | GPU cost per run, by strategy and pattern: GPU slice hours with A100 MIG, GPU hours with L4 |
| `4_coldstart.png` | where scale up time goes: decision, node start, image pull, model load |
| `5_warm_spare.png` | time to scale and GPU hours, with and without a warm spare node (L4 only) |
| `6_threshold_sweep.png` | Strategy B thresholds 2, 5, 10: SLO violations against GPU hours |

Every metric is defined once, in the docstring of `analysis/relay_data.py`.
To try the analysis before real runs: `python3 analysis/make_synthetic.py && python3 analysis/make_charts.py --platform synthetic`.
Synthetic numbers come from a toy model and must never appear in the report.

## Cost

Approximate on demand prices; check the GCP pricing page for your region.

- **A100 (default):** an `a2-highgpu-1g` node costs roughly $3.70 per hour while it exists. Main and sweep take about
  13.5 hours of run time, so plan for about 20 node hours with setup and calibration, roughly $75.
  **Never leave it running overnight**: one night is about $40.
- **L4:** a `g2-standard-4` node costs roughly $0.70 per hour; the three studies need about 25 to 40 node hours.
- The CPU node runs all the time while the cluster exists, roughly $3 per day.
- With $300 of credits the A100 plan fits with room for reruns, if GPU nodes are always scaled to 0 after a session.
- **Run `scripts/gcp/scale_gpu_zero.sh` at the end of every session.** It removes the autoscalers, scales vLLM to 0 and the GPU pool to 0.
- For a long break, also scale the CPU pool to 0:
  `gcloud container clusters resize relay --node-pool default-pool --num-nodes 0 --zone <zone>`. Prometheus history is lost then,
  but every finished run's metrics are already in `results/`.

## Teardown, after Dec 18

```bash
scripts/gcp/teardown.sh     # asks you to type the cluster name; deletes the cluster and the Artifact Registry repo
```

## Scope notes

- **GCP only.** The NYU HPC Torch fallback (router, autoscaler, control job) was not built; the team chose GCP for all
  results. `hpc/` still has single replica scripts for free GPU testing. See the note at the top of `docs/RUN_ON_NYU_HPC.md`.
- One model, two strategies, no quantization, as the professor asked.

## Layout

| Folder | What it holds |
| --- | --- |
| `config/` | `relay.env.example`; your `relay.env` is never committed |
| `k8s/` | vLLM, ServiceMonitor, Strategy A HPA, Strategy B KEDA, Locust pod |
| `scripts/gcp/` | cluster, serving, strategy switch, calibration, experiment and study runners |
| `scripts/check_local.sh` | every free local check |
| `loadtest/` | Locust script, prompts, smoke test, mock vLLM server |
| `analysis/` | cold start analysis, metrics, charts, notebook, synthetic data generator |
| `monitoring/` | Prometheus values and the Grafana dashboard |
| `results/` | one folder per run |
| `docs/` | run guide, verification guide, file index, build prompts |

Every file is described in `docs/FILES.md`.

## How this was built

The code was written with Claude Code, following `CLAUDE.md` and the prompts in `docs/PROMPTS_STEP_BY_STEP.md`.
The team reviews, runs and verifies every step (`docs/VERIFY.md`) and is responsible for the results.

## Load test, run locally

The Locust script works against any OpenAI compatible endpoint: the mock server, or a port-forwarded GKE Service.

```bash
python3 loadtest/mock_server.py --port 8000 --max-seqs 8 &          # fake vLLM, a queue builds above 8 users
python3 loadtest/smoke_test.py --url http://127.0.0.1:8000 --model "Qwen/Qwen2.5-1.5B-Instruct"
SHAPE=calibrate USERS=12 DURATION=30 RESULTS_DIR=results/local \
  locust -f loadtest/locustfile.py --headless --host http://127.0.0.1:8000 --only-summary
```

`SHAPE` is `steady`, `ramp`, `spike` or `calibrate` (see `CLAUDE.md`). With a shape active Locust ignores `-t`;
the shape decides when the test ends. Use `requests.csv` for TTFT and latency: in Locust's own table the `chat` row
only measures time to response headers, and the `ttft` row is the real time to first token.
