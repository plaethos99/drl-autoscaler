"""Policies return a target allocation (units). Baselines mirror the simulator's, adapted to real metrics."""
from __future__ import annotations

import math

from .prom import Metrics


class RLPolicy:
    name = "rl"

    def __init__(self, model, state_builder):
        self.model, self.state = model, state_builder

    def decide(self, m: Metrics, units: int) -> tuple[int, float, list]:
        obs = self.state.build(m, units)
        delta, raw = self.model.decide(obs)
        return units + delta, raw, obs.tolist()


class ThresholdPolicy:
    """HPA-like: desired = ceil(units * cpu_util / target), 10 % tolerance, scale down only after the
    last `window` decisions agree (30 s). On a VM the CPU quota throttles the container, so utilisation
    can't exceed 100 %: one step grows at most by 1/target (≈1.43×), like HPA with limit == request."""
    name = "threshold"

    def __init__(self, cpus_per_unit: float, target: float = 0.70, tolerance: float = 0.10, window: int = 6):
        self.cpus_per_unit, self.target, self.tolerance, self.window = cpus_per_unit, target, tolerance, window
        self.history: list[int] = []

    def decide(self, m: Metrics, units: int) -> tuple[int, float, list]:
        util = m.cpu_cores / max(units * self.cpus_per_unit, 1e-6)
        ratio = util / self.target
        desired = units if abs(ratio - 1.0) <= self.tolerance else math.ceil(units * ratio)
        self.history = (self.history + [desired])[-self.window:]
        return max(self.history), util, []


class StaticPolicy:
    """Fixed allocation (the simulator's round-robin/static baseline uses half of max)."""
    name = "static"

    def __init__(self, units: int):
        self.units = units

    def decide(self, m: Metrics, units: int) -> tuple[int, float, list]:
        return self.units, 0.0, []
