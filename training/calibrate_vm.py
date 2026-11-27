"""Fit the simulator's VM-tier latency model to real AWS decision logs (Phase 2 calibration).

    python training/calibrate_vm.py path/to/decisions.sqlite [--cpus-per-unit 0.05]
    python training/calibrate_vm.py path/to/decisions.sqlite --tier k8s --cpus-per-unit 0.1   # pods

Uses every 5 s step of the measured runs (calibration ramp, rl, threshold, static) with traffic, and compares
two latency models with the same p95 formula the simulator uses,

    p95 = overhead + k_s * service_ms + k_q * wq_ms           (wq from the Sakasegawa M/M/c approximation)

* "parallel"  (simulator v0.1): `units` servers of rate mu each        -> service = 1000 / mu
* "shared"    (one container whose CPU quota grows with units):          one server of rate units * mu
                                                                       -> service = 1000 / (units * mu)

* "random"    (k8s only: kube-proxy sends each connection to a random pod): `units` separate M/M/1 queues,
                                                                       each with rate mu and 1/units of the traffic

k8s tier: units = *ready* pods (pods still starting serve nothing), and the run ids start with "k8s-".

Each model's parameters (mu, overhead, k_s, k_q) are fitted by grid search on log(p95). Steps that are
overloaded (backlog depends on history, not on the current state) or that hit the client timeout are left
out of the fit and reported separately. Prints the fit, the error of both models, a per-allocation check,
and the mu range across runs (for the simulator's domain randomisation).
"""
from __future__ import annotations

import argparse
import math
import sqlite3

import numpy as np


def load(db_path: str, tier: str = "vm", steady: bool = False, window: int = 3, backlog_ms: float = 1500.0):
    """Steps with traffic. steady=True keeps only steps whose ready allocation was the same for the previous
    `window` steps (the 15 s p95/rps window saw one allocation) and none of which had p95 above backlog_ms
    (no backlog left over from an earlier overload, which a stationary queue model can't explain)."""
    db = sqlite3.connect(db_path)
    cols = {r[1] for r in db.execute("PRAGMA table_info(decisions)")}
    ready = "units_before - COALESCE(pending, 0)" if "pending" in cols else "units_before"
    rows = db.execute(
        f"SELECT run_id, step, rps, p95_ms, cpu_cores, {ready} FROM decisions "
        f"WHERE run_id LIKE '{tier}-%' AND shadow = 0 ORDER BY run_id, step").fetchall()
    keep = []
    for i, (r, _, rps, p95, _, u) in enumerate(rows):
        if p95 is None or rps is None or rps <= 0.5:
            continue
        if steady:
            prev = rows[max(0, i - window):i]
            if len(prev) < window or any(p[0] != r or p[5] != u or (p[3] or 0) > backlog_ms for p in prev):
                continue
        keep.append(rows[i])
    run, _, rps, p95, cpu, units = zip(*keep)
    return np.array(run), np.array(rps, float), np.array(p95, float), np.array(cpu, float), np.array(units, float)


def predict(model, rps, units, mu, overhead, ks, kq):
    """p95 (ms) for arrays rps/units and scalar or broadcastable parameters (simulator formula)."""
    if model == "shared":
        c, rate, lam = np.ones_like(units), units * mu, rps
    elif model == "random":      # k8s: kube-proxy sends each connection to a random pod -> `units` M/M/1 queues
        c, rate, lam = np.ones_like(units), mu + 0 * units, rps / units
    else:
        c, rate, lam = units, mu, rps
    rho = np.minimum(lam / (c * rate), 0.98)
    service = 1000.0 / rate
    wq = service * rho ** (np.sqrt(2.0 * (c + 1.0)) - 1.0) / (c * (1.0 - rho))
    return overhead + ks * service + kq * wq


GRID = dict(mu=np.linspace(2.0, 12.0, 101), overhead=np.linspace(0.0, 200.0, 41),
            ks=np.array([0.5, 1.0, 1.5, 2.0, 2.3, 2.6, 3.0]), kq=np.array([0.5, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]))


