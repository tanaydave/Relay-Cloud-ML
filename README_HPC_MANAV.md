# Relay HPC Execution Guide (Complete End-to-End Runbook)

This guide contains **every single step and exact command from start to finish** to run the complete Relay experiment on **NYU HPC Burst (Open OnDemand)** using your class account: `csci_ga_3033_085-2026fa`.

---

## Architecture Summary (How It Runs on HPC)

```text
                           [ control.sbatch (CPU Node: n2c48m24) ]
                                          │
                  ┌───────────────────────┼───────────────────────┐
                  ▼                       ▼                       ▼
          [ hpc/router.py ]      [ hpc/autoscaler.py ]    [ Locust Loadgen ]
         (Port: 15000-25000)      (Checks every 15s)    (Steady/Ramp/Spike)
                  │                       │                       │
                  │              ┌────────┴────────┐              │
                  │              ▼                 ▼              │
                  │        Scale UP:         Scale DOWN:          │
                  │        sbatch GPU       scancel <jid>         │
                  │              │                                │
                  └──────────────┼────────────────────────────────┘
                                 ▼
                     [ 1 to 3 vLLM Replicas ]
                     (A100 GPU: c12m85-a100-1)
                     (Registered in /scratch/$USER/relay/registry)
```

---

## Pre-requisites: Disk & Account Rules
1. **Always stay in your repo folder**: `~/Relay-Cloud-ML-main/Relay-Cloud-ML-main`.
2. **Never store models or images in `$HOME`**: The scripts automatically redirect all caches, container `.sif`, and HuggingFace weights to `/scratch/$USER` to protect your `$HOME` disk quota.
3. **GPU Allocation**: Your class account has **300 GPU hours**. The scripts use **NVIDIA A100 40GB** (`c12m85-a100-1`) or **2x L4** (`g2-standard-24`).

---

## Phase 1: Initial Login, Code Setup & Dependencies

Open an Open OnDemand terminal (**Clusters -> >_Burst Shell Access** or a CPU Jupyter terminal) and run:

```bash
# 1. Step into the project folder
cd ~/Relay-Cloud-ML-main/Relay-Cloud-ML-main

# 2. Make sure you have the latest code from the manav branch
git pull origin manav

# 3. Create config/relay.env if it doesn't exist
if [ ! -f config/relay.env ]; then
  cp config/relay.env.example config/relay.env
  sed -i "s/REPLACE_WITH_YOUR_NETID/$USER/g" config/relay.env
  sed -i 's/REPLACE_WITH_PINNED_VERSION/v0.6.3.post1/g' config/relay.env
  sed -i 's/SLURM_ACCOUNT="REPLACE"/SLURM_ACCOUNT="csci_ga_3033_085-2026fa"/g' config/relay.env
fi

# 4. Make all scripts executable
chmod +x hpc/*.sh

# 5. Load Python 3.11 module and install dependencies (locust, aiohttp, requests, pandas, etc.)
module load python/3.11 2>/dev/null || true
pip install --user -r requirements.txt
```

---

## Phase 2: One-Time Download of Singularity Image & Model to `/scratch`

This downloads the vLLM Singularity Image File (`.sif`) and model weights into `/scratch/$USER`.

> **Note**: If you already ran this earlier and `/scratch/$USER/relay/vllm-openai_v0.6.3.post1.sif` exists, this step will quickly skip the container download and verify model files.

```bash
bash hpc/download_model.sh
```

To verify the files were downloaded properly to `/scratch`:
```bash
ls -lh /scratch/$USER/relay/vllm-openai_v0.6.3.post1.sif
ls -lh /scratch/$USER/models/Qwen2.5-1.5B-Instruct/
```

---

## Phase 3: Quick 2-Minute Smoke Test (Verify GPU Inference)

Ensure that 1 replica starts cleanly on the A100 GPU and serves completions:

