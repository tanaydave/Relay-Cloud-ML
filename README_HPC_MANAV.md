# Relay HPC Execution Guide (Manav's Personal Quickstart)

Quick reference for running the full Relay experiment on **NYU HPC Burst (Open OnDemand)** using your class account and A100 GPU allocation.

---

## 1. Setup Python Dependencies in OOD Terminal

Before running Locust or the control job, install the required packages:

```bash
cd ~/Relay-Cloud-ML-main/Relay-Cloud-ML-main
pip install --user -r requirements.txt
```

---

## 2. Test 1: Quick Interactive Smoke Test

Ensure your single replica works on the A100:

```bash
# Submit replica job
sbatch hpc/vllm_replica.sbatch

# View live log
tail -f logs/vllm-*.out
```
*(Once it says `Application startup complete.`, press `Ctrl + C`)*.

Test endpoint:
```bash
export REPLICA_ENDPOINT=$(cat /scratch/$USER/relay/registry/* | head -n 1)
curl "http://${REPLICA_ENDPOINT}/v1/chat/completions" \
  -H "Content-Type: application/json" \
  -d '{"model":"Qwen/Qwen2.5-1.5B-Instruct","messages":[{"role":"user","content":"Hello"}],"max_tokens":32}'

# Cancel test replica
scancel -u $USER -n relay-vllm
rm -f /scratch/$USER/relay/registry/*
```

---

## 3. Run a Single Automated Experiment (e.g., Spike Traffic with Strategy B)

You can launch a full experiment run with a single `sbatch` command:

```bash
cd ~/Relay-Cloud-ML-main/Relay-Cloud-ML-main

# Command syntax:
# sbatch hpc/control.sbatch <study> <strategy: a|b> <pattern: steady|ramp|spike> <repeat_num> [threshold]

# Example: Run Study "main", Strategy B (Queue-based), Spike pattern, Repeat 1
sbatch hpc/control.sbatch main b spike 1
```

### What this automated job does:
1. Boots `hpc/router.py` on an available port.
2. Starts 1 baseline vLLM GPU replica and waits for it to become healthy.
3. Launches `hpc/autoscaler.py` in the background (monitoring metrics every 15s).
4. Runs Locust traffic (headless) across the router according to the selected shape.
5. Scales replicas up (submitting GPU jobs via `sbatch`) or down (via `scancel`).
6. Saves all output data directly into:
   ```text
   results/hpc/main/b_spike_r1/
     ├── meta.json
     ├── requests.csv
     ├── timeseries.csv
     └── events.json
   ```
7. Shuts down all replicas automatically at the end of the test!

---

## 4. Run the Full Study (All 18 Experiments)

To run the entire study automatically in sequence:

```bash
cd ~/Relay-Cloud-ML-main/Relay-Cloud-ML-main
bash hpc/run_study.sh main
```
This runs 3 repeats of `steady`, `ramp`, and `spike` traffic, alternating between Strategy A (CPU-based) and Strategy B (Queue-based).
