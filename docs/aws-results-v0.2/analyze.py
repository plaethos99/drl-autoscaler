"""Summarise the AWS runs in summary.csv: mean ± SD per scenario and method, Welch t-test and exact
Mann-Whitney U test (two-sided) of RL against each baseline. Pure Python, no scipy needed.

    python docs/aws-results-v0.2/analyze.py docs/aws-results-v0.2/summary.csv
"""
from __future__ import annotations

import csv
import itertools
import math
import statistics as st
import sys

METRICS = ["sla_violation_pct", "mean_p95_ms", "max_p95_ms", "avg_units", "cost", "scaling_actions"]


def _betacf(a, b, x, it=200, eps=1e-12):
    qab, qap, qam = a + b, a + 1, a - 1
    c, d = 1.0, 1 - qab * x / qap
    d = 1 / (d if abs(d) > 1e-300 else 1e-300)
    h = d
    for m in range(1, it + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-300 else 1e-300
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1 + aa * d; d = 1 / (d if abs(d) > 1e-300 else 1e-300)
        c = 1 + aa / c if abs(1 + aa / c) > 1e-300 else 1e-300
        de = d * c
        h *= de
        if abs(de - 1) < eps:
            break
    return h


def _ibeta(a, b, x):
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    lb = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b) + a * math.log(x) + b * math.log(1 - x)
    if x < (a + 1) / (a + b + 2):
        return math.exp(lb) * _betacf(a, b, x) / a
    return 1 - math.exp(lb) * _betacf(b, a, 1 - x) / b


def welch_p(x, y):
    mx, my, vx, vy = st.mean(x), st.mean(y), st.variance(x), st.variance(y)
    se2 = vx / len(x) + vy / len(y)
    if se2 == 0:
        return 1.0 if mx == my else 0.0
    t = (mx - my) / math.sqrt(se2)
    df = se2 ** 2 / ((vx / len(x)) ** 2 / (len(x) - 1) + (vy / len(y)) ** 2 / (len(y) - 1))
    return _ibeta(df / 2, 0.5, df / (df + t * t))


def mannwhitney_p(x, y):
    """Exact two-sided permutation p-value of U (ties count half)."""
    def u(a, b):
        return sum((ai > bi) + 0.5 * (ai == bi) for ai in a for bi in b)
    allv, n = list(x) + list(y), len(x)
    centre = len(x) * len(y) / 2
    obs = abs(u(x, y) - centre)
    hits = tot = 0
    for idx in itertools.combinations(range(len(allv)), n):
        s = set(idx)
        a = [allv[i] for i in idx]
        b = [allv[i] for i in range(len(allv)) if i not in s]
        tot += 1
        hits += abs(u(a, b) - centre) >= obs - 1e-9
    return hits / tot


def fmt_p(p):
    return "< 0.001" if p < 0.001 else f"{p:.3f}"


def main(path):
    rows = [r for r in csv.DictReader(open(path)) if r["shadow"] == "0" and "calibrate" not in r["run_id"]]
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        scen = r["run_id"].split("-")[2]
        groups.setdefault((scen, r["policy"]), []).append(r)
    scens = sorted({s for s, _ in groups})
    for s in scens:
        print(f"\n### {s}\n")
        print("| Method | n | " + " | ".join(METRICS) + " |")
        print("|---" * (len(METRICS) + 2) + "|")
        for p in ("rl", "threshold", "static"):
            g = groups.get((s, p), [])
            if not g:
                continue
            cells = []
            for m in METRICS:
                v = [float(r[m]) for r in g]
                cells.append(f"{st.mean(v):.2f} ± {st.stdev(v):.2f}" if len(v) > 1 else f"{v[0]:.2f}")
            print(f"| {p} | {len(g)} | " + " | ".join(cells) + " |")
        print("\n| Metric | RL vs threshold: Welch / MWU | RL vs static: Welch / MWU |\n|---|---|---|")
        for m in METRICS[:5]:
            out = []
            rl = [float(r[m]) for r in groups.get((s, "rl"), [])]
            for b in ("threshold", "static"):
                ob = [float(r[m]) for r in groups.get((s, b), [])]
                if len(rl) > 1 and len(ob) > 1:
                    d = (st.mean(rl) - st.mean(ob)) / st.mean(ob) * 100 if st.mean(ob) else float("nan")
                    out.append(f"{fmt_p(welch_p(rl, ob))} / {fmt_p(mannwhitney_p(rl, ob))} (RL {d:+.0f} %)")
                else:
                    out.append("n/a")
            print(f"| {m} | " + " | ".join(out) + " |")


if __name__ == "__main__":
    main(sys.argv[1])