```bash
# 1. Clean registry and submit a test replica
rm -f /scratch/$USER/relay/registry/*
sbatch hpc/vllm_replica.sbatch

# 2. Check that the job is in squeue
squeue -u $USER

# 3. Follow the live startup log
tail -f logs/vllm-*.out
```
*(Wait ~45 seconds until you see `Application startup complete.` or `Uvicorn running on socket...`. Then press `Ctrl + C`)*.

Test the endpoint:
```bash
export REPLICA_ENDPOINT=$(cat /scratch/$USER/relay/registry/* | head -n 1)
echo "Testing replica at: http://${REPLICA_ENDPOINT}"

# Send test chat prompt
curl "http://${REPLICA_ENDPOINT}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-1.5B-Instruct",
    "messages": [{"role": "user", "content": "Explain autoscaling in 15 words."}],
    "max_tokens": 32
  }'

# Test Prometheus queue metrics (used for Strategy B)
curl -s "http://${REPLICA_ENDPOINT}/metrics" | grep num_requests_waiting

# Stop the test replica to free the GPU
scancel -u $USER -n relay-vllm
rm -f /scratch/$USER/relay/registry/*
```

---

## Phase 4: Run a Single Automated Experiment Run

You can run any single test using the automated control job (`hpc/control.sbatch`).

### Command Syntax:
```bash
sbatch hpc/control.sbatch <study> <strategy> <pattern> <repeat> [threshold]
```
- `<study>`: `main`, `warm`, or `sweep`
- `<strategy>`: `a` (CPU-based) or `b` (Queue-based)
- `<pattern>`: `steady`, `ramp`, or `spike`
- `<repeat>`: `1`, `2`, or `3`
- `[threshold]`: Optional threshold override (e.g., `2`, `5`, or `10`)

### Example 1: Run Strategy B (Queue-based) with Spike Traffic
```bash
sbatch hpc/control.sbatch main b spike 1
```

### Example 2: Run Strategy A (CPU-based) with Spike Traffic
```bash
sbatch hpc/control.sbatch main a spike 1
```

### Monitoring the Experiment in Real-Time:
```bash
# 1. Watch all jobs (control job, router, and dynamically scaled GPU replicas):
squeue -u $USER

# 2. Follow the control job log:
tail -f logs/control-*.out
```

When Locust finishes, the control job automatically:
1. Waits for the 180s stabilization window.
2. Stops the autoscaler and writes `timeseries.csv` and `events.json`.
3. Records metadata and commit hash into `meta.json`.
4. Cancels all running GPU replicas so no GPU hours are wasted.

---

## Phase 5: Run the Entire 18-Run Study Automatically

To execute the entire main study (all 18 runs: 3 repeats × 3 patterns × 2 strategies) in sequence:

```bash
bash hpc/run_study.sh main
```

*(This submits one test at a time, waits for it to complete, sleeps 60s, and moves to the next. If any run was already completed, it automatically skips it!)*

To run the threshold sweep study (thresholds 2, 5, 10):
```bash
bash hpc/run_study.sh sweep
```

---

## Phase 6: Viewing Results & Analyzing Data

All results are automatically organized into:
```text
results/hpc/<study>/<strategy>_<pattern>_r<repeat>/
  ├── meta.json         # Settings, timestamps, git commit
  ├── requests.csv      # TTFT, latency, and success flag per request
  ├── timeseries.csv    # Replicas, queue length, CPU, running requests every 15s
  └── events.json       # Slurm scale-up submit, queue-wait, and scale-down events
```

### Quick CLI Summary of a Run:
```bash
# View sample requests with TTFT and Latency:
head -n 10 results/hpc/main/b_spike_r1/requests.csv

# View timeseries scaling history:
head -n 20 results/hpc/main/b_spike_r1/timeseries.csv

# View run metadata:
cat results/hpc/main/b_spike_r1/meta.json
```

---

## Emergency Clean-Up (Stop Everything Immediately)

If you need to stop all experiments and release all GPU nodes:

```bash
scancel -u $USER -n relay-ctrl
scancel -u $USER -n relay-vllm
rm -f /scratch/$USER/relay/registry/*
squeue -u $USER
```
