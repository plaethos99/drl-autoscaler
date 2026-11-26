"""Figures for the model-v0.2 AWS results (docs/aws-results-v0.2/figures/).

    python docs/aws-results-v0.2/make_figures.py

Reads docs/aws-results-v0.2/summary.csv (this test), docs/aws-results-2026-11-26/summary.csv (model-v0.1,
earlier session) and docs/sim-results-v0.2/sim_eval_results.csv (simulator, per episode). Needs matplotlib.
"""
from __future__ import annotations

import csv
import statistics as st
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

DOCS = Path(__file__).resolve().parent.parent
OUT = DOCS / "aws-results-v0.2" / "figures"

# Colours: first three categorical slots of the validated default palette (CVD-safe across all pairs).
SURFACE, INK, INK2, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"
METHODS = [("rl", "RL (DDPG)", "#2a78d6"), ("threshold", "HPA-like threshold", "#eb6834"),
           ("static", "Static (4 units)", "#1baf7a")]
SCEN = [("paper5min", "Generated pattern\n(paper5min)"), ("azure_typical", "Real traffic, held out\n(azure_typical)")]

plt.rcParams.update({
    "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
    "axes.edgecolor": INK2, "axes.labelcolor": INK, "xtick.color": INK2, "ytick.color": INK2,
    "text.color": INK, "font.size": 9, "axes.titlesize": 10, "axes.titleweight": "bold",
    "axes.spines.top": False, "axes.spines.right": False, "axes.grid": True, "axes.grid.axis": "y",
    "grid.color": GRID, "grid.linewidth": 0.8, "axes.axisbelow": True, "legend.frameon": False,
})


def load(path):
    rows = [r for r in csv.DictReader(open(path)) if r["shadow"] == "0" and "calibrate" not in r["run_id"]]
    for r in rows:
        r["scenario"] = r["run_id"].split("-")[2]
    return rows


def vals(rows, scen, pol, metric):
    return [float(r[metric]) for r in rows if r["scenario"] == scen and r["policy"] == pol]


def legend_top(fig, extra=()):
    handles = [plt.Rectangle((0, 0), 1, 1, color=c) for _, _, c in METHODS] + [h for h, _ in extra]
    labels = [lab for _, lab, _ in METHODS] + [lab for _, lab in extra]
    fig.legend(handles, labels, loc="upper center", ncol=len(labels), bbox_to_anchor=(0.5, 1.0))


def fig_metrics(rows):
    """Bars = mean of 5 runs, dots = each run; three metrics side by side."""
    panels = [("sla_violation_pct", "SLA violations (% of 5 s steps)", "{:.1f}"),
              ("mean_p95_ms", "Mean p95 latency (ms)", "{:.0f}"),
              ("avg_units", "CPU allocated (avg units; 1 = 0.05 vCPU)", "{:.2f}")]
    fig, axes = plt.subplots(1, 3, figsize=(11, 3.8))
    w = 0.26
    for ax, (metric, title, fmt) in zip(axes, panels):
        for si, (sc, _) in enumerate(SCEN):
            for mi, (pol, _, col) in enumerate(METHODS):
                v = vals(rows, sc, pol, metric)
                x = si + (mi - 1) * (w + 0.02)
                m = st.mean(v)
                ax.bar(x, m, w, color=col, linewidth=0)
                ax.scatter([x + (k - 2) * 0.035 for k in range(len(v))], v, s=9, color=INK,
                           edgecolors=SURFACE, linewidths=0.6, zorder=3)
                ax.annotate(fmt.format(m), (x, max(max(v), m)), textcoords="offset points", xytext=(0, 3),
                            ha="center", va="bottom", fontsize=8, color=INK2)
        if metric == "mean_p95_ms":
            ax.axhline(500, color=INK2, linestyle=(0, (4, 3)), linewidth=1)
            ax.annotate("SLA: p95 ≤ 500 ms", (-0.42, 500), textcoords="offset points", xytext=(0, 3),
                        ha="left", fontsize=8, color=INK2)
        ax.set_title(title, loc="left")
        ax.set_xticks(range(len(SCEN)), [lab for _, lab in SCEN])
        ax.margins(y=0.12)
    legend_top(fig, [(plt.Line2D([], [], marker="o", color=INK, linestyle="", markersize=3.5), "one 5-min run")])
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(OUT / "aws_v02_metrics.png", dpi=200)
    plt.close(fig)


