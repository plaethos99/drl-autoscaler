"""Simulator checks that run in GitHub Actions without PyTorch or Stable-Baselines3.

Only the simulator part of drl_autoscaler_training.py (sections 1-3) is executed, not the training.
"""
import math
from pathlib import Path

import gymnasium as gym
import numpy as np
import pytest
from gymnasium import spaces

SRC = (Path(__file__).resolve().parents[1] / "drl_autoscaler_training.py").read_text(encoding="utf-8")
START, END = "# ## 1. Simulation constants", "# ## 4. Baselines"
NS = dict(np=np, math=math, gym=gym, spaces=spaces)
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
