#!/usr/bin/env python3
"""Cold start breakdown for every scale up in every run folder of one platform.

    python3 analysis/coldstart.py --platform gcp      # reads results/gcp/<study>/<run>/, writes results/gcp/coldstart.csv
    python3 analysis/coldstart.py --platform hpc

Each new replica (created after the run started) becomes one row, split into phases that add up to total_s:

  phase         GCP (Kubernetes)                                    HPC (Slurm)
  decision_s    metric first above target -> HPA/KEDA rescale       metric first above target -> sbatch
  node_start_s  pod created -> pod scheduled (waits for a new node) sbatch -> job running (queue wait)
  image_pull_s  Pulling -> Pulled event (0 if image already cached) 0, the SIF is already on /scratch
  model_load_s  container started -> Ready (weights download, load) job running -> first healthy /health
  other_s       the rest: rescale -> pod created, scheduled -> pull, pulled -> started
  total_s       metric first above target -> Ready (or rescale -> Ready when no trigger was found)

"Metric above target" uses timeseries.csv: strategy b, waiting / replicas_ready > threshold;
strategy a, cpu > target percent. decision_s is blank when the trigger cannot be found (for example
strategy none, or a scale up that happened inside one 15 s sample).

HPC events.json format, written by hpc/autoscaler.py:
  {"platform": "hpc", "replicas": [{"job_id": "123", "trigger_time": 1700000000.0, "submit_time": ...,
    "start_time": ..., "registry_time": ..., "healthy_time": ...}, ...]}   (unix seconds, null if unknown)
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from datetime import datetime
from pathlib import Path

FIELDS = ["platform", "study", "run", "strategy", "pattern", "repeat", "variant", "replica", "new_node",
          "decision_s", "node_start_s", "image_pull_s", "model_load_s", "other_s", "total_s",
          "trigger_time", "rescale_time", "created_time", "ready_time"]


def ts(value: str | None) -> float | None:
    """RFC 3339 Kubernetes timestamp to unix seconds."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def event_time(ev: dict) -> float | None:
    return ts(ev.get("eventTime")) or ts(ev.get("firstTimestamp")) or ts(ev.get("lastTimestamp"))


