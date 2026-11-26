"""Summarise runs with the same metrics the simulator reports (per 5 s step)."""
from __future__ import annotations

import csv
import sqlite3

from .config import SLA_P95_MS, TIERS

COLUMNS = ["run_id", "policy", "shadow", "steps", "sla_violation_pct", "mean_p95_ms", "max_p95_ms",
           "avg_units", "cost", "scaling_actions", "failed_reads"]


def summarise(rows: list[dict], unit_cost: float) -> dict:
    ok = [r for r in rows if r["p95_ms"] is not None]
    n = max(len(ok), 1)
    return dict(
        steps=len(rows),
        sla_violation_pct=round(100.0 * sum(r["p95_ms"] > SLA_P95_MS for r in ok) / n, 2),
        mean_p95_ms=round(sum(r["p95_ms"] for r in ok) / n, 1),
        max_p95_ms=round(max((r["p95_ms"] for r in ok), default=0.0), 1),
        avg_units=round(sum(r["units_after"] for r in rows) / max(len(rows), 1), 2),
        cost=round(sum(r["units_after"] * unit_cost for r in rows), 2),
        scaling_actions=sum(1 for r in rows if r["applied"]),
        failed_reads=len(rows) - len(ok),
    )


def estimate_mu(db_path: str, run_id: str, units: int = 1) -> float:
    """Calibration: highest request rate one allocation served while p95 stayed within the SLA,
    divided by the units used. Run with a fixed allocation and a ramp load (scripts/run_test.sh calibrate)."""
    db = sqlite3.connect(db_path)
    rows = db.execute("SELECT rps, p95_ms, units_after FROM decisions WHERE run_id=? AND p95_ms IS NOT NULL",
                      (run_id,)).fetchall()
    good = [rps / u for rps, p95, u in rows if p95 <= SLA_P95_MS and u == units and rps > 0]
    if not good:
        raise SystemExit(f"no steps of run {run_id} with {units} unit(s) and p95 <= {SLA_P95_MS} ms")
    mu = max(good)
    worst = max((p95 for _, p95, _ in rows), default=0.0)
    print(f"run {run_id}: max {mu:.2f} req/s per unit within the SLA "
          f"(worst p95 in the run: {worst:.0f} ms; if it never exceeded {SLA_P95_MS:.0f} ms, raise the load)")
    print(f"export MU_PER_UNIT={mu:.2f}")
    return mu


def report(db_path: str, run_ids=None, csv_path=None) -> list[dict]:
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    runs = db.execute("SELECT * FROM runs ORDER BY started").fetchall()
    out = []
    for run in runs:
        if run_ids and run["run_id"] not in run_ids:
            continue
        rows = [dict(r) for r in db.execute("SELECT * FROM decisions WHERE run_id=? ORDER BY step", (run["run_id"],))]
        s = dict(run_id=run["run_id"], policy=run["policy"], shadow=run["shadow"],
                 **summarise(rows, TIERS[run["tier"]]["unit_cost"]))
        out.append(s)
    print(" | ".join(COLUMNS))
    for s in out:
        print(" | ".join(str(s[c]) for c in COLUMNS))
    if csv_path:
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=COLUMNS)
            w.writeheader()
            w.writerows(out)
    return out
