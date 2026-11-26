"""Turn the Azure Functions Invocation Trace 2021 into 5-minute traffic windows for the simulator.

    python training/build_trace_windows.py <folder with the extracted trace>

Source: Azure Public Dataset, AzureFunctionsInvocationTrace2021 (two weeks from 2021-01-31, one row per
function invocation: app, func, end_timestamp, duration). Licence CC-BY 4.0. Cite: Y. Zhang et al.,
"Faster and Cheaper Serverless Computing on Harvested Resources", SOSP 2021.
https://github.com/Azure/AzurePublicDataset/blob/master/AzureFunctionsInvocationTrace2021.md

What it writes (runs in GitHub Actions, see .github/workflows/build-traces.yml):

* training/data/azure2021_windows.csv.gz: one row per window: split (train/test), source (an app rank or
  "all"), start time (s), mean req/s, then w00..w59 = request count per 5 s step divided by the window's
  maximum (so 1.0 = the window's peak; the simulator rescales it to the tier's capacity).
* loadgen/traces.json: a few held-out windows for replaying the same real traffic on AWS with Locust.

Split by time: days 1-10 train, days 11-14 test, so evaluation never sees traffic the model trained on.
Windows come from the busiest apps (bursty) and from the sum over all apps (smooth, with daily cycles).
"""
from __future__ import annotations

import glob
import gzip
import json
import os
import sys

import numpy as np
import pandas as pd

STEP_S = 5            # simulator decision interval
WINDOW = 60           # steps per episode (5 minutes)
TOP_APPS = 40         # busiest apps used as separate sources
MIN_MEAN_COUNT = 2.0  # windows need on average >= 2 invocations per 5 s step (0.4 req/s)
TRAIN_DAYS = 10
MAX_PER_SOURCE = {"train": 40, "test": 15}
SEED = 2021
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def load(folder: str) -> pd.DataFrame:
    files = [f for f in glob.glob(os.path.join(folder, "**", "*"), recursive=True)
             if os.path.isfile(f) and f.lower().endswith((".csv", ".txt"))]
    if not files:
        sys.exit(f"no .csv/.txt file under {folder}")
    path = max(files, key=os.path.getsize)
    print("reading", path, f"({os.path.getsize(path) / 1e6:.1f} MB)")
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    need = {"app", "end_timestamp", "duration"}
    if not need <= set(df.columns):
        sys.exit(f"unexpected columns {list(df.columns)}")
    # The docs disagree on seconds vs milliseconds: a 14-day trace spans ~1.2e6 s (or ~1.2e9 ms).
    span = float(df["end_timestamp"].max() - df["end_timestamp"].min())
    scale = 1e-3 if span > 1e8 else 1.0
    print(f"rows {len(df):,}  apps {df['app'].nunique():,}  span {span:,.0f} -> unit "
          f"{'ms' if scale < 1 else 's'}")
    df["start_s"] = (df["end_timestamp"] - df["duration"]) * scale
    df["start_s"] -= df["start_s"].min()
    return df[["app", "start_s"]]


def windows_for(counts: np.ndarray, source: str, rng: np.random.Generator) -> list[dict]:
    n_win = len(counts) // WINDOW
    w = counts[: n_win * WINDOW].reshape(n_win, WINDOW).astype(float)
    starts = np.arange(n_win) * WINDOW * STEP_S
    rows = []
    for split in ("train", "test"):
        in_split = (starts < TRAIN_DAYS * 86400) if split == "train" else (starts >= TRAIN_DAYS * 86400)
        ok = np.where(in_split & (w.mean(axis=1) >= MIN_MEAN_COUNT))[0]
        if len(ok) == 0:
            continue
        pick = rng.choice(ok, size=min(len(ok), MAX_PER_SOURCE[split]), replace=False)
        for i in sorted(pick):
            peak = w[i].max()
            rows.append(dict(split=split, source=source, start_s=int(starts[i]),
                             mean_rps=round(float(w[i].mean() / STEP_S), 3),
                             **{f"w{j:02d}": round(float(v / peak), 4) for j, v in enumerate(w[i])}))
    return rows


def burstiness(row: dict) -> float:
    v = np.array([row[f"w{j:02d}"] for j in range(WINDOW)])
    return float(v.std() / max(v.mean(), 1e-9))


def main(folder: str) -> None:
    df = load(folder)
    nbins = int(df["start_s"].max() // STEP_S) + 1
    rng = np.random.default_rng(SEED)
    rows = windows_for(np.bincount((df["start_s"] // STEP_S).astype(int), minlength=nbins), "all", rng)
    top = df["app"].value_counts().index[:TOP_APPS]
    for rank, app in enumerate(top, 1):
        b = (df.loc[df["app"] == app, "start_s"] // STEP_S).astype(int)
        rows += windows_for(np.bincount(b, minlength=nbins), f"app{rank:02d}", rng)
    out = pd.DataFrame(rows)
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    path = os.path.join(HERE, "data", "azure2021_windows.csv.gz")
    with gzip.open(path, "wt", newline="") as f:
        out.to_csv(f, index=False)
    print(f"wrote {path}: {len(out)} windows "
          f"(train {int((out.split == 'train').sum())}, test {int((out.split == 'test').sum())}, "
          f"sources {out.source.nunique()})")

    # Three held-out windows for AWS replay: the calmest, a typical and the burstiest test window.
    test = [r for r in rows if r["split"] == "test"]
    test.sort(key=burstiness)
    picks = {"azure_calm": test[len(test) // 10], "azure_typical": test[len(test) // 2],
             "azure_bursty": test[(9 * len(test)) // 10]}
    traces = {name: dict(source=r["source"], start_s=r["start_s"], burstiness=round(burstiness(r), 3),
                         shape=[r[f"w{j:02d}"] for j in range(WINDOW)]) for name, r in picks.items()}
    traces["_about"] = ("Held-out 5-minute windows (60 x 5 s, normalised to the window peak) from the Azure "
                        "Functions Invocation Trace 2021, CC-BY 4.0; see training/build_trace_windows.py")
    with open(os.path.join(REPO, "loadgen", "traces.json"), "w") as f:
        json.dump(traces, f, indent=1)
    print("wrote loadgen/traces.json:", {k: v["burstiness"] for k, v in traces.items() if k != "_about"})


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "trace")