def fit(model, rps, units, p95):
    y = np.log(p95)
    best = (math.inf, None)
    mu = GRID["mu"][:, None, None]
    ov = GRID["overhead"][None, :, None]
    for ks in GRID["ks"]:
        for kq in GRID["kq"]:
            pred = predict(model, rps[None, None, :], units[None, None, :], mu, ov, ks, kq)
            err = ((np.log(pred) - y[None, None, :]) ** 2).mean(axis=2)
            i, j = np.unravel_index(np.argmin(err), err.shape)
            if err[i, j] < best[0]:
                best = (float(err[i, j]), dict(mu=float(GRID["mu"][i]), overhead=float(GRID["overhead"][j]),
                                              ks=float(ks), kq=float(kq)))
    return best[1], math.sqrt(best[0])


def mu_per_run(run, rps, units, p95, params, model="shared"):
    """mu re-fitted per run with the other parameters fixed: the spread to randomise over in training."""
    out = {}
    for r in np.unique(run):
        s = run == r
        if s.sum() < 10:
            continue
        errs = [((np.log(predict(model, rps[s], units[s], m, params["overhead"], params["ks"], params["kq"]))
                  - np.log(p95[s])) ** 2).mean() for m in GRID["mu"]]
        out[str(r)] = float(GRID["mu"][int(np.argmin(errs))])
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("db")
    ap.add_argument("--tier", default="vm", choices=["vm", "k8s"])
    ap.add_argument("--steady", action="store_true",
                    help="only steps with an unchanged allocation and no leftover backlog over the past 15 s")
    ap.add_argument("--cpus-per-unit", type=float, default=0.05)
    ap.add_argument("--timeout-ms", type=float, default=5000.0, help="leave out steps at/above this p95")
    a = ap.parse_args(argv)

    run, rps, p95, cpu, units = load(a.db, a.tier, a.steady)
    units = np.maximum(units, 1.0)
    cpu_per_req = float(np.median(cpu[rps > 2] / rps[rps > 2]))
    mu_cpu = a.cpus_per_unit / cpu_per_req
    util = rps / (units * mu_cpu)
    keep = (p95 < a.timeout_ms) & (util < 0.95)
    print(f"steps with traffic: {len(rps)}; used for the fit: {int(keep.sum())} "
          f"(left out: {int((~keep).sum())} overloaded or timed out)")
    print(f"CPU per request: {1000 * cpu_per_req:.1f} ms  ->  capacity from CPU: {mu_cpu:.2f} req/s per unit")

    fits = {}
    models = ("parallel", "shared", "random") if a.tier == "k8s" else ("parallel", "shared")
    for model in models:
        params, rmse = fit(model, rps[keep], units[keep], p95[keep])
        pred = predict(model, rps[keep], units[keep], **params)
        mape = float(np.median(np.abs(pred - p95[keep]) / p95[keep]))
        fits[model] = params
        print(f"\n{model:8s} fit: mu={params['mu']:.2f} req/s/unit  overhead={params['overhead']:.0f} ms  "
              f"k_s={params['ks']}  k_q={params['kq']}   RMSE(log)={rmse:.3f}  median |error|={100 * mape:.0f} %")

    best = "random" if a.tier == "k8s" else "shared"   # pods: separate queues, random routing; a VM quota: one
    p = fits[best]
    print("\nper allocation (fitted steps): measured vs predicted median p95, ms")
    print(" units  n   measured  " + "  ".join(f"{m:>8s}" for m in models))
    for u in np.unique(units[keep]):
        s = keep & (units == u)
        if s.sum() < 5:
            continue
        print(f"  {int(u):3d} {int(s.sum()):4d}  {np.median(p95[s]):8.0f}  " + "  ".join(
            f"{np.median(predict(m, rps[s], units[s], **fits[m])):8.0f}" for m in models))

    per_run = mu_per_run(run[keep], rps[keep], units[keep], p95[keep], p, best)
    vals = np.array(list(per_run.values()))
    print(f"\nmu per run ({best} model, other parameters fixed): min {vals.min():.2f}  median {np.median(vals):.2f}  "
          f"max {vals.max():.2f}  over {len(vals)} runs")
    over = ~keep
    if over.any():
        print(f"overloaded/timed-out steps: {int(over.sum())}, median measured p95 {np.median(p95[over]):.0f} ms "
              f"(utilisation {np.median(util[over]):.2f}); the simulator's backlog term covers these")


if __name__ == "__main__":
    main()
