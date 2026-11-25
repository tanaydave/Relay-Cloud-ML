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
