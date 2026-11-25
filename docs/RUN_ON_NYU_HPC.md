# Running Relay on NYU HPC Torch

## What HPC is good for here

Torch has free GPUs, so use it to build and test without spending GCP credits:

- Run vLLM and check that it answers
- Write and debug the Locust script
- Calibrate Strategy A and B thresholds on one GPU
- A fallback version of the autoscaling experiment if GCP GPUs are not available

Torch has no Kubernetes, so there is no HPA and no KEDA. The HPC version uses:

| GCP piece | Torch replacement |
| --- | --- |
| vLLM pod | One Slurm GPU job per replica, `hpc/vllm_replica.sbatch` |
| Kubernetes Service | `hpc/router.py`, a small round robin proxy |
| HPA and KEDA | `hpc/autoscaler.py`, which implements the same two rules and calls `sbatch` and `scancel` |
| Prometheus | The autoscaler scrapes each replica's metrics page and writes `timeseries.csv` |
| GPU node start | Slurm queue wait, which is its own cold start finding |

The GCP results are the main results. If you also run on Torch, present it as a second platform, not a replacement.

## 0. Access

1. You need an HPC account and an **active project allocation**. Torch jobs require `--account`; check yours with `my_slurm_accounts`. If you have none, ask the professor; allocations are registered by a sponsor at projects.hpc.nyu.edu, on the NYU VPN.
2. Connect to the NYU VPN, then SSH to Torch using the login address in the Torch New User Guide.
3. Keep code in `$HOME` and large files in `/scratch/$USER`. Scratch is not backed up and clears files unused for 60 days.

## 1. One time setup

```bash
cd ~ && git clone <your repo url> && cd relay-autoscaling
cp config/relay.env.example config/relay.env      # fill NETID, SLURM_ACCOUNT, VLLM_TAG
bash hpc/download_model.sh                        # pulls the vLLM SIF and model weights to /scratch
```

If the pull is slow or blocked on the login node, run it inside a short CPU job or on a data transfer node.

## 2. Test one replica interactively

```bash
source config/relay.env
srun --account="$SLURM_ACCOUNT" --gres=gpu:1 --constraint="$HPC_GPU_CONSTRAINT" \
     --cpus-per-task=4 --mem=32GB --time=1:00:00 --pty /bin/bash

# inside the GPU job
export HF_HOME=/scratch/$NETID/hf_cache HF_HUB_OFFLINE=1
apptainer exec --nv --bind /scratch/$NETID "$HPC_SIF" \
  vllm serve "$HPC_MODEL_DIR" --served-model-name "$MODEL_ID" --port 8000 --dtype float16 --max-model-len 4096 &
# wait for "Application startup complete", then:
curl localhost:8000/v1/chat/completions -H 'Content-Type: application/json' \
  -d "{\"model\":\"$MODEL_ID\",\"messages\":[{\"role\":\"user\",\"content\":\"Hello\"}],\"max_tokens\":32}"
curl -s localhost:8000/metrics | grep num_requests_waiting
```

## 3. Calibrate thresholds on one GPU

In the same job, run Locust headless against `localhost:8000` at 4, 16, 32 and 60 users for 2 minutes each. Record CPU use and waiting requests per level into `results/hpc/calibration.csv`. Use these numbers to set `CPU_TARGET_PERCENT` and `QUEUE_THRESHOLD`.

## 4. Full HPC experiment, fallback mode

1. Submit the control job, `hpc/control.sbatch`, a CPU job that runs `router.py`, `autoscaler.py` and Locust together.
2. The autoscaler starts with 1 replica job and adds or cancels `vllm_replica.sbatch` jobs, max 3.
3. Each replica registers `host:port` in `$RELAY_SCRATCH/registry/`; the router health checks every registered replica and only sends traffic to healthy ones.
4. Results land in `results/hpc/<study>/...` in the same format as GCP.

```bash
sbatch --account="$SLURM_ACCOUNT" hpc/control.sbatch main b spike 1
squeue -u "$NETID"                     # watch replica jobs appear and disappear
```

## Known limits to state in the report

- Slurm queue wait adds unpredictable delay to every scale up; record it separately.
- Replicas may land on different GPU types unless `--constraint` is set; always set it.
- Torch's per user GPU quota is large, but shared partitions can run out at busy times.
- Only cancel your own jobs: `scancel -u $NETID -n relay-vllm` cleans up all replicas.