def fig_tradeoff(rows, rows_v01):
    """Cost vs SLA per run: down-left is better. Hollow = model-v0.1 session (paper5min only)."""
    fig, axes = plt.subplots(1, 2, figsize=(10, 4.2), sharey=True)
    for ax, (sc, title) in zip(axes, SCEN):
        sets = [(rows, True)] + ([(rows_v01, False)] if sc == "paper5min" else [])
        for data, filled in sets:
            for pol, name, col in METHODS:
                x, y = vals(data, sc, pol, "avg_units"), vals(data, sc, pol, "sla_violation_pct")
                if not x:
                    continue
                ax.scatter(x, y, s=34 if filled else 30, color=col if filled else SURFACE, edgecolors=col,
                           linewidths=1.6 if not filled else 0.8, zorder=3, alpha=0.9)
                mx, my = st.mean(x), st.mean(y)
                short = {"rl": "RL", "threshold": "HPA-like", "static": "Static"}[pol]
                label = f"{short} v0.2" if filled and pol == "rl" else short
                if not filled:
                    label = "RL v0.1" if pol == "rl" else f"{short} (v0.1 session)"
                    if pol == "static":
                        continue  # same 4 units, overlaps the v0.2 static runs
                dx = 0.12 if not (pol == "rl" and not filled) else -0.12
                tx, ty, ha = mx + dx, my, "left" if dx > 0 else "right"
                if pol == "threshold" and not filled:
                    label, tx, ty, ha = "HPA-like, v0.1 session", 5.05, 10.5, "left"  # clear of the RL v0.2 runs
                ax.annotate(label, (mx, my), xytext=(tx, ty), fontsize=8, color=INK2, ha=ha, va="center")
        ax.set_title(title.replace("\n", " "), loc="left")
        ax.set_xlabel("Average CPU allocated (units of 0.05 vCPU)  →  more cost")
        ax.set_xlim(3.6, 7.1)
        ax.grid(axis="x", color=GRID, linewidth=0.8)
    axes[0].set_ylabel("SLA violations (% of 5 s steps)  →  worse")
    axes[0].set_ylim(0, 72)
    legend_top(fig, [(plt.Line2D([], [], marker="o", color=INK2, markerfacecolor=SURFACE, linestyle="",
                                 markersize=5), "model-v0.1 session (3 runs)")])
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(OUT / "aws_tradeoff.png", dpi=200)
    plt.close(fig)


def fig_sim_vs_aws(rows):
    """SLA violations of the released VM model and the baselines, simulator vs AWS, same patterns."""
    sim = list(csv.DictReader(open(DOCS / "sim-results-v0.2" / "sim_eval_results.csv")))
    simkey = {"rl": ("DDPG", "2"), "threshold": ("HPA", "-1"), "static": ("RoundRobin-Static", "-1")}
    pairs = [("paper5min", "paper5min", "Generated pattern (paper5min)"),
             ("azure_trace_test", "azure_typical", "Real traffic, held out (Azure trace, days 11–14)")]
    fig, axes = plt.subplots(1, 2, figsize=(9, 3.8), sharey=True)
    w = 0.26
    for ax, (simp, awsp, title) in zip(axes, pairs):
        for gi, where in enumerate(["Simulator", "AWS"]):
            for mi, (pol, _, col) in enumerate(METHODS):
                if where == "Simulator":
                    meth, seed = simkey[pol]
                    v = [float(r["sla_violation_pct"]) for r in sim if r["mode"] == "vm" and r["pattern"] == simp
                         and r["method"] == meth and r["train_seed"] == seed]
                else:
                    v = vals(rows, awsp, pol, "sla_violation_pct")
                x = gi + (mi - 1) * (w + 0.02)
                m = st.mean(v)
                ax.bar(x, m, w, color=col, linewidth=0)
                ax.annotate(f"{m:.1f}", (x, m), textcoords="offset points", xytext=(0, 3), ha="center",
                            fontsize=8, color=INK2)
        sim_lab = "Simulator\n(10 episodes)" if simp == awsp else "Simulator\n(10 random test windows)"
        ax.set_xticks([0, 1], [sim_lab, f"AWS\n(5 runs, {awsp})"])
        ax.set_title(title, loc="left")
    axes[0].set_ylabel("SLA violations (% of 5 s steps)")
    axes[0].set_ylim(0, 70)
    legend_top(fig)
    fig.tight_layout(rect=(0, 0, 1, 0.9))
    fig.savefig(OUT / "sim_vs_aws.png", dpi=200)
    plt.close(fig)


def main():
    OUT.mkdir(exist_ok=True)
    rows = load(DOCS / "aws-results-v0.2" / "summary.csv")
    rows_v01 = load(DOCS / "aws-results-2026-11-26" / "summary.csv")
    fig_metrics(rows)
    fig_tradeoff(rows, rows_v01)
    fig_sim_vs_aws(rows)
    print("wrote", ", ".join(p.name for p in sorted(OUT.glob("*.png"))))


if __name__ == "__main__":
    main()
