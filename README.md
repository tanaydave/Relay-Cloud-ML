# Relay: autoscaling LLM inference

Relay serves Qwen2.5 1.5B Instruct with vLLM and compares two autoscaling strategies under changing traffic:

- **Strategy A, resource based:** Kubernetes HPA on CPU use
- **Strategy B, load based:** KEDA on requests waiting in vLLM's queue

Main platform: Google Kubernetes Engine with L4 GPUs. Development and fallback platform: NYU HPC Torch.

Course project for Cloud and Machine Learning, NYU, Fall 2026.

## Start here

1. Copy `config/relay.env.example` to `config/relay.env` and fill it in.
2. Open Claude Code in this folder. It reads `CLAUDE.md` automatically.
3. Build the code with either:
   - `docs/PROMPTS_STEP_BY_STEP.md`, recommended, one prompt per step
   - `docs/PROMPT_ALL_AT_ONCE.md`, everything in one session
4. Run it with `docs/RUN_ON_GCP.md` or `docs/RUN_ON_NYU_HPC.md`.

## Layout

| Folder | What it holds |
| --- | --- |
| `k8s/` | GKE manifests: vLLM, ServiceMonitor, Strategy A HPA, Strategy B KEDA, Locust pod |
| `scripts/gcp/` | Cluster, deploy, strategy switch, experiment runner |
| `hpc/` | Slurm replica job, router, autoscaler, control job for Torch |
| `loadtest/` | Locust script and prompts |
| `analysis/` | Charts and summary tables |
| `monitoring/` | Grafana dashboard |
| `results/` | One folder per run |
| `docs/` | Run guides, prompts, file index |

## Cost warning

GPU nodes cost money every minute. Run `scripts/gcp/scale_gpu_zero.sh` at the end of every session, and `scripts/gcp/teardown.sh` after the project ends.
