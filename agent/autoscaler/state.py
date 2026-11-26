"""Turn live metrics into the 9-value observation the model was trained on.

The order and scaling must match CloudScalingEnv._obs() in training/drl_autoscaler_training.py
(and metadata.json["observation"]). Sim-to-real notes:
  * cpu       sim: served / capacity of allocated units.  real: classifier cores used / cores allocated.
  * mem       sim: a synthetic function of cpu and queue (it never measured memory). Feeding real memory
              would put the model outside its training distribution, so the same formula is used here;
              real memory is still logged.
  * queue     sim: backlog not yet served.  real: classifier in-flight requests (includes ones in service).
  * pending   VM quota changes are instant -> always 0.   cold_frac: no cold starts on a VM -> 0.
"""
from __future__ import annotations

import math

import numpy as np

from .config import DT_S, SLA_P95_MS
from .prom import Metrics

OBS_ORDER = ["cpu_util", "mem_util", "queue_norm", "p95_over_sla", "rps_over_capacity",
             "rps_trend", "units_over_max", "pending_over_max", "cold_start_frac"]


class StateBuilder:
    def __init__(self, max_units: int, cpus_per_unit: float, mu_per_unit: float, expected_order=None):
        if expected_order is not None and list(expected_order) != OBS_ORDER:
            raise ValueError(f"model expects observation order {expected_order}, controller builds {OBS_ORDER}")
        self.max_units = max_units
        self.cpus_per_unit = cpus_per_unit
        self.cap = max_units * mu_per_unit          # requests/s at max allocation (same as the simulator)
        self.prev_rps: float | None = None

    def build(self, m: Metrics, units: int, pending: int = 0, cold_frac: float = 0.0) -> np.ndarray:
        if any(math.isnan(x) for x in (m.rps, m.p95_ms, m.cpu_cores)):
            raise ValueError("metrics contain NaN")
        alloc_cores = max(units * self.cpus_per_unit, 1e-6)
        cpu = min(1.0, max(0.0, m.cpu_cores / alloc_cores))
        queue_norm = min(m.inflight / (self.cap * DT_S), 3.0)
        mem = min(1.0, 0.25 + 0.6 * cpu + 0.1 * min(m.inflight / max(m.rps * DT_S, 1.0), 1.0))
        prev = m.rps if self.prev_rps is None else self.prev_rps
        obs = np.array([
            cpu,
            mem,
            queue_norm,
            min(m.p95_ms / SLA_P95_MS, 4.0),
            m.rps / self.cap,
            float(np.clip((m.rps - prev) / self.cap, -1.0, 1.0)),
            units / self.max_units,
            pending / self.max_units,
            cold_frac,
        ], dtype=np.float32)
        self.prev_rps = m.rps
        return np.clip(obs, -1.0, 5.0)
