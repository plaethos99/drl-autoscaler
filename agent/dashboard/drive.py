"""Play one load scenario on a remote Locust (the load-generator box), for scripted runs.

  python -m dashboard.drive --url http://<loadgen>:8089 --scenario paper5min --peak 51 --duration 300

Sets the user count every 2 s from loadgen/shapes.py (same shapes as the Locust file), stops the load at the
end and prints Locust's totals. Used by scripts/run_test.sh when LOADGEN_URL is set.
"""
from __future__ import annotations

import argparse
import time

import requests

from .app import TICK_S, Locust, users_at


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(prog="dashboard.drive")
    ap.add_argument("--url", required=True)
    ap.add_argument("--scenario", default="paper5min")
    ap.add_argument("--peak", type=int, required=True)
    ap.add_argument("--duration", type=float, default=300)
    a = ap.parse_args(argv)

    locust, last = Locust(a.url), -1
    t0 = time.monotonic()
    try:
        while (t := time.monotonic() - t0) < a.duration:
            users = max(1, round(a.peak * users_at(t, a.scenario, a.duration)))
            if users != last:
                locust.set_users(users)
                last = users
            time.sleep(TICK_S)
    finally:
        try:
            stats = requests.get(f"{locust.url}/stats/requests", timeout=5).json()
            total = next((s for s in stats.get("stats", []) if s.get("name") == "Aggregated"), {})
            print(f"locust totals: requests={total.get('num_requests')} failures={total.get('num_failures')} "
                  f"median_ms={total.get('median_response_time')} avg_ms={round(total.get('avg_response_time') or 0)}")
        except (requests.RequestException, ValueError) as exc:
            print(f"locust totals unavailable: {exc}")
        locust.stop()


if __name__ == "__main__":
    main()
