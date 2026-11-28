# Relay: Autoscaling LLM Inference

Relay serves `Qwen/Qwen2.5-1.5B-Instruct` with vLLM and compares two autoscaling strategies under changing traffic patterns:

- **Strategy A (Resource-based):** Scale on replica CPU utilization (replicating Kubernetes HPA).
- **Strategy B (Load-based):** Scale on waiting requests in vLLM's queue (`num_requests_waiting`, replicating KEDA).

Platforms:
- **Primary / Fallback:** NYU HPC Burst (Slurm + Singularity + A100/L4 GPUs).
- **Optional Cloud:** Google Kubernetes Engine (GKE + L4 GPUs).

---

## Quickstart for NYU HPC Burst (Open OnDemand)

For complete step-by-step instructions with exact commands, see:
👉 **[`README_HPC_MANAV.md`](README_HPC_MANAV.md)**

### Fast Execution Overview:

```bash
# 1. Setup & Dependencies
git pull origin manav
pip install --user -r requirements.txt
chmod +x hpc/*.sh

# 2. Download container & model to /scratch (one-time setup)
bash hpc/download_model.sh

# 3. Quick smoke test on 1 GPU
sbatch hpc/vllm_replica.sbatch
tail -f logs/vllm-*.out

# 4. Run an automated experiment run (e.g. Spike traffic with Strategy B)
sbatch hpc/control.sbatch main b spike 1

# 5. Run the entire 18-run study
bash hpc/run_study.sh main
```

---

## Repository Layout

| Directory / File | Description |
| :--- | :--- |
| `README_HPC_MANAV.md` | Complete end-to-end runbook for NYU HPC Burst |
| `config/relay.env.example` | Template for environment variables and cluster settings |
| `hpc/download_model.sh` | Pulls vLLM Singularity image and model weights to `/scratch` |
| `hpc/vllm_replica.sbatch` | Slurm batch script to run a vLLM replica on an A100 GPU |
| `hpc/router.py` | Reverse proxy load-balancing requests across active replicas |
| `hpc/autoscaler.py` | Controller implementing Strategy A (CPU) and Strategy B (Queue) |
| `hpc/control.sbatch` | Orchestration CPU job running router, autoscaler, and Locust |
| `hpc/run_study.sh` | Shell script iterating through all experiment runs in sequence |
| `loadtest/locustfile.py` | Streaming Locust load test with `steady`, `ramp`, and `spike` shapes |
| `loadtest/prompts.json` | 50 technical test prompts for LLM inference |
| `results/` | Output directory storing `meta.json`, `requests.csv`, `timeseries.csv`, `events.json` |
