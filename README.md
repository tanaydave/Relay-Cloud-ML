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

## Load test, run locally

The Locust script works against any OpenAI compatible endpoint: the mock server, a Torch test replica, or a port-forwarded GKE Service.

```bash
python3.11 -m venv .venv && . .venv/bin/activate && pip install -r requirements.txt

# 1. no GPU: start the mock server (8 concurrent slots, so a queue builds above 8 users)
python3 loadtest/mock_server.py --port 8000 --max-seqs 8 &

# 2. one request, prints the answer and time to first token
python3 loadtest/smoke_test.py --url http://127.0.0.1:8000 --model "Qwen/Qwen2.5-1.5B-Instruct"

# 3. 30 s at 12 users; writes results/local/requests.csv
SHAPE=calibrate USERS=12 DURATION=30 RESULTS_DIR=results/local \
  locust -f loadtest/locustfile.py --headless --host http://127.0.0.1:8000 --only-summary
```

`SHAPE` is `steady`, `ramp`, `spike` or `calibrate` (see CLAUDE.md). With a shape active Locust ignores `-t`; the shape decides when the test ends.
For a Torch replica, replace the host with `http://<node>:<port>` from `$RELAY_SCRATCH/registry/`, or tunnel it with `ssh -L`.
Use `requests.csv` for TTFT and latency. In Locust's own table the `chat` row only measures time to response headers; the `ttft` row is our TTFT.
