"""Load Relay run folders and compute per run metrics. Used by make_charts.py and the notebook.

A run folder (CLAUDE.md): results/<platform>/<study>/<strategy>_<pattern>_r<repeat>[_<variant>]/
with meta.json, requests.csv, timeseries.csv, events.json. Folders without meta.json, or with a
FAILED file, are skipped.

Metric definitions, used everywhere in the report:
  slo_violation_rate  share of requests with ttft_s > SLO_TTFT_S (2 s); failed requests count as violations
  p50/p95_latency_s   over successful requests, full response time
  p50/p95_ttft_s      over successful requests
  gpu_hours           integral of gpu_nodes over the run window (start to end, which includes the 180 s
                      cool down); nodes bill from creation, so this is what the run costs
  replica_hours       integral of replicas_ready over the same window
  gpu_slice_hours     integral of gpu_slices (vllm pods holding a GPU or MIG slice) over the same window
  cost_hours          the cost measure used in charts: gpu_slice_hours when the run used MIG slices
                      (GPU_PARTITION_SIZE set; node hours are then the same for every run), else gpu_hours
  time_to_scale_s     first scale up of the run: metric first above target to new replica Ready
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coldstart  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
PATTERN_SECONDS = {"steady": 600, "ramp": 600, "spike": 720}


def users_at(pattern: str, t: np.ndarray) -> np.ndarray:
    """Locust users at seconds t since start, the same shapes as loadtest/locustfile.py."""
    t = np.asarray(t, dtype=float)
    if pattern == "steady":
        users = np.full_like(t, 8.0)
    elif pattern == "ramp":
        users = np.round(2 + 58 * t / 600)
    elif pattern == "spike":
        users = np.where((t >= 120) & (t < 420), 60.0, 4.0)
    else:
        raise ValueError(f"unknown pattern {pattern}")
    return np.where((t >= 0) & (t < PATTERN_SECONDS[pattern]), users, 0.0)


@dataclass
class Run:
    path: Path
    meta: dict
    requests: pd.DataFrame
    timeseries: pd.DataFrame
    scaleups: list[dict] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def study(self) -> str:
        return self.meta["study"]

    @property
    def strategy(self) -> str:
        return self.meta["strategy"]

    @property
    def pattern(self) -> str:
        return self.meta["pattern"]

    @property
    def variant(self) -> str:
        return self.meta.get("variant") or ""

    @property
    def partitioned(self) -> bool:
        return bool(self.meta.get("settings", {}).get("GPU_PARTITION_SIZE"))

    @property
    def slo_s(self) -> float:
        return float(self.meta.get("settings", {}).get("SLO_TTFT_S") or 2)


def load_run(path: Path) -> Run | None:
    if not (path / "meta.json").exists() or (path / "FAILED").exists():
        return None
    meta = json.loads((path / "meta.json").read_text())
    requests = pd.read_csv(path / "requests.csv")
    ts_path = path / "timeseries.csv"
    timeseries = pd.read_csv(ts_path) if ts_path.exists() else pd.DataFrame()
    scaleups = coldstart.run_rows(path) if (path / "events.json").exists() else []
    return Run(path, meta, requests, timeseries, scaleups)


def load_runs(platform: str = "gcp", results: Path | None = None) -> list[Run]:
    root = (results or REPO_ROOT / "results") / platform
    runs = [load_run(p) for p in sorted(root.glob("*/*")) if p.is_dir()]
    return [r for r in runs if r is not None]


def integrate_hours(ts: pd.DataFrame, column: str, start: float, end: float) -> float:
    """Step integral of a 15 s sampled column over [start, end], in hours."""
    if ts.empty or column not in ts:
        return float("nan")
    df = ts[(ts["time"] >= start - 15) & (ts["time"] <= end)].sort_values("time")
    if df.empty:
        return float("nan")
    times = np.clip(df["time"].to_numpy(dtype=float), start, end)
    edges = np.append(times, end)
    return float(np.sum(df[column].fillna(0).to_numpy() * np.diff(edges)) / 3600.0)


def run_metrics(run: Run) -> dict:
    req = run.requests
    ok = req[req["ok"] == 1]
    violations = (req["ok"] != 1) | (req["ttft_s"].fillna(np.inf) > run.slo_s)
    first_scale = next((float(s["total_s"]) for s in run.scaleups if s.get("total_s")), float("nan"))
    start, end = float(run.meta["start"]), float(run.meta["end"])
    ts = run.timeseries
    return {
        "study": run.study, "strategy": run.strategy, "pattern": run.pattern, "variant": run.variant,
        "repeat": run.meta.get("repeat"), "run": run.name,
        "requests": len(req), "failed": int((req["ok"] != 1).sum()),
        "slo_violation_rate": float(violations.mean()) if len(req) else float("nan"),
        "p50_ttft_s": ok["ttft_s"].quantile(0.5), "p95_ttft_s": ok["ttft_s"].quantile(0.95),
        "p50_latency_s": ok["latency_s"].quantile(0.5), "p95_latency_s": ok["latency_s"].quantile(0.95),
        "gpu_hours": integrate_hours(ts, "gpu_nodes", start, end),
        "replica_hours": integrate_hours(ts, "replicas_ready", start, end),
        "gpu_slice_hours": integrate_hours(ts, "gpu_slices", start, end),
        "partitioned": run.partitioned,
        "max_replicas": float(ts["replicas_ready"].max()) if not ts.empty else float("nan"),
        "scale_ups": len(run.scaleups),
        "time_to_scale_s": first_scale,
    }


def metrics_table(runs: list[Run]) -> pd.DataFrame:
    df = pd.DataFrame([run_metrics(r) for r in runs])
    if not df.empty:
        df["cost_hours"] = np.where(df["partitioned"], df["gpu_slice_hours"], df["gpu_hours"])
    return df


def cost_label(per_run: pd.DataFrame) -> str:
    """Axis label for cost_hours; says so when runs mix whole GPUs and MIG slices."""
    if per_run.empty or not per_run["partitioned"].any():
        return "GPU hours per run"
    if per_run["partitioned"].all():
        return "GPU slice hours per run"
    return "GPU or slice hours per run (mixed)"


def summary_table(per_run: pd.DataFrame) -> pd.DataFrame:
    """One row per study, strategy, pattern, variant: mean, min and max over repeats."""
    if per_run.empty:
        return per_run
    keys = ["study", "strategy", "pattern", "variant"]
    value_cols = ["slo_violation_rate", "p50_ttft_s", "p95_ttft_s", "p50_latency_s", "p95_latency_s",
                  "gpu_hours", "gpu_slice_hours", "cost_hours", "replica_hours", "max_replicas",
                  "time_to_scale_s", "failed", "requests"]
    g = per_run.groupby(keys, dropna=False)
    out = g.size().rename("runs").to_frame()
    for col in value_cols:
        out[f"{col}_mean"] = g[col].mean()
        if col in ("slo_violation_rate", "gpu_hours", "cost_hours", "time_to_scale_s", "p95_ttft_s"):
            out[f"{col}_min"] = g[col].min()
            out[f"{col}_max"] = g[col].max()
    return out.reset_index().round(4)


def coldstart_table(runs: list[Run]) -> pd.DataFrame:
    rows = [row for r in runs for row in r.scaleups]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    for col in ("decision_s", "node_start_s", "image_pull_s", "model_load_s", "other_s", "total_s"):
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df
