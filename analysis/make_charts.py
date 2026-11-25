#!/usr/bin/env python3
"""Make every Relay chart and the summary table from the run folders.

    python3 analysis/make_charts.py                       # results/gcp -> analysis/figures/, analysis/summary_table.csv
    python3 analysis/make_charts.py --platform synthetic  # -> analysis/figures/synthetic/, analysis/summary_table_synthetic.csv

Charts (PNG, sized for a 16:9 slide), skipped with a message when their study has no runs yet:
  1_spike_timeline.png    one spike run per strategy: users, waiting, replicas, p95 TTFT on a shared time axis
  2_slo_violations.png    SLO violation rate by strategy and pattern, mean with min and max over repeats
  3_gpu_hours.png         GPU hours by strategy and pattern, mean with min and max
  4_coldstart.png         mean cold start phases per scale up, new node versus an existing node
  5_warm_spare.png        time to scale and GPU hours, 0 versus 1 warm spare node
  6_threshold_sweep.png   SLO violation rate against GPU hours, one point per Strategy B threshold
Also writes per_run_metrics.csv next to the summary table.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import relay_data as rd  # noqa: E402

# Validated default palette (dataviz skill), categorical slots in fixed order. Color follows the entity.
STRATEGY_COLOR = {"a": "#257de0", "b": "#f47230", "none": "#666666"}
STRATEGY_LABEL = {"a": "A: CPU (HPA)", "b": "B: queue (KEDA)", "none": "no autoscaler"}
PHASES = [("decision_s", "decision", "#257de0"), ("node_start_s", "node start", "#f47230"),
          ("image_pull_s", "image pull", "#00b894"), ("model_load_s", "model load", "#f2a600"),
          ("other_s", "other", "#b5b5b5")]
TEXT, TEXT_2, GRID = "#191919", "#666666", "#e6e6e6"
PATTERNS = ["steady", "ramp", "spike"]


def style() -> None:
    plt.rcParams.update({
        "font.size": 15, "axes.titlesize": 18, "axes.labelsize": 15, "xtick.labelsize": 14,
        "ytick.labelsize": 14, "legend.fontsize": 14, "axes.edgecolor": TEXT_2, "axes.labelcolor": TEXT,
        "xtick.color": TEXT_2, "ytick.color": TEXT_2, "text.color": TEXT, "axes.spines.top": False,
        "axes.spines.right": False, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 1,
        "axes.axisbelow": True, "lines.linewidth": 2, "figure.dpi": 100, "savefig.dpi": 150,
        "legend.frameon": False, "axes.titlelocation": "left",
    })


def save(fig: plt.Figure, out: Path) -> None:
    fig.savefig(out, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    print(f"  wrote {out}")


def whiskers(values: pd.Series) -> tuple[float, float, float]:
    return float(values.mean()), float(values.mean() - values.min()), float(values.max() - values.mean())


# ---------------------------------------------------------------- 1 spike timeline

def pick_spike_run(runs: list[rd.Run], strategy: str) -> rd.Run | None:
    candidates = [r for r in runs if r.pattern == "spike" and r.strategy == strategy and r.study == "main"]
    candidates = candidates or [r for r in runs if r.pattern == "spike" and r.strategy == strategy and not r.variant]
    return sorted(candidates, key=lambda r: r.meta.get("repeat", 0))[0] if candidates else None


def chart_spike_timeline(runs: list[rd.Run], out: Path) -> None:
    chosen = {s: pick_spike_run(runs, s) for s in ("a", "b")}
    chosen = {s: r for s, r in chosen.items() if r is not None and not r.timeseries.empty}
    if not chosen:
        print("  skip 1_spike_timeline: no spike runs yet")
        return
    fig, axes = plt.subplots(4, 1, figsize=(13, 10), sharex=True, gridspec_kw={"hspace": 0.35})
    t_users = np.arange(0, 901)
    axes[0].plot(t_users, rd.users_at("spike", t_users), color=TEXT_2)
    axes[0].set_ylabel("users")
    axes[0].set_title("Spike traffic: Strategy A versus B", pad=28)
    for s, run in chosen.items():
        ts = run.timeseries.copy()
        ts["t"] = ts["time"] - float(run.meta["start"])
        kw = {"color": STRATEGY_COLOR[s], "label": f"{STRATEGY_LABEL[s]} ({run.name})"}
        axes[1].plot(ts["t"], ts["waiting"], **kw)
        axes[2].step(ts["t"], ts["replicas_ready"], where="post", **kw)
        axes[3].plot(ts["t"], ts["p95_ttft"], **kw)
    axes[1].set_ylabel("waiting\nrequests")
    axes[2].set_ylabel("replicas\nready")
    axes[2].set_yticks([0, 1, 2, 3])
    slo = next(iter(chosen.values())).slo_s
    axes[3].axhline(slo, color=TEXT_2, linestyle="--", linewidth=1.5)
    axes[3].annotate(f"SLO {slo:g} s", (1.0, slo), xycoords=("axes fraction", "data"), xytext=(4, 0),
                     textcoords="offset points", va="center", color=TEXT_2, fontsize=13)
    axes[3].set_ylabel("p95 TTFT (s)")
    axes[3].set_xlabel("time since run start (s)")
    for ax in axes:
        ax.axvspan(120, 420, color="#f2f2f2", zorder=0)
        ax.set_ylim(bottom=0)
    axes[3].set_xlim(0, max(float(r.meta["end"]) - float(r.meta["start"]) for r in chosen.values()))
    axes[1].legend(loc="lower left", bbox_to_anchor=(0, 1.02), ncol=2, fontsize=13)
    save(fig, out)


# ---------------------------------------------------------------- 2 and 3 grouped bars

def chart_by_pattern(per_run: pd.DataFrame, column: str, ylabel: str, title: str, out: Path,
                     scale: float = 1.0) -> None:
    df = per_run[(per_run["study"] == "main") & per_run["strategy"].isin(["a", "b"])]
    if df.empty:
        print(f"  skip {out.name}: no main study runs yet")
        return
    fig, ax = plt.subplots(figsize=(12, 6))
    width = 0.36
    x = np.arange(len(PATTERNS))
    for i, s in enumerate(["a", "b"]):
        means, lo, hi = [], [], []
        for p in PATTERNS:
            vals = df[(df["strategy"] == s) & (df["pattern"] == p)][column].dropna() * scale
            m, l, h = whiskers(vals) if len(vals) else (np.nan, 0, 0)
            means.append(m); lo.append(l); hi.append(h)
        pos = x + (i - 0.5) * (width + 0.02)
        ax.bar(pos, means, width, color=STRATEGY_COLOR[s], label=STRATEGY_LABEL[s])
        ax.errorbar(pos, means, yerr=[lo, hi], fmt="none", ecolor=TEXT, elinewidth=1.5, capsize=6)
        for xp, m, h in zip(pos, means, hi):
            if not np.isnan(m):
                ax.annotate(f"{m:.1f}" if scale != 1 else f"{m:.2f}", (xp, m + h), xytext=(0, 6),
                            textcoords="offset points", ha="center", va="bottom", fontsize=13, color=TEXT)
    ax.set_xticks(x, PATTERNS)
    ax.set_ylabel(ylabel)
    ax.set_title(title)
    ax.grid(axis="x", visible=False)
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper left")
    ax.text(1.0, -0.12, "bars: mean over repeats; whiskers: min to max", transform=ax.transAxes,
            ha="right", fontsize=12, color=TEXT_2)
    save(fig, out)


# ---------------------------------------------------------------- 4 cold start

def chart_coldstart(cold: pd.DataFrame, out: Path) -> None:
    if cold.empty:
        print("  skip 4_coldstart: no scale ups yet")
        return
    df = cold[cold["study"].isin(["main", "warm"]) & cold["strategy"].isin(["a", "b"])].copy()
    if df.empty:
        print("  skip 4_coldstart: no scale ups in main or warm")
        return
    df["group"] = df.apply(lambda r: f"{r['strategy'].upper()}, {'new node' if r['new_node'] else 'existing node'}", axis=1)
    groups = sorted(df["group"].unique())
    means = df.groupby("group")[[p for p, _, _ in PHASES]].mean().reindex(groups).fillna(0)
    counts = df.groupby("group").size().reindex(groups)
    fig, ax = plt.subplots(figsize=(13, 1.3 * len(groups) + 2.2))
    left = np.zeros(len(groups))
    for col, label, color in PHASES:
        vals = means[col].to_numpy()
        ax.barh(groups, vals, left=left, color=color, label=label, edgecolor="white", linewidth=2, height=0.6)
        left += vals
    for y, (total, n) in enumerate(zip(left, counts)):
        ax.annotate(f"{total:.0f} s  (n={n})", (total, y), xytext=(6, 0), textcoords="offset points",
                    va="center", fontsize=13, color=TEXT)
    ax.invert_yaxis()
    ax.set_xlabel("mean seconds from metric above target to replica Ready")
    ax.set_title("Where scale up time goes")
    ax.grid(axis="y", visible=False)
    ax.set_xlim(0, left.max() * 1.2)
    ax.legend(loc="upper left", bbox_to_anchor=(0, -0.16), ncol=len(PHASES), fontsize=13)
    save(fig, out)


# ---------------------------------------------------------------- 5 warm spare

def chart_warm_spare(per_run: pd.DataFrame, out: Path) -> None:
    df = per_run[per_run["study"] == "warm"].copy()
    if df.empty:
        print("  skip 5_warm_spare: no warm study runs yet")
        return
    df["spare"] = df["variant"].str.extract(r"spare(\d)")[0].astype(float)
    fig, axes = plt.subplots(1, 2, figsize=(13, 5.5), gridspec_kw={"wspace": 0.35})
    for ax, col, label in [(axes[0], "time_to_scale_s", "time to first new replica (s)"),
                           (axes[1], "gpu_hours", "GPU hours per run")]:
        stats = [whiskers(df[df["spare"] == s][col].dropna()) if (df["spare"] == s).any() else (np.nan, 0, 0)
                 for s in (0, 1)]
        means = [m for m, _, _ in stats]
        ax.bar([0, 1], means, 0.55, color=STRATEGY_COLOR["b"])
        ax.errorbar([0, 1], means, yerr=[[l for _, l, _ in stats], [h for _, _, h in stats]],
                    fmt="none", ecolor=TEXT, elinewidth=1.5, capsize=6)
        for xp, (m, _, h) in enumerate(stats):
            if not np.isnan(m):
                ax.annotate(f"{m:.2f}" if col == "gpu_hours" else f"{m:.0f}", (xp, m + h), xytext=(0, 6),
                            textcoords="offset points", ha="center", fontsize=13)
        ax.set_xticks([0, 1], ["no spare", "1 warm spare"])
        ax.set_ylabel(label)
        ax.grid(axis="x", visible=False)
        ax.set_ylim(bottom=0)
    axes[0].set_title("Warm spare GPU node, Strategy B, spike")
    save(fig, out)


# ---------------------------------------------------------------- 6 threshold sweep

def chart_threshold_sweep(per_run: pd.DataFrame, out: Path) -> None:
    df = per_run[per_run["study"] == "sweep"].copy()
    if df.empty:
        print("  skip 6_threshold_sweep: no sweep study runs yet")
        return
    df["threshold"] = df["variant"].str.extract(r"t([\d.]+)")[0].astype(float)
    fig, ax = plt.subplots(figsize=(11, 6.5))
    ax.scatter(df["gpu_hours"], df["slo_violation_rate"] * 100, s=60, color=STRATEGY_COLOR["b"], alpha=0.35,
               edgecolor="white", linewidth=2, label="single run")
    mean = df.groupby("threshold")[["gpu_hours", "slo_violation_rate"]].mean().sort_index()
    ax.plot(mean["gpu_hours"], mean["slo_violation_rate"] * 100, color=STRATEGY_COLOR["b"], marker="o",
            markersize=11, markeredgecolor="white", markeredgewidth=2, label="mean per threshold")
    for i, (th, row) in enumerate(mean.iterrows()):
        ax.annotate(f"threshold {th:g}", (row["gpu_hours"], row["slo_violation_rate"] * 100),
                    xytext=(10, 8 if i % 2 == 0 else -22),
                    textcoords="offset points", fontsize=13)
    ax.set_xlabel("GPU hours per run")
    ax.set_ylabel("SLO violation rate (%)")
    ax.set_title("Strategy B threshold: cost against latency, spike")
    ax.set_ylim(bottom=0)
    ax.legend(loc="upper right")
    save(fig, out)


# ---------------------------------------------------------------- main

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--platform", default="gcp")
    p.add_argument("--results", type=Path, default=rd.REPO_ROOT / "results")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    here = Path(__file__).resolve().parent
    real = args.platform == "gcp"
    figures = here / "figures" if real else here / "figures" / args.platform
    summary_path = here / ("summary_table.csv" if real else f"summary_table_{args.platform}.csv")
    figures.mkdir(parents=True, exist_ok=True)
    style()

    runs = rd.load_runs(args.platform, args.results)
    print(f"{len(runs)} complete runs under {args.results / args.platform}")
    if not runs:
        print("nothing to chart yet")
        return 0
    per_run = rd.metrics_table(runs)
    cold = rd.coldstart_table(runs)
    per_run.round(4).to_csv(summary_path.with_name(summary_path.name.replace("summary_table", "per_run_metrics")),
                            index=False)
    rd.summary_table(per_run).to_csv(summary_path, index=False)
    print(f"  wrote {summary_path}")

    chart_spike_timeline(runs, figures / "1_spike_timeline.png")
    chart_by_pattern(per_run, "slo_violation_rate", "SLO violation rate (%)\nTTFT over 2 s or failed",
                     "SLO violations by traffic pattern", figures / "2_slo_violations.png", scale=100)
    chart_by_pattern(per_run, "gpu_hours", "GPU hours per run", "GPU cost by traffic pattern",
                     figures / "3_gpu_hours.png")
    chart_coldstart(cold, figures / "4_coldstart.png")
    chart_warm_spare(per_run, figures / "5_warm_spare.png")
    chart_threshold_sweep(per_run, figures / "6_threshold_sweep.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
