"""CLI.

  python -m autoscaler run --policy rl --duration 300            # RL model controls the classifier
  python -m autoscaler run --policy rl --shadow                   # log what the model would do, change nothing
  python -m autoscaler run --policy threshold --duration 300      # HPA-like baseline
  python -m autoscaler run --policy static --duration 300         # fixed allocation baseline
  python -m autoscaler report                                     # compare runs in decisions.sqlite
  python -m autoscaler mu <run_id>                                # calibration: req/s per unit
"""
from __future__ import annotations

import argparse
import logging

from .config import Settings


def main(argv=None):
    ap = argparse.ArgumentParser(prog="autoscaler")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="run the control loop")
    r.add_argument("--tier", default="vm", choices=["vm"])
    r.add_argument("--policy", default="rl", choices=["rl", "threshold", "static"])
    r.add_argument("--shadow", action="store_true", help="rl only: log decisions without applying them")
    r.add_argument("--duration", type=float, default=0.0, help="seconds (0 = until Ctrl+C)")
    r.add_argument("--run-id", default="")
    r.add_argument("--start-units", type=int, default=0, help="set this allocation before starting")
    r.add_argument("--static-units", type=int, default=0)
    r.add_argument("--mu", type=float, help="requests/s per unit (overrides MU_PER_UNIT)")
    r.add_argument("--release", help="model release tag (overrides MODEL_RELEASE)")
    r.add_argument("--db", help="SQLite path (overrides DECISION_DB)")

    rep = sub.add_parser("report", help="summarise runs from the decision log")
    rep.add_argument("--db", default=None)
    rep.add_argument("--runs", nargs="*", help="run ids (default: all)")
    rep.add_argument("--csv", help="also write the summary to this CSV file")

    mu = sub.add_parser("mu", help="estimate requests/s per unit from a calibration run")
    mu.add_argument("run_id")
    mu.add_argument("--db", default=None)
    mu.add_argument("--units", type=int, default=1)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if a.cmd == "run":
        from .controller import run
        s = Settings(tier=a.tier, policy=a.policy, shadow=a.shadow, duration_s=a.duration, run_id=a.run_id,
                     start_units=a.start_units, static_units=a.static_units)
        if a.mu:
            s.mu_per_unit = a.mu
        if a.release:
            s.model_release = a.release
        if a.db:
            s.db_path = a.db
        if a.shadow and a.policy != "rl":
            ap.error("--shadow only applies to --policy rl")
        print(run(s))
    elif a.cmd == "mu":
        from .report import estimate_mu
        estimate_mu(a.db or Settings().db_path, a.run_id, a.units)
    else:
        from .report import report
        report(a.db or Settings().db_path, a.runs, a.csv)


if __name__ == "__main__":
    main()
