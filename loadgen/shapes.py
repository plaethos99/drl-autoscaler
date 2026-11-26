"""Load shapes shared by the Locust file and the demo dashboard (same shapes as the simulator's workloads).

The azure_* scenarios replay held-out 5-minute windows of real traffic from the Azure Functions Invocation
Trace 2021 (CC-BY 4.0; see training/build_trace_windows.py), stored in traces.json next to this file.
"""
import json
import math
import os

_TRACES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "traces.json")
try:
    with open(_TRACES_PATH) as _f:
        TRACES = {k: v["shape"] for k, v in json.load(_f).items() if not k.startswith("_")}
except (OSError, ValueError, KeyError):
    TRACES = {}

SCENARIOS = ["paper5min", "steady", "periodic", "ramp", "spike", "double_spike", "sawtooth"] + sorted(TRACES)


def users_at(t: float, scenario: str = "paper5min", duration_s: float = 300.0) -> float:
    """Fraction of the peak user count at second t of a run (shapes are defined on 300 s and stretched
    to duration_s)."""
    s = t * 300.0 / duration_s
    if scenario in TRACES:                           # 60 points, one per 5 s of the 300 s window
        shape = TRACES[scenario]
        return max(0.03, shape[min(int(s // 5), len(shape) - 1)])
    if scenario == "steady":
        return 0.5
    if scenario == "periodic":                       # 3 waves
        return 0.2 + 0.35 * (1 + math.sin(2 * math.pi * 3 * s / 300))
    if scenario == "ramp":
        return 0.1 + 0.8 * s / 300
    if scenario == "spike":
        return 0.95 if 120 <= s < 160 else 0.15
    if scenario == "double_spike":
        return 0.95 if (70 <= s < 95 or 200 <= s < 225) else 0.15
    if scenario == "sawtooth":
        return 0.15 + 0.7 * ((s % 75) / 75)
    # paper5min: quiet -> ramp -> flash-sale spike -> waves
    if s < 60:
        return 0.15
    if s < 120:
        return 0.15 + (0.5 - 0.15) * (s - 60) / 60
    if s < 180:
        return 0.95
    return 0.35 + 0.2 * math.sin(2 * math.pi * (s - 180) / 40)
