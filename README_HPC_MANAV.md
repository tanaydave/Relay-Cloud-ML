# Relay HPC Execution Guide (Manav's Personal Quickstart)

Quick, foolproof reference for running Relay on **NYU HPC Burst (Open OnDemand)** using your class account and A100 GPU allocation.

---

## 1. Cluster & Account Details

- **Account:** `csci_ga_3033_085-2026fa`
- **Fastest GPU Partition (1x NVIDIA A100 40GB):** `c12m85-a100-1` (`--gres=gpu:a100:1`)
- **Code Directory:** `~/Relay-Cloud-ML-main/Relay-Cloud-ML-main`
- **Large Files / SIF Image / Model:** `/scratch/mp7264/` (Protects $HOME quota)

---

## 2. The Final Exact Commands

### Step 1: Submit the Replica Job
Inside your OOD terminal:

```bash
cd ~/Relay-Cloud-ML-main/Relay-Cloud-ML-main

# Cancel any old failed jobs and clean registry
scancel -u $USER -n relay-vllm 2>/dev/null || true
rm -f /scratch/$USER/relay/registry/*

# Submit replica with 1x A100 GPU
sbatch hpc/vllm_replica.sbatch
```
*(It prints `Submitted batch job <JOBID>`)*.

---

### Step 2: Check Status & Follow Live Logs

```bash
# Check status in queue
squeue -u $USER

# Follow live output (replace <JOBID> with the job number from squeue)
tail -f logs/vllm-*.out
```

> **Note on Slurm states:**
> - `CF` = Google Cloud is powering up the node (takes ~2–4 minutes).
> - `R` = Node is running; vLLM is starting.
> 
> Once you see:
> `INFO: Application startup complete.`
> `INFO: Uvicorn running on http://0.0.0.0:<PORT>`
> Press `Ctrl + C` to stop watching the log.

---

### Step 3: Run Inference & Prometheus Metrics Test

```bash
# Capture the host:port from the registry
export REPLICA_ENDPOINT=$(cat /scratch/$USER/relay/registry/* | head -n 1)
echo "Testing replica at: http://${REPLICA_ENDPOINT}"

# 1. Send test prompt (Chat Completion)
curl "http://${REPLICA_ENDPOINT}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "Qwen/Qwen2.5-1.5B-Instruct",
    "messages": [{"role": "user", "content": "Explain autoscaling in 15 words."}],
    "max_tokens": 32
  }'

# 2. Test Prometheus Waiting Queue Metric (Strategy B)
curl -s "http://${REPLICA_ENDPOINT}/metrics" | grep num_requests_waiting
```

---

### Step 4: Shut Down Replica (Save GPU Allocation Hours)

When done testing:

```bash
# Cancel running job
scancel -u $USER -n relay-vllm

# Clean registry
rm -f /scratch/$USER/relay/registry/*

# Confirm job is stopped
squeue -u $USER
```
