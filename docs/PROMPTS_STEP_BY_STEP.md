# Claude Code prompts, step by step

Paste one prompt at a time into Claude Code from the repo root. After each one: run the test it prints, then commit.
Owner in brackets is who usually runs that prompt. Every prompt assumes Claude Code has read `CLAUDE.md`.

---

## Prompt 0: orientation  [anyone, first]

```
Read CLAUDE.md, docs/RUN_ON_GCP.md and docs/RUN_ON_NYU_HPC.md fully. Then:
1. Create requirements.txt with pinned versions for: locust, requests, pandas, matplotlib, numpy, jupyter, prometheus-client, aiohttp, fastapi, uvicorn, httpx.
2. Create docs/FILES.md listing every existing file with a 2 to 3 line explanation.
3. Create empty folders with .gitkeep: scripts/gcp, loadtest, analysis, monitoring, results, report.
4. Summarize back to me in 10 bullets what Relay is and what you will build, so I can confirm you understood.
Do not create cloud resources.
```

## Prompt 1: GCP cluster scripts  [Person 1]

```
Following CLAUDE.md and docs/RUN_ON_GCP.md sections 1 to 4, write these bash scripts in scripts/gcp/. Each one sources config/relay.env through a shared scripts/gcp/common.sh, uses set -euo pipefail, has --help, and is safe to rerun:
- create_cluster.sh: cluster, multi zone GPU pool, autoscaling profile, get-credentials. Skip steps that already exist.
- gpu_proof.sh: resize GPU pool to 1 node per zone, wait until all are Ready or 15 minutes pass, print node names and zones, then resize back to 0.
- copy_image.sh: create the Artifact Registry repo if missing and crane copy the pinned vLLM image.
- scale_gpu_zero.sh: delete any HPA or ScaledObject, scale deployment vllm to 0, resize GPU pool to 0.
- reset_run.sh <baseline_gpu_nodes>: remove autoscalers, scale vllm to 1, resize GPU pool so exactly <baseline_gpu_nodes> GPU nodes exist, wait until vllm is Ready and the node count is stable for 60 s.
- teardown.sh: ask for confirmation, then delete the cluster and Artifact Registry repo.
Update docs/FILES.md. Tell me the order to run them and what success looks like. Do not run them.
```

## Prompt 2: serving and monitoring  [Person 2 and Person 3]

```
Following CLAUDE.md and docs/RUN_ON_GCP.md section 5:
1. scripts/gcp/install_monitoring.sh: installs kube-prometheus-stack and KEDA with the exact settings in the doc. Idempotent with helm upgrade --install.
2. scripts/gcp/deploy_serving.sh: envsubst k8s/vllm.yaml and apply, apply the ServiceMonitor, wait for readiness, then port-forward and send one test chat request and print the answer and the time to first token.
3. scripts/gcp/check_metrics.sh: port-forwards vLLM and prints every metric line containing num_requests_waiting, num_requests_running, time_to_first_token, process_cpu. If names differ from CLAUDE.md, print a clear warning.
4. monitoring/relay-dashboard.json: a Grafana dashboard with panels for replicas ready, waiting requests, running requests, p95 time to first token, GPU node count, vLLM CPU use. Use the PromQL in this list:
   kube_deployment_status_replicas_available{deployment="vllm"}
   sum(vllm:num_requests_waiting)
   sum(vllm:num_requests_running)
   histogram_quantile(0.95, sum(rate(vllm:time_to_first_token_seconds_bucket[1m])) by (le))
   count(kube_node_labels{label_cloud_google_com_gke_accelerator="nvidia-l4"})
   sum(rate(container_cpu_usage_seconds_total{container="vllm"}[1m]))
5. scripts/gcp/import_dashboard.sh that loads the JSON into Grafana through its HTTP API.
Update docs/FILES.md and tell me how to test each piece.
```

## Prompt 3: the two strategies  [Person 2]

```
Following CLAUDE.md, write scripts/gcp/use_strategy.sh <a|b|none> [threshold]:
- a: delete the KEDA ScaledObject if present, envsubst and apply k8s/strategy-a-hpa.yaml
- b: delete the HPA if present, envsubst and apply k8s/strategy-b-keda.yaml, using the threshold argument if given
- none: delete both
- Finally print kubectl get hpa so it is obvious which autoscaler is active. Refuse to leave both active.
Also write scripts/gcp/calibrate.sh: with no autoscaler and 1 replica, runs Locust from the loadgen pod at 4, 16, 32, 60 users for 120 s each, and records average vLLM CPU percent of request and average waiting requests per level into results/gcp/calibration.csv using the Prometheus API. Explain how to pick CPU_TARGET_PERCENT and QUEUE_THRESHOLD from the CSV.
```

## Prompt 4: load test  [Person 3]

