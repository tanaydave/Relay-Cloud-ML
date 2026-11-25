#!/usr/bin/env python3
"""Minimal Prometheus HTTP API client, standard library only.

Import it (export_metrics.py, calibrate.sh) or use the CLI:
    python3 scripts/gcp/prom.py mean  --url http://127.0.0.1:9090 --query 'sum(vllm:num_requests_waiting)' --start 1700000000 --end 1700000120
    python3 scripts/gcp/prom.py range --url http://127.0.0.1:9090 --query 'up' --start ... --end ... --step 15
`mean` prints one number (nan if no data); `range` prints time,value CSV rows.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import urllib.parse
import urllib.request


def query_range(url: str, query: str, start: float, end: float, step: float = 15.0,
                timeout: float = 30.0) -> list[tuple[float, float]]:
    """Run query_range and return (time, value) points, summed across series per timestamp.

    Relay queries are written to return a single series; if several come back, values at the
    same timestamp are added, which is right for the count and sum style queries used here.
    """
    params = urllib.parse.urlencode({"query": query, "start": start, "end": end, "step": step})
    with urllib.request.urlopen(f"{url.rstrip('/')}/api/v1/query_range?{params}", timeout=timeout) as resp:
        payload = json.load(resp)
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus error for {query!r}: {payload}")
    merged: dict[float, float] = {}
    for series in payload["data"]["result"]:
        for ts, val in series["values"]:
            v = float(val)
            if math.isnan(v):
                continue
            merged[float(ts)] = merged.get(float(ts), 0.0) + v
    return sorted(merged.items())


def mean(points: list[tuple[float, float]]) -> float:
    return sum(v for _, v in points) / len(points) if points else float("nan")


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("mode", choices=["mean", "range"])
    p.add_argument("--url", default="http://127.0.0.1:9090")
    p.add_argument("--query", required=True)
    p.add_argument("--start", type=float, required=True, help="unix seconds")
    p.add_argument("--end", type=float, required=True, help="unix seconds")
    p.add_argument("--step", type=float, default=15.0)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    points = query_range(args.url, args.query, args.start, args.end, args.step)
    if args.mode == "mean":
        print(f"{mean(points):.4f}")
    else:
        print("time,value")
        for ts, v in points:
            print(f"{ts:.0f},{v:.6g}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
