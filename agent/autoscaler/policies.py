"""Policies return a target allocation (units). Baselines mirror the simulator's, adapted to real metrics."""
from __future__ import annotations

import math

from .prom import Metrics


class RLPolicy:
    name = "rl"

    def __init__(self, model, state_builder):
        self.model, self.state = model, state_builder

    def decide(self, m: Metrics, units: int, pending: int = 0) -> tuple[int, float, list]:
        # units = allocation asked for; the model (like the simulator) sees ready units and pending ones apart
        obs = self.state.build(m, units - pending, pending)
        delta, raw = self.model.decide(obs)
        return units + delta, raw, obs.tolist()


class ThresholdPolicy:
    """HPA-like: desired = ceil(ready units * cpu_util / target), 10 % tolerance, scale down only after the
    last `window` decisions agree (30 s); units still starting are never cancelled while above target.
    The CPU quota throttles, so utilisation can't exceed 100 %: one step grows at most by 1/target (≈1.43×),
    like HPA with limit == request."""
    name = "threshold"

    def __init__(self, cpus_per_unit: float, target: float = 0.70, tolerance: float = 0.10, window: int = 6):
        self.cpus_per_unit, self.target, self.tolerance, self.window = cpus_per_unit, target, tolerance, window
        self.history: list[int] = []

    def decide(self, m: Metrics, units: int, pending: int = 0) -> tuple[int, float, list]:
        ready = max(units - pending, 1)
        util = m.cpu_cores / max(ready * self.cpus_per_unit, 1e-6)
        ratio = util / self.target
        desired = units if abs(ratio - 1.0) <= self.tolerance else math.ceil(ready * ratio)
        if ratio > 1.0:
            desired = max(desired, units)
        self.history = (self.history + [desired])[-self.window:]
        return max(self.history), util, []


class StaticPolicy:
    """Fixed allocation (the simulator's round-robin/static baseline uses half of max)."""
    name = "static"

    def __init__(self, units: int):
        self.units = units

    def decide(self, m: Metrics, units: int, pending: int = 0) -> tuple[int, float, list]:
        return self.units, 0.0, []


class ObservePolicy:
    """Changes nothing: another controller scales (k8s tier: the cluster's HorizontalPodAutoscaler,
    apps/image-service/k8s/testbed/hpa.yaml). The run still logs every 5 s step for the comparison."""
    name = "hpa"

    def decide(self, m: Metrics, units: int, pending: int = 0) -> tuple[int, float, list]:
        return units, 0.0, []