def diff(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None else max(0.0, b - a)


# ---------------------------------------------------------------- trigger from timeseries.csv

def load_timeseries(path: Path) -> list[dict[str, float]]:
    if not path.exists():
        return []
    rows = []
    for r in csv.DictReader(path.open()):
        rows.append({k: float(v) if v not in ("", None) else float("nan") for k, v in r.items()})
    return rows


def above_target(row: dict[str, float], meta: dict) -> bool:
    if meta["strategy"] == "b":
        threshold = float(meta.get("queue_threshold") or meta["settings"]["QUEUE_THRESHOLD"])
        return row["waiting"] / max(row["replicas_ready"], 1.0) > threshold
    if meta["strategy"] == "a":
        target = float(meta.get("cpu_target_percent") or meta["settings"]["CPU_TARGET_PERCENT"])
        return row["cpu"] > target  # NaN compares False
    return False


def find_trigger(series: list[dict[str, float]], meta: dict, decision_at: float,
                 not_before: float) -> float | None:
    """Start of the unbroken above-target streak that ends at the scale decision."""
    trigger = None
    for row in reversed([r for r in series if not_before <= r["time"] <= decision_at]):
        if above_target(row, meta):
            trigger = row["time"]
        else:
            break
    return trigger


# ---------------------------------------------------------------- GCP

def gcp_pods(doc: dict) -> dict[str, dict]:
    """Merge every pod snapshot (watch stream plus final list) into first seen lifecycle times."""
    pods: dict[str, dict] = {}
    snapshots = [w.get("object", {}) for w in doc.get("pod_watch", [])] + doc.get("pods_at_end", [])
    for pod in snapshots:
        md, status = pod.get("metadata", {}), pod.get("status", {})
        name = md.get("name")
        if not name:
            continue
        p = pods.setdefault(name, {"created": ts(md.get("creationTimestamp")), "node": None,
                                   "scheduled": None, "started": None, "ready": None})
        p["node"] = p["node"] or pod.get("spec", {}).get("nodeName")
        for cond in status.get("conditions") or []:
            if cond.get("status") != "True":
                continue
            if cond["type"] == "PodScheduled" and p["scheduled"] is None:
                p["scheduled"] = ts(cond.get("lastTransitionTime"))
            if cond["type"] == "Ready" and p["ready"] is None:
                p["ready"] = ts(cond.get("lastTransitionTime"))
        for cs in status.get("containerStatuses") or []:
            started = (cs.get("state", {}).get("running") or {}).get("startedAt")
            if cs.get("name") == "vllm" and started and p["started"] is None:
                p["started"] = ts(started)
    return pods


def gcp_node_created(doc: dict) -> dict[str, float | None]:
    nodes = [w.get("object", {}) for w in doc.get("node_watch", [])] + doc.get("nodes_at_end", [])
    return {n["metadata"]["name"]: ts(n["metadata"].get("creationTimestamp")) for n in nodes if "metadata" in n}


def gcp_rows(doc: dict, meta: dict, series: list[dict[str, float]]) -> list[dict]:
    events = doc.get("events", [])
    by_pod: dict[str, list[dict]] = {}
    for ev in events:
        obj = ev.get("involvedObject", {})
        if obj.get("kind") == "Pod":
            by_pod.setdefault(obj.get("name", ""), []).append(ev)
    rescales = sorted(t for ev in events
                      if ev.get("reason") == "SuccessfulRescale"
                      and ev.get("involvedObject", {}).get("kind") == "HorizontalPodAutoscaler"
                      and (t := event_time(ev)) is not None and t >= meta["start"])
    nodes = gcp_node_created(doc)
    rows, prev_decision = [], float(meta["start"])
    for name, p in sorted(gcp_pods(doc).items(), key=lambda kv: kv[1]["created"] or 0):
        if p["created"] is None or p["created"] < meta["start"]:
            continue  # the baseline replica, already running when the run began
        evs = by_pod.get(name, [])

        def first(reason: str) -> dict | None:
            hits = sorted((e for e in evs if e.get("reason") == reason), key=lambda e: event_time(e) or 0)
            return hits[0] if hits else None

        pulling, pulled = first("Pulling"), first("Pulled")
        if pulled and "already present" in pulled.get("message", ""):
            image_pull = 0.0
        else:
            image_pull = diff(event_time(pulling) if pulling else None, event_time(pulled) if pulled else None)
        rescale = max((t for t in rescales if t <= p["created"] + 1), default=None)
        decision_at = rescale if rescale is not None else p["created"]
        trigger = find_trigger(series, meta, decision_at, prev_decision)
        prev_decision = decision_at
        node_created = nodes.get(p["node"] or "")
        new_node = bool(first("TriggeredScaleUp")) or (node_created is not None and node_created > p["created"])
        rows.append(phase_row(meta, name, new_node, trigger, rescale, p["created"],
                              node_start=diff(p["created"], p["scheduled"]), image_pull=image_pull,
                              model_load=diff(p["started"], p["ready"]), ready=p["ready"]))
    return rows


# ---------------------------------------------------------------- HPC

def hpc_rows(doc: dict, meta: dict, series: list[dict[str, float]]) -> list[dict]:
    rows, prev = [], float(meta["start"])
    for rep in sorted(doc.get("replicas", []), key=lambda r: r.get("submit_time") or 0):
        submit = rep.get("submit_time")
        if submit is None or submit < meta["start"]:
            continue
        trigger = rep.get("trigger_time") or find_trigger(series, meta, submit, prev)
        prev = submit
        rows.append(phase_row(meta, str(rep.get("job_id")), True, trigger, submit, submit,
                              node_start=diff(submit, rep.get("start_time")), image_pull=0.0,
                              model_load=diff(rep.get("start_time"), rep.get("healthy_time")),
                              ready=rep.get("healthy_time")))
    return rows


# ---------------------------------------------------------------- shared

def phase_row(meta: dict, replica: str, new_node: bool, trigger: float | None, rescale: float | None,
              created: float, node_start: float | None, image_pull: float | None,
              model_load: float | None, ready: float | None) -> dict:
    decision = diff(trigger, rescale)
    begin = trigger if trigger is not None else (rescale if rescale is not None else created)
    total = diff(begin, ready)
    known = sum(x for x in (decision, node_start, image_pull, model_load) if x is not None)
    other = None if total is None else max(0.0, total - known)
    fmt = lambda x: "" if x is None else f"{x:.1f}"  # noqa: E731
    return {
        "platform": meta.get("platform", ""), "study": meta.get("study", ""), "run": meta["_run"],
        "strategy": meta.get("strategy", ""), "pattern": meta.get("pattern", ""),
        "repeat": meta.get("repeat", ""), "variant": meta.get("variant", ""), "replica": replica,
        "new_node": int(new_node), "decision_s": fmt(decision), "node_start_s": fmt(node_start),
        "image_pull_s": fmt(image_pull), "model_load_s": fmt(model_load), "other_s": fmt(other),
        "total_s": fmt(total), "trigger_time": fmt(trigger), "rescale_time": fmt(rescale),
        "created_time": fmt(created), "ready_time": fmt(ready),
    }


def run_rows(run_dir: Path) -> list[dict]:
    meta_path, events_path = run_dir / "meta.json", run_dir / "events.json"
    if not meta_path.exists() or not events_path.exists() or (run_dir / "FAILED").exists():
        return []
    meta = json.loads(meta_path.read_text())
    meta["_run"] = run_dir.name
    doc = json.loads(events_path.read_text())
    series = load_timeseries(run_dir / "timeseries.csv")
    if doc.get("platform", meta.get("platform")) == "hpc":
        return hpc_rows(doc, meta, series)
    return gcp_rows(doc, meta, series)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--platform", required=True, help="folder under results/, for example gcp, hpc or synthetic")
    p.add_argument("--results", type=Path, default=Path(__file__).resolve().parent.parent / "results")
    p.add_argument("--out", type=Path, help="default results/<platform>/coldstart.csv")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    root = args.results / args.platform
    run_dirs = sorted(d for d in root.glob("*/*") if d.is_dir())
    rows = [row for d in run_dirs for row in run_rows(d)]
    out = args.out or root / "coldstart.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(f"{len(run_dirs)} run folders, {len(rows)} scale ups -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
