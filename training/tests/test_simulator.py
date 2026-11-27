"""Simulator checks that run in GitHub Actions without PyTorch or Stable-Baselines3.

Only the simulator part of drl_autoscaler_training.py (sections 1-3) is executed, not the training.
"""
import math
import os
from pathlib import Path

import gymnasium as gym
import numpy as np
import pytest
from gymnasium import spaces

SRC = (Path(__file__).resolve().parents[1] / "drl_autoscaler_training.py").read_text(encoding="utf-8")
START, END = "# ## 1. Simulation constants", "# ## 4. Baselines"
NS = dict(np=np, math=math, os=os, gym=gym, spaces=spaces)
exec(compile(SRC[SRC.index(START):SRC.index(END)], "simulator", "exec"), NS)
Env, PROFILES = NS["CloudScalingEnv"], NS["PROFILES"]


@pytest.mark.parametrize("mode", ["k8s", "vm", "function"])
def test_episode_runs_and_observations_are_finite(mode):
    env = Env(mode=mode, continuous=True)
    obs, _ = env.reset(seed=1)
    assert obs.shape == (9,)
    for _ in range(NS["EPISODE_STEPS"]):
        obs, reward, term, trunc, info = env.step(env.action_space.sample())
        assert np.isfinite(obs).all() and math.isfinite(reward)
    assert trunc


def test_azure_trace_patterns_are_loaded_and_split_by_time():
    import csv
    import gzip
    windows = NS["TRACE_WINDOWS"]
    assert len(windows["train"]) > 100 and len(windows["test"]) > 50
    assert "azure_trace" in NS["TRAIN_PATTERNS"] and "azure_trace_test" in NS["HELDOUT_PATTERNS"]
    assert "azure_trace_test" not in NS["TRAIN_PATTERNS"]
    rows = list(csv.DictReader(gzip.open(Path(__file__).resolve().parents[1] / "data" / NS["TRACE_FILE"], "rt")))
    last_train = max(int(r["start_s"]) for r in rows if r["split"] == "train")
    first_test = min(int(r["start_s"]) for r in rows if r["split"] == "test")
    assert last_train < first_test                            # held-out windows come strictly later
    rng = np.random.default_rng(0)
    for pattern in ("azure_trace", "azure_trace_test"):
        rps = NS["gen_workload"](pattern, NS["EPISODE_STEPS"], 20.0, rng)
        assert rps.shape == (NS["EPISODE_STEPS"],) and rps.min() >= 0.5 and rps.max() <= 22.0
    env = Env(mode="vm", patterns=["azure_trace"])
    env.reset(seed=3)
    assert env.pattern == "azure_trace"


def test_observation_has_no_lookahead():
    """A spike that starts at step k must not be visible in the observation taken before step k runs."""
    env = Env(mode="vm", randomize=False, fixed_pattern="steady")
    env.reset(seed=0)
    env.rps[:] = 2.0
    env.rps[10:] = 25.0
    for _ in range(10):
        obs, *_ = env.step(2)                 # discrete action 2 = no change
    cap = PROFILES["vm"]["max_units"] * env.mu
    assert obs[4] < 4.0 / cap                 # still the quiet rate after 10 quiet steps
    obs, *_ = env.step(2)                     # the first spike step happens now
    assert obs[4] > 4.0 / cap                 # and only now shows up (partly, 15 s window)


def test_vm_latency_matches_aws_calibration():
    """Medians measured on AWS (docs/aws-results-2026-11-26, training/calibrate_vm.py): about 205 ms at
    4 units and 4.5 req/s, about 89 ms at 8 units and 4.5 req/s, about 356 ms at 8 units and 25 req/s."""
    env = Env(mode="vm", randomize=False, fixed_pattern="steady")
    env.reset(seed=0)
    env.mu, env.overhead = 3.9, 15.0          # fitted centre values

    def p95_at(units, rps, steps=8):
        env.units, env.queue, env.pending = units, 0.0, []
        env.rps[:] = rps
        env.t = 0
        vals = []
        for _ in range(steps):
            _, _, _, _, info = env.step_target(units)
            vals.append(info["p95"])
        return float(np.median(vals))

    assert 120 < p95_at(4, 4.5) < 320
    assert 50 < p95_at(8, 4.5) < 160
    assert 250 < p95_at(8, 25.0) < 800        # near 80 % utilisation, so Poisson noise matters
    assert p95_at(8, 4.5) < p95_at(4, 4.5)     # more CPU makes each request faster


def test_k8s_latency_matches_aws_calibration():
    """AWS k3s testbed (2026-11-27): 3.9 req/s per 0.1-vCPU pod within the 500 ms SLA (MU_PER_UNIT), about
    120-150 ms at light load, CPU for ~7.2 req/s per pod; beyond that the backlog grows."""
    p = PROFILES["k8s"]
    assert p["max_units"] == 8 and p["latency"] == "random"
    env = Env(mode="k8s", randomize=False, fixed_pattern="steady")
    env.reset(seed=0)
    env.mu, env.overhead = 3.9, 60.0          # calibrated centre values

    def run(units, rps, steps=8):
        env.units, env.queue, env.pending = units, 0.0, []
        env.rps[:] = rps
        env.t = 0
        infos = [env.step_target(units)[4] for _ in range(steps)]
        return float(np.median([i["p95"] for i in infos])), infos

    assert 100 < run(4, 2.0)[0] < 220                       # light load
    assert 330 < run(4, 4 * 3.9)[0] < 750                   # at MU_PER_UNIT per pod: around the SLA
    assert run(8, 4 * 3.9)[0] < run(4, 4 * 3.9)[0]          # more pods -> less queueing
    assert env.cpu_mu == pytest.approx(3.9 * 1.86)
    _, infos = run(4, 4 * 7.25 * 1.3, steps=12)              # 30 % above CPU capacity: backlog builds up
    assert infos[-1]["p95"] > infos[1]["p95"] and infos[-1]["p95"] > 1000
    assert env.last["cpu_demand"] <= 1.0                     # request = limit: HPA never sees > 100 %


def test_k8s_new_pods_start_after_a_delay():
    env = Env(mode="k8s", randomize=False, fixed_pattern="steady")
    env.reset(seed=0)
    env.units, env.pending = 2, []
    _, _, _, _, info = env.step_target(4)
    assert info["units"] == 2 and info["pending"] == 2       # startup_steps lower bound is 2 steps (10 s)
    env.step_target(4)
    _, _, _, _, info = env.step_target(4)
    assert info["units"] == 4 and info["pending"] == 0
