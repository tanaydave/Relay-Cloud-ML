# Relay: project rules for Claude Code

Read this file fully before writing any code. It is the single source of truth for scope, layout and conventions.

## What Relay is

Relay serves one language model, Qwen/Qwen2.5-1.5B-Instruct in FP16, with vLLM, and compares two autoscaling strategies under changing traffic. It is a 3 person course project for Cloud and Machine Learning at NYU. Presentation is Dec 10, 2026; final report and code are due Dec 18, 2026.

Research question: does scaling on waiting requests keep response times low with fewer GPU hours than scaling on CPU use?

## Scope rules

- ONE model only: Qwen/Qwen2.5-1.5B-Instruct, FP16. Do not add quantized variants, a quality benchmark, or other models. The professor explicitly asked to drop quantization.
- TWO strategies only, plus an optional stretch strategy C if asked:
  - Strategy A, resource based: scale on average CPU use of the vLLM replicas, target 60 percent, after calibration.
  - Strategy B, load based: scale on `sum(vllm:num_requests_waiting)` divided by replicas, threshold 5 waiting per replica.
  - Both: 1 to 3 replicas, 15 s check interval, 120 s scale down stabilization.
- No custom gateway. Locust talks to the vLLM Service on GCP, or to a tiny round robin router on HPC.
- No Terraform, no CI pipeline. Plain bash and Python.

## Two platforms

1. GCP, the primary platform for graded results: GKE, one L4 GPU per replica, HPA for Strategy A, KEDA for Strategy B, kube-prometheus-stack for metrics.
2. NYU HPC Torch, for free GPU development, calibration, and a fallback experiment: Slurm jobs, Apptainer, one GPU job per replica, our own Python autoscaler implementing the same two strategies, and a small Python router.

Decision, Nov 25, 2026: all graded results come from GCP. The HPC router, autoscaler and control job are NOT built; do not build them unless asked. `hpc/` keeps only single replica testing.

Shared code must not assume one platform. Platform specific code lives in `k8s/` and `scripts/gcp/` for GCP, and `hpc/` for Torch.

## Repo layout

```
CLAUDE.md                 this file
README.md                 how to run everything, written for a new teammate
config/relay.env.example  every setting; copy to config/relay.env, never commit relay.env
k8s/                      GKE manifests
scripts/gcp/              GCP and GKE scripts
hpc/                      Slurm scripts, router, autoscaler for Torch
loadtest/                 locustfile.py, prompts.json, shapes
analysis/                 notebook and chart scripts
monitoring/               Grafana dashboard JSON
results/                  one folder per run, committed
report/                   report and slides material
docs/                     run guides and prompts
```

## Experiment spec

Traffic patterns, every request: 128 output tokens, temperature 0, ignore_eos true, prompts from `loadtest/prompts.json`.

| Pattern | Shape | Length |
| --- | --- | --- |
| steady | 8 users | 600 s |
| ramp | 2 rising evenly to 60 users | 600 s |
| spike | 4 users, 60 users from 120 s to 420 s, then 4 users until 720 s | 720 s |

Studies:

1. Main: strategies a and b, patterns steady ramp spike, 3 repeats each, 18 runs.
2. Cold start breakdown: derived from every scale up in studies 1 and 3.
3. Warm spare: strategy b, spike, 0 or 1 spare GPU node, 3 repeats each.
4. Threshold sweep: strategy b, spike, thresholds 2 5 10, 2 repeats each.

Results layout, one folder per run:

```
results/<platform>/<study>/<strategy>_<pattern>_r<repeat>[_<variant>]/
  meta.json        all settings, start and end times, git commit, platform
  requests.csv     start, ttft_s, latency_s, ok
  timeseries.csv   time, replicas_ready, waiting, running, cpu, gpu_nodes, p95_ttft
  events.json      Kubernetes events or Slurm job timeline, for cold start analysis
```

Metrics: SLO violation rate is the share of requests with ttft over 2 s; also p50 and p95 latency, failed requests, time to scale, GPU hours.

## Coding conventions

- Bash: `set -euo pipefail`, every script has `--help`, reads settings from `config/relay.env`, is safe to rerun.
- Python 3.11, standard library plus requirements listed in `requirements.txt`. Type hints, small functions, `argparse` CLIs.
- No hard coded project IDs, accounts, NetIDs or regions. Everything comes from `config/relay.env`.
- Never commit secrets, keys, tokens or `config/relay.env`.
- Pin the vLLM image tag in `relay.env`; never use `latest` in experiments.
- Every new file gets a 2 to 3 line entry in `docs/FILES.md` explaining what it does, so every teammate can answer questions about it.

## Safety rules for Claude Code

- Do not create, resize or delete cloud resources or submit Slurm jobs without asking first. Write the scripts; let a human run anything that costs money or uses an allocation.
- After writing a step, print exactly how to test it and what output means success.
- If a metric name, flag or API differs from this file in the installed version, say so and update this file.
