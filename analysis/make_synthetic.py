#!/usr/bin/env python3
"""Write a FAKE results tree to results/synthetic/ so the analysis can be tested before real runs exist.

    python3 analysis/make_synthetic.py && python3 analysis/make_charts.py --platform synthetic

Every file follows the GCP format (meta.json, requests.csv, timeseries.csv, events.json) and every
meta.json has "synthetic": true. The numbers come from a toy simulation, NOT from measurements:
never put them in the report. results/synthetic/ is git ignored.

Toy model: each ready replica runs up to 20 requests at once, the rest wait. Strategy B scales on
waiting per replica, Strategy A on a CPU signal that only rises once a replica is saturated.
A new replica needs a new GPU node (about 100 s), an image pull (about 40 s) and a model load (about 70 s),
unless a spare node is free.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "results" / "synthetic"
LENGTH = {"steady": 600, "ramp": 600, "spike": 720}
SLOTS, MIN_R, MAX_R, CHECK, DOWN_WINDOW, COOL = 20, 1, 3, 15, 120, 180


def users(pattern: str, t: int) -> int:
    if t >= LENGTH[pattern]:
        return 0
    if pattern == "steady":
        return 8
    if pattern == "ramp":
        return round(2 + 58 * t / 600)
    return 60 if 120 <= t < 420 else 4


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Sim:
    def __init__(self, strategy: str, pattern: str, threshold: float, spare: int, seed: str) -> None:
        self.rng = random.Random(seed)
        self.strategy, self.pattern, self.threshold = strategy, pattern, threshold
        self.start = 1795000000 + self.rng.randint(0, 10**6)
        self.baseline_nodes = 1 + spare
        # replica: dict(name, created, scheduled, pull_start, pulled, started, ready, deleted, node, new_node)
        self.replicas = [{"name": "vllm-base", "created": self.start - 900, "ready": self.start - 700,
                          "deleted": None, "node": "gpu-node-0", "scheduled": self.start - 890,
                          "pull_start": None, "pulled": None, "started": self.start - 800, "new_node": False}]
        self.nodes = [{"name": f"gpu-node-{i}", "created": self.start - 1000, "deleted": None} for i in range(1 + spare)]
        self.events: list[dict] = []
        self.desired_history: list[tuple[int, int]] = []

    # -- cluster state at time t (absolute seconds)
    def ready(self, t: float) -> int:
        return sum(1 for r in self.replicas if r["ready"] is not None and r["ready"] <= t
                   and (r["deleted"] is None or r["deleted"] > t))

    def wanted(self, t: float) -> int:
        return sum(1 for r in self.replicas if r["created"] <= t and (r["deleted"] is None or r["deleted"] > t))

    def gpu_nodes(self, t: float) -> int:
        return sum(1 for n in self.nodes if n["created"] <= t and (n["deleted"] is None or n["deleted"] > t))

    def load(self, t: int) -> tuple[float, float, float]:
        u, r = users(self.pattern, t - self.start), max(self.ready(t), 1)
        running = min(u, SLOTS * r)
        waiting = max(0, u - SLOTS * r)
        cpu = 22 + 50 * (running / (SLOTS * r)) ** 3 + self.rng.gauss(0, 2)  # rises only near saturation
        return waiting, running, cpu

    def add_replica(self, t: int) -> None:
        free = [n for n in self.nodes if (n["deleted"] is None or n["deleted"] > t) and n["created"] <= t
                and not any(r["node"] == n["name"] and (r["deleted"] is None or r["deleted"] > t) for r in self.replicas)]
        name = f"vllm-{len(self.replicas):02d}{self.rng.randint(100, 999)}"
        created = t + 1
        if free:
            node, new_node = free[0]["name"], False
            scheduled = created + 2
            pull_start, pulled = scheduled + 1, scheduled + 2
        else:
            node, new_node = f"gpu-node-{len(self.nodes)}", True
            node_created = created + 10 + self.rng.uniform(0, 5)
            self.nodes.append({"name": node, "created": node_created, "deleted": None})
            scheduled = node_created + 90 + self.rng.gauss(0, 12)
            pull_start = scheduled + 3
            pulled = pull_start + 40 + self.rng.gauss(0, 6)
            self.events.append(ev("Pod", name, "TriggeredScaleUp", created + 2, "pod triggered scale-up"))
        started = pulled + 3
        ready = started + 70 + self.rng.gauss(0, 8)
        msg = "Container image already present on machine" if not new_node else "Successfully pulled image in 40s"
        self.events += [ev("Pod", name, "Scheduled", scheduled), ev("Pod", name, "Pulling", pull_start),
                        ev("Pod", name, "Pulled", pulled, msg), ev("Pod", name, "Started", started)]
        self.replicas.append({"name": name, "created": created, "scheduled": scheduled, "pull_start": pull_start,
                              "pulled": pulled, "started": started, "ready": ready, "deleted": None,
                              "node": node, "new_node": new_node})

    def remove_replica(self, t: int) -> None:
        live = [r for r in self.replicas if r["created"] <= t and (r["deleted"] is None or r["deleted"] > t)]
        newest = max(live, key=lambda r: r["created"])
        newest["deleted"] = t
        node = next(n for n in self.nodes if n["name"] == newest["node"])
        if self.gpu_nodes(t) > self.baseline_nodes:
            node["deleted"] = t + 300 + self.rng.uniform(0, 60)  # cluster autoscaler removes idle nodes later

    def desired(self, t: int) -> int:
        waiting, running, cpu = self.load(t)
        current = max(self.wanted(t), 1)
        ready = max(self.ready(t), 1)
        if self.strategy == "b":
            metric, target = waiting / ready, self.threshold
        elif self.strategy == "a":
            metric, target = cpu, 60.0
        else:
            return current
        ratio = metric / target if target else 0
        if abs(ratio - 1) <= 0.1:
            return current
        want = math.ceil(ready * ratio) if self.strategy == "a" else math.ceil(waiting / target) + ready - 1
        return max(MIN_R, min(MAX_R, want if ratio > 1 else max(want, 1)))

    def run(self) -> None:
        end = self.start + LENGTH[self.pattern] + COOL
        for t in range(self.start, end, CHECK):
            want = self.desired(t)
            self.desired_history.append((t, want))
            # scale down only to the highest recommendation of the last DOWN_WINDOW seconds
            recent = max(w for ts, w in self.desired_history if ts > t - DOWN_WINDOW)
            current = self.wanted(t)
            target = want if want > current else min(current, recent)
            if target != current:
                self.events.append(ev("HorizontalPodAutoscaler", "keda-hpa-vllm-queue" if self.strategy == "b"
                                      else "vllm-cpu", "SuccessfulRescale", t, f"New size: {target}"))
            for _ in range(target - current):
                self.add_replica(t)
            for _ in range(current - target):
                self.remove_replica(t)
        self.end = end


def ev(kind: str, name: str, reason: str, t: float, msg: str = "") -> dict:
    return {"involvedObject": {"kind": kind, "name": name}, "reason": reason, "message": msg,
            "firstTimestamp": iso(t), "lastTimestamp": iso(t), "eventTime": None}


def pod_object(r: dict) -> dict:
    conds = [{"type": "PodScheduled", "status": "True", "lastTransitionTime": iso(r["scheduled"])}]
    if r["ready"] is not None:
        conds.append({"type": "Ready", "status": "True", "lastTransitionTime": iso(r["ready"])})
    return {"metadata": {"name": r["name"], "creationTimestamp": iso(r["created"])},
            "spec": {"nodeName": r["node"]},
            "status": {"conditions": conds,
                       "containerStatuses": [{"name": "vllm", "state": {"running": {"startedAt": iso(r["started"])}}}]}}


def write_run(sim: Sim, out: Path, meta: dict) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rng = sim.rng
    # requests: closed loop users, latency grows with queueing
    rows, carry = [], 0.0
    for t in range(sim.start, sim.start + LENGTH[sim.pattern]):
        waiting, running, _ = sim.load(t)
        u = users(sim.pattern, t - sim.start)
        if not u:
            continue
        r = max(sim.ready(t), 1)
        queue_delay = waiting / (SLOTS * r / 1.9)
        decode = 128 * 0.011 * (1 + running / (SLOTS * r))
        latency = 0.15 + queue_delay + decode
        carry += u / latency
        for _ in range(int(carry)):
            ttft = max(0.03, 0.12 + queue_delay * rng.uniform(0.7, 1.3) + rng.gauss(0, 0.03))
            ok = ttft < 60
            rows.append((t + rng.random(), f"{ttft:.4f}" if ok else "", round(ttft + decode, 4), int(ok)))
        carry -= int(carry)
    with (out / "requests.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["start", "ttft_s", "latency_s", "ok"])
        w.writerows(sorted(rows))
    # timeseries at 15 s
    with (out / "timeseries.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["time", "replicas_ready", "waiting", "running", "cpu", "gpu_nodes", "p95_ttft"])
        for t in range(sim.start, sim.end + 1, 15):
            waiting, running, cpu = sim.load(t)
            recent = sorted(float(x[1]) for x in rows if t - 60 <= x[0] <= t and x[1])
            p95 = f"{recent[int(0.95 * (len(recent) - 1))]:.3f}" if recent else ""
            w.writerow([t, sim.ready(t), int(waiting), int(running), f"{cpu:.1f}", sim.gpu_nodes(t), p95])
    doc = {"platform": "gcp", "synthetic": True, "events": sim.events,
           "pod_watch": [{"type": "ADDED", "object": pod_object(r)} for r in sim.replicas],
           "pods_at_end": [], "nodes_at_end": [],
           "node_watch": [{"type": "ADDED", "object": {"metadata": {"name": n["name"], "creationTimestamp": iso(n["created"])}}}
                          for n in sim.nodes]}
    (out / "events.json").write_text(json.dumps(doc))
    meta.update({"platform": "gcp", "synthetic": True, "start": sim.start, "end": sim.end,
                 "locust_end": sim.start + LENGTH[sim.pattern]})
    (out / "meta.json").write_text(json.dumps(meta, indent=2))


def plan() -> list[tuple[str, str, str, int, float, int, str]]:
    runs = []
    for r in (1, 2, 3):
        for p in ("steady", "ramp", "spike"):
            for s in ("a", "b"):
                runs.append(("main", s, p, r, 5.0, 0, ""))
        for spare in (0, 1):
            runs.append(("warm", "b", "spike", r, 5.0, spare, f"spare{spare}"))
    for r in (1, 2):
        for th in (2, 5, 10):
            runs.append(("sweep", "b", "spike", r, float(th), 0, f"t{th}"))
    return runs


def main() -> int:
    argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter).parse_args()
    shutil.rmtree(ROOT, ignore_errors=True)
    for study, s, p, r, th, spare, variant in plan():
        name = f"{s}_{p}_r{r}" + (f"_{variant}" if variant else "")
        sim = Sim(s, p, th, spare, seed=f"{study}/{name}")
        sim.run()
        meta = {"study": study, "strategy": s, "pattern": p, "repeat": r, "variant": variant,
                "spare_gpu_nodes": spare, "queue_threshold": th if s == "b" else None,
                "cpu_target_percent": 60.0 if s == "a" else None,
                "settings": {"QUEUE_THRESHOLD": str(th), "CPU_TARGET_PERCENT": "60", "SLO_TTFT_S": "2"}}
        write_run(sim, ROOT / study / name, meta)
    print(f"wrote {len(plan())} synthetic runs to {ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
