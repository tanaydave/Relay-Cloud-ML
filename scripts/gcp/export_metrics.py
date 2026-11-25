#!/usr/bin/env python3
"""Export one run's Prometheus metrics to timeseries.csv at a 15 s step.

Needs Prometheus reachable at --prom-url; run_experiment.sh port-forwards it first:
    kubectl port-forward -n monitoring svc/monitoring-kube-prometheus-prometheus 9090:9090 &
    python3 scripts/gcp/export_metrics.py --start 1700000000 --end 1700001000 --out results/.../timeseries.csv

Columns (CLAUDE.md): time, replicas_ready, waiting, running, cpu, gpu_nodes, p95_ttft
  time            unix seconds
  replicas_ready  available vllm replicas (kube-state-metrics)
  waiting         sum of vllm:num_requests_waiting over replicas
  running         sum of vllm:num_requests_running over replicas
  cpu             average CPU use of running vllm pods, percent of their CPU request (what HPA compares to its target)
  gpu_nodes       GPU nodes that exist, Ready or not (they bill from creation)
  p95_ttft        p95 time to first token over the last minute, seconds; blank when there was no traffic
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from prom import query_range  # noqa: E402

COLUMNS = ["time", "replicas_ready", "waiting", "running", "cpu", "gpu_nodes", "p95_ttft"]
ZERO_WHEN_MISSING = {"replicas_ready", "waiting", "running", "gpu_nodes"}


def queries(namespace: str, gpu_type: str) -> dict[str, str]:
    ns = f'namespace="{namespace}"'
    return {
        "replicas_ready": f'kube_deployment_status_replicas_available{{{ns},deployment="vllm"}}',
        "waiting": "sum(vllm:num_requests_waiting)",
        "running": "sum(vllm:num_requests_running)",
        # Per pod usage / per pod request, then averaged. Pending pods have no usage series, so they drop out.
        "cpu": (f'100 * avg(sum by (pod) (rate(container_cpu_usage_seconds_total{{{ns},container="vllm"}}[1m]))'
                f' / sum by (pod) (kube_pod_container_resource_requests{{{ns},container="vllm",resource="cpu"}}))'),
        "gpu_nodes": f'count(kube_node_labels{{label_cloud_google_com_gke_accelerator="{gpu_type}"}})',
        "p95_ttft": "histogram_quantile(0.95, sum(rate(vllm:time_to_first_token_seconds_bucket[1m])) by (le))",
    }


def collect(url: str, start: float, end: float, step: float, qs: dict[str, str]) -> list[dict[str, str]]:
    """Query every column and join on timestamp. Missing counts become 0, missing ratios stay blank."""
    series = {name: dict(query_range(url, q, start, end, step)) for name, q in qs.items()}
    # Every step from start to end, so gaps (for example no GPU nodes) still give a row.
    times = [start + i * step for i in range(int((end - start) // step) + 1)]
    rows = []
    for t in times:
        row = {"time": f"{t:.0f}"}
        for name in qs:
            v = series[name].get(t)
            if v is None:
                row[name] = "0" if name in ZERO_WHEN_MISSING else ""
            else:
                row[name] = f"{v:.4g}"
        rows.append(row)
    return rows


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--start", type=float, required=True, help="unix seconds")
    p.add_argument("--end", type=float, required=True, help="unix seconds")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--prom-url", default="http://127.0.0.1:9090")
    p.add_argument("--step", type=float, default=15.0)
    p.add_argument("--namespace", default="default")
    p.add_argument("--gpu-type", default="nvidia-l4")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    start = args.start - args.start % args.step  # align to the step so every query shares timestamps
    rows = collect(args.prom_url, start, args.end, args.step, queries(args.namespace, args.gpu_type))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    filled = sum(1 for r in rows if r["replicas_ready"] != "0")
    print(f"wrote {len(rows)} rows to {args.out} ({filled} with replicas_ready > 0)")
    if filled == 0:
        print("WARNING: replicas_ready was 0 or missing for the whole run; check kube-state-metrics", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
