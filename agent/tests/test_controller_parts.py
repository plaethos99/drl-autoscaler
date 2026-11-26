"""Unit tests that need no Prometheus, Docker or PyTorch (they run in GitHub Actions)."""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autoscaler.decisionlog import DecisionLog  # noqa: E402
from autoscaler.guards import Guards  # noqa: E402
from autoscaler.modelstore import delta_from_action  # noqa: E402
from autoscaler.policies import StaticPolicy, ThresholdPolicy  # noqa: E402
from autoscaler.prom import Metrics  # noqa: E402
from autoscaler.report import estimate_mu, report  # noqa: E402
from autoscaler.state import OBS_ORDER, StateBuilder  # noqa: E402

METADATA_ORDER = ["cpu_util", "mem_util", "queue_norm", "p95_over_sla", "rps_over_capacity",
                  "rps_trend", "units_over_max", "pending_over_max", "cold_start_frac"]


def m(rps=20.0, p95=250.0, inflight=2.0, cpu=0.8, mem=1e8):
    return Metrics(rps=rps, p95_ms=p95, inflight=inflight, cpu_cores=cpu, mem_bytes=mem)


def test_observation_order_matches_model_metadata():
    assert OBS_ORDER == METADATA_ORDER
    with pytest.raises(ValueError):
        StateBuilder(8, 0.5, 10.0, expected_order=list(reversed(METADATA_ORDER)))


def test_observation_values_follow_simulator_scaling():
    sb = StateBuilder(max_units=8, cpus_per_unit=0.5, mu_per_unit=10.0, expected_order=METADATA_ORDER)
    obs = sb.build(m(rps=40.0, p95=750.0, inflight=4.0, cpu=0.8), units=2)   # 2 units = 1 core
    cap = 80.0
    assert obs.shape == (9,) and obs.dtype == np.float32
    assert obs[0] == pytest.approx(0.8)                      # 0.8 cores / 1 core
    assert obs[1] == pytest.approx(0.25 + 0.6 * 0.8 + 0.1 * (4 / 200))
    assert obs[2] == pytest.approx(4.0 / (cap * 5.0))
    assert obs[3] == pytest.approx(1.5)                      # 750 / 500
    assert obs[4] == pytest.approx(0.5)                      # 40 / 80
    assert obs[5] == pytest.approx(0.0)                      # first step: no trend
    assert obs[6] == pytest.approx(0.25) and obs[7] == 0 and obs[8] == 0
    obs2 = sb.build(m(rps=56.0), units=2)
    assert obs2[5] == pytest.approx((56.0 - 40.0) / cap)


def test_observation_clips_like_training():
    sb = StateBuilder(8, 0.5, 10.0)
    obs = sb.build(m(rps=1000.0, p95=10_000.0, inflight=1e6, cpu=9.0), units=1)
    assert obs[0] == 1.0 and obs[2] == 3.0 and obs[3] == 4.0 and obs.max() <= 5.0


def test_nan_metrics_are_rejected():
    with pytest.raises(ValueError):
        StateBuilder(8, 0.5, 10.0).build(m(p95=float("nan")), units=2)


@pytest.mark.parametrize("a,expected", [(-1.0, -2), (-0.3, -1), (0.0, 0), (0.2, 0), (0.3, 1), (1.0, 2)])
def test_ddpg_action_mapping(a, expected):
    assert delta_from_action(np.array([a], dtype=np.float32), continuous=True) == expected


@pytest.mark.parametrize("a,expected", [(0, -2), (2, 0), (4, 2)])
def test_discrete_action_mapping(a, expected):
    assert delta_from_action(np.int64(a), continuous=False) == expected


def test_guards_clamp_step_and_cooldown():
    g = Guards(min_units=1, max_units=8, cooldown_down_s=30)
    assert g.apply(4, 9, now=0.0)[0] == 6            # ±2 per step
    assert g.apply(8, 9, now=1.0)[0] == 8            # max
    assert g.apply(1, 0, now=2.0)[0] == 1            # min
    assert g.apply(6, 5, now=10.0) == (6, "scale-down held (cooldown 30s)")
    assert g.apply(6, 5, now=40.0)[0] == 5


def test_threshold_policy_scales_up_fast_and_down_after_window():
    p = ThresholdPolicy(cpus_per_unit=0.5, target=0.7, window=3)
    assert p.decide(m(cpu=1.0), units=2)[0] == 3      # 100 % of 1 core -> ceil(2 * 1/0.7)
    assert p.decide(m(cpu=0.7), units=3)[0] == 3      # 47 % wants fewer units, but the window still holds 3
    for _ in range(3):
        target = p.decide(m(cpu=0.2), units=3)[0]
    assert target == 1                                 # all of the last 3 wishes agree on scaling down


def test_static_policy():
    assert StaticPolicy(4).decide(m(), units=1)[0] == 4


def test_log_report_and_mu(tmp_path):
    db_path = str(tmp_path / "d.sqlite")
    log = DecisionLog(db_path)
    log.start_run("r1", "vm", "static", False, "", {})
    for i, (rps, p95) in enumerate([(5, 200), (9, 400), (12, 900), (8, None)]):
        log.record(run_id="r1", step=i, tier="vm", policy="static", shadow=0, rps=rps, p95_ms=p95,
                   units_before=1, proposed=1, units_after=1, applied=0, obs=[0.0] * 9)
    log.close()
    (s,) = report(db_path)
    assert s["steps"] == 4 and s["failed_reads"] == 1
    assert s["sla_violation_pct"] == pytest.approx(100 / 3, abs=0.01)
    assert s["avg_units"] == 1 and s["cost"] == pytest.approx(2.0)
    assert estimate_mu(db_path, "r1") == 9