```
Following CLAUDE.md, build the load test in loadtest/:
1. prompts.json: 50 varied, realistic user prompts of similar length, about 150 to 250 tokens each.
2. locustfile.py:
   - HttpUser posting to /v1/chat/completions with stream true, max_tokens 128, temperature 0, ignore_eos true, model from env MODEL_ID.
   - Measures time to first token from the first streamed data line, and total latency. Timeout 60 s.
   - Appends one row per request to memory and on test_stop writes RESULTS_DIR/requests.csv with columns start, ttft_s, latency_s, ok.
   - Also fires a custom Locust event named ttft so the Locust CSV shows it.
   - LoadTestShape selected by env SHAPE in steady, ramp, spike, calibrate, matching the table in CLAUDE.md. calibrate reads env USERS and DURATION.
3. A README section on running it locally against any OpenAI compatible endpoint, for example the Torch test replica.
Test it locally with a 30 second dry run against a mock server you write in loadtest/mock_server.py that streams fake tokens with a delay.
```

## Prompt 5: run automation  [Person 3, with Person 1 for cold start]

```
Following CLAUDE.md, write the experiment runner for GCP:
1. scripts/gcp/export_metrics.py --start --end --out: queries Prometheus query_range at 15 s step through a port-forward and writes timeseries.csv with columns time, replicas_ready, waiting, running, cpu, gpu_nodes, p95_ttft.
2. scripts/gcp/run_experiment.sh <study> <strategy> <pattern> <repeat> [--spare 0|1] [--threshold N]:
   reset_run.sh with baseline 1 plus spare, use_strategy.sh, record start time, refresh the locust-files ConfigMap, kubectl exec into loadgen to run locust headless with SHAPE set and host http://vllm.default.svc:8000, wait 180 s after Locust ends, kubectl cp results out, run export_metrics.py, save kubectl get events -A -o json and pod and node JSON to events.json, write meta.json with all settings, times and git commit. Output folder per CLAUDE.md. Stop with a clear error if any step fails.
3. scripts/gcp/run_study.sh <main|warm|sweep>: runs every run of that study in the order from CLAUDE.md, alternating strategies, skipping runs whose folder already has requests.csv, and printing progress and estimated time left.
4. analysis/coldstart.py: for every run folder, finds each new vllm pod, and splits its startup into decision, node start, image pull, model load using event and condition timestamps. Writes results/<platform>/coldstart.csv.
Update docs/FILES.md. Explain how to do one practice run end to end.
```

## Prompt 6: NYU HPC version  [whoever has Torch access]

```
Following CLAUDE.md and docs/RUN_ON_NYU_HPC.md, build the Torch version in hpc/:
1. router.py: aiohttp or FastAPI async reverse proxy on a port given by argument. Every 5 s it reads $RELAY_SCRATCH/registry, health checks each host:port at /health, and round robins requests, including streaming responses, across healthy replicas only. Exposes /router/status with the healthy list.
2. autoscaler.py --strategy a|b|none --threshold N: every CHECK_INTERVAL_S it scrapes each healthy replica's /metrics. Strategy b uses num_requests_waiting summed and divided by ready replicas. Strategy a uses CPU percent derived from process_cpu_seconds_total if present, otherwise from sstat for the job. It applies the same desired replicas formula as Kubernetes HPA, ceil(current * metric / target), clamps to MIN and MAX, and enforces the scale down window. Scales up with sbatch --account --constraint hpc/vllm_replica.sbatch and down with scancel of the newest replica. Writes timeseries.csv and an events.json timeline with submit time, start time from squeue, registry time, and first healthy time for each replica job.
3. control.sbatch <study> <strategy> <pattern> <repeat>: a CPU job that starts router.py and autoscaler.py, waits for 1 healthy replica, runs Locust headless against the router with the right SHAPE, waits 180 s, stops everything, cancels all replica jobs, and writes the run folder under results/hpc/ in the same format as GCP.
4. hpc/run_study.sh: submits control jobs for a study one at a time, waiting for each to finish.
Do not submit any jobs. Tell me exactly how to test each file, starting with a single replica.
```

## Prompt 7: analysis and charts  [Person 3]

```
Following CLAUDE.md, write analysis/make_charts.py and analysis/relay_analysis.ipynb that read every run folder and produce PNG charts in analysis/figures/:
1. For one spike run per strategy: a stacked timeline of users, waiting requests, replicas and p95 time to first token, strategy A above strategy B, shared time axis.
2. SLO violation rate by strategy and pattern: mean with min and max whiskers over repeats.
3. GPU hours by strategy and pattern.
4. Cold start breakdown: stacked bars of decision, node start, image pull, model load.
5. Warm spare: time to scale and GPU hours, 0 versus 1 spare.
6. Threshold sweep: SLO violation rate against GPU hours, one point per threshold.
Also write analysis/summary_table.csv with every metric per strategy and pattern. Charts must be readable on a slide: large fonts, labeled axes with units, no chart junk.
```

## Prompt 8: README and handoff  [anyone, last]

```
Rewrite README.md for a new teammate or grader: what Relay is, architecture, prerequisites, how to run on GCP from zero, how to run on Torch, how to reproduce every chart, cost warnings, teardown, and a note that Claude Code assisted with development. Make sure docs/FILES.md covers every file. Then list anything in the repo that is untested or uncertain.
```
