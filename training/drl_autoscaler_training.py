# %% [markdown]
# # DRL Autoscaler — Simulator + Training (Objectives 1 & 2)
#
# This notebook:
# 1. Builds a **simulated cloud environment** (Gymnasium) with three modes:
#    `k8s` (replicas), `vm` (cgroup CPU quota), `function` (provisioned concurrency).
# 2. Generates **synthetic workloads** (steady, periodic, ramp, spike, 5-minute paper scenario)
#    plus **held-out** patterns the agent never trains on.
# 3. Trains **PPO, DQN, DDPG** with several seeds and the same network architecture.
# 4. Benchmarks them in simulation against **HPA-like threshold** and **static (round-robin)** baselines.
# 5. Saves models, metadata, CSV results and plots to the output folder (download it, then
#    publish the best model as a GitHub Release).
#
# Kaggle settings: **Internet = On** (for pip install). Accelerator: GPU is optional. DQN/DDPG use it
# when present (`DEVICE=auto`), PPO always runs on CPU. Training runs in parallel, one process per CPU core.
#
# Calibration status: the `vm` profile is fitted to real AWS runs (2026-11-26, m7i-flex.large, see
# training/calibrate_vm.py and docs/aws-results-2026-11-26). The `k8s` and `function` numbers (req/s per
# replica, startup time, cold start, ...) are still placeholders until they are measured the same way.

# %%
import importlib, subprocess, sys

def _ensure(pip_name, module):
    try:
        importlib.import_module(module)
    except ImportError:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pip_name])

_ensure("gymnasium>=0.29", "gymnasium")
_ensure("stable-baselines3>=2.3", "stable_baselines3")

# %%
import os, json, math, time, glob, shutil
import numpy as np
import pandas as pd
import gymnasium as gym
from gymnasium import spaces

# ----------------------------- CONFIG ---------------------------------------------------------
QUICK = os.environ.get("QUICK") == "1"   # set True for a tiny smoke run (few minutes)
MODES = ["k8s", "vm", "function"]   # train one model per tier
ALGOS = ["PPO", "DQN", "DDPG"]      # comparative study (Objective 1)
SEEDS = [0, 1, 2, 3, 4]             # 5 seeds (v0.2 paper run)
TIMESTEPS = 150_000                 # per (mode, algo, seed)
EVAL_EPISODES = 10                  # per pattern per method
NET_ARCH = [128, 128]               # same MLP for every algorithm (fair comparison)
DEVICE = os.environ.get("DEVICE", "auto")             # DQN/DDPG device: auto (GPU if present), cpu, cuda
N_WORKERS = int(os.environ.get("N_WORKERS", os.cpu_count() or 1))   # parallel training processes

if QUICK:
    MODES, SEEDS, TIMESTEPS, EVAL_EPISODES = ["k8s"], [0], 6_000, 3

OUT_DIR = "/kaggle/working/outputs" if os.path.isdir("/kaggle/working") else os.path.abspath("outputs")
os.makedirs(OUT_DIR, exist_ok=True)
print("Output dir:", OUT_DIR)

# %% [markdown]
# ## 1. Simulation constants and cloud profiles
#
# * One step = **5 s** (agent decision interval); one episode = **60 steps = 5 minutes**
#   (same length as the Locust load tests).
# * SLA: **p95 latency ≤ 500 ms**.
# * Each profile gives **ranges**; every episode samples new values (domain randomisation)
#   so one model generalises across AWS / Azure / GCP.

# %%
DT = 5.0                 # seconds per step
EPISODE_STEPS = 60       # 5 minutes
OBS_WINDOW_STEPS = 3     # the controller reads rate() and p95 over a 15 s Prometheus window = 3 steps
SLA_P95_MS = 500.0
CLIENT_TIMEOUT_MS = 10_000.0   # requests slower than this time out (caps the latency metric)
ACTION_DELTAS = np.array([-2, -1, 0, 1, 2])   # discrete action -> change in allocation units

PROFILES = {
    # unit = 1 pod replica
    "k8s": dict(min_units=1, max_units=10, mu=(15.0, 22.0), overhead_ms=(20.0, 40.0),
                startup_steps=(1, 4), unit_cost=1.0, cold_ms=(0.0, 0.0), max_queue_s=10.0),
    # unit = a slice of one container's cgroup CPU quota (AWS testbed: 0.05 vCPU, 1..8 units); changes apply
    # instantly. Calibrated on AWS (training/calibrate_vm.py, 584 steps, 10 runs): ONE server whose speed
    # grows with the quota ("shared"), mu 3.7-4.1 req/s per unit across runs (3.5 from CPU time per
    # request), overhead ~15 ms, p95 = overhead + 2.0 x service + 3.0 x queueing delay.
    "vm": dict(min_units=1, max_units=8, mu=(3.5, 4.1), overhead_ms=(10.0, 25.0),
               startup_steps=(0, 0), unit_cost=0.5, cold_ms=(0.0, 0.0), max_queue_s=10.0,
               latency="shared", k_service=2.0, k_queue=3.0),
    # unit = 1 provisioned (warm) function instance; overflow runs on-demand with a cold start
    "function": dict(min_units=0, max_units=20, mu=(6.0, 9.0), overhead_ms=(30.0, 60.0),
                     startup_steps=(1, 2), unit_cost=0.6, cold_ms=(300.0, 1500.0), max_queue_s=10.0,
                     ondemand_cost=0.25, keepalive_steps=(6, 12), concurrency_limit=40),
}

# %% [markdown]
# ## 2. Workloads: generated patterns + a real traffic dataset
#
# Generated training patterns: `steady`, `periodic`, `ramp`, `spike`, `paper5min`.
# Generated held-out patterns (never seen in training): `double_spike`, `sawtooth`, `noisy_trace`.
#
# Real traffic: **Azure Functions Invocation Trace 2021** (Azure Public Dataset, CC-BY 4.0; Zhang et al.,
# "Faster and Cheaper Serverless Computing on Harvested Resources", SOSP 2021): 1.98 M invocations of 119
# apps over 14 days, cut into 5-minute windows of 60 × 5 s (`training/build_trace_windows.py`).
# `azure_trace` trains on windows from days 1–10; `azure_trace_test` is held out (days 11–14).
# Each window is scaled so its busiest 5 s reach 70–100 % of the generated patterns' peak.

# %%
TRACE_FILE = "azure2021_windows.csv.gz"
TRACE_URL = os.environ.get(   # pinned to the commit that built the windows, for reproducibility
    "TRACE_URL", "https://raw.githubusercontent.com/plaethos99/drl-autoscaler/ce976e2/training/data/" + TRACE_FILE)


def load_trace_windows():
    """{'train': array (n, 60), 'test': array (m, 60)} of peak-normalised request counts, or {} if missing."""
    import csv, gzip, io, os, urllib.request
    raw = None
    for path in (os.path.join("data", TRACE_FILE), os.path.join("training", "data", TRACE_FILE)):
        if os.path.exists(path):
            with open(path, "rb") as f:
                raw = f.read()
            break
    if raw is None:
        try:
            raw = urllib.request.urlopen(TRACE_URL, timeout=60).read()
        except Exception as exc:                    # no internet: train on generated patterns only
            print("Azure trace windows unavailable, using generated patterns only:", exc)
            return {}
    rows = list(csv.DictReader(io.StringIO(gzip.decompress(raw).decode())))
    cols = [f"w{j:02d}" for j in range(60)]
    out = {s: np.array([[float(r[c]) for c in cols] for r in rows if r["split"] == s]) for s in ("train", "test")}
    print(f"Azure trace windows: {len(out['train'])} train, {len(out['test'])} held-out")
    return out


TRACE_WINDOWS = load_trace_windows()
TRAIN_PATTERNS = ["steady", "periodic", "ramp", "spike", "paper5min"] + (["azure_trace"] if TRACE_WINDOWS else [])
HELDOUT_PATTERNS = ["double_spike", "sawtooth", "noisy_trace"] + (["azure_trace_test"] if TRACE_WINDOWS else [])


def gen_workload(pattern, steps, peak, rng):
    """Return requests/second for each step. `peak` ~ max load the tier can serve."""
    t = np.arange(steps)
    base = peak * rng.uniform(0.10, 0.25)
    if pattern in ("azure_trace", "azure_trace_test"):
        windows = TRACE_WINDOWS["train" if pattern == "azure_trace" else "test"]
        shape = windows[rng.integers(len(windows))]
        if len(shape) != steps:
            shape = np.interp(np.linspace(0, len(shape) - 1, steps), np.arange(len(shape)), shape)
        # real traffic already has its own noise: no extra multiplicative noise
        return np.clip(peak * rng.uniform(0.7, 1.0) * shape, 0.5, peak * 1.1)
    if pattern == "steady":
        rps = np.full(steps, peak * rng.uniform(0.2, 0.7))
    elif pattern == "periodic":
        cycles = rng.uniform(1.5, 4.0)
        amp = peak * rng.uniform(0.25, 0.45)
        rps = base + amp * (1 + np.sin(2 * np.pi * cycles * t / steps + rng.uniform(0, 2 * np.pi)))
    elif pattern == "ramp":
        lo, hi = peak * rng.uniform(0.05, 0.2), peak * rng.uniform(0.6, 0.95)
        rps = np.linspace(lo, hi, steps) if rng.random() < 0.6 else np.linspace(hi, lo, steps)
    elif pattern == "spike":
        rps = np.full(steps, base)
        start, length = rng.integers(8, steps - 15), rng.integers(4, 10)
        rps[start:start + length] = peak * rng.uniform(0.7, 1.0)
    elif pattern == "paper5min":  # same shape as the Locust 5-minute test
        rps = np.zeros(steps)
        s = steps / 300.0          # steps per second of the 5-minute script
        for i in range(steps):
            sec = i / s
            if sec < 60:
                rps[i] = base
            elif sec < 120:
                rps[i] = base + (peak * 0.5 - base) * (sec - 60) / 60
            elif sec < 180:
                rps[i] = peak * 0.95
            else:
                rps[i] = peak * (0.35 + 0.2 * np.sin(2 * np.pi * (sec - 180) / 40))
    elif pattern == "double_spike":
        rps = np.full(steps, base)
        for start in (rng.integers(8, 22), rng.integers(35, 48)):
            rps[start:start + rng.integers(3, 7)] = peak * rng.uniform(0.75, 1.0)
    elif pattern == "sawtooth":
        period = rng.integers(10, 20)
        rps = base + (peak * 0.8 - base) * ((t % period) / period)
    elif pattern == "noisy_trace":  # random walk with bursts (trace-like)
        x = np.empty(steps)
        x[0] = peak * 0.3
        for i in range(1, steps):
            x[i] = x[i - 1] + rng.normal(0, peak * 0.04)
            if rng.random() < 0.05:
                x[i] += peak * rng.uniform(0.2, 0.5)
            x[i] = 0.85 * x[i] + 0.15 * peak * 0.3
        rps = x
    else:
        raise ValueError(pattern)
    noise = rng.normal(1.0, 0.05, steps)
    return np.clip(rps * noise, 0.5, peak * 1.1)

# %% [markdown]
# ## 3. The environment (State, Action, Reward — Objective 1)
#
# **State** (9 values, normalised): CPU utilisation, memory utilisation, queue length,
# p95 latency / SLA, request rate, request-rate trend, allocated units, pending units,
# cold-start fraction.
#
# **Action**: change allocation by {-2, -1, 0, +1, +2} units (DDPG: continuous value in [-1, 1]
# rounded to the same deltas).
#
# **Reward**: `r = -(W_SLA · violation + W_COST · cost + W_DROP · dropped + W_ACT · |Δ|)`
# where violation = 1[p95 > SLA] + min(2, (p95 − SLA)/SLA).

# %%
W_SLA, W_COST, W_DROP, W_ACT = 1.0, 0.5, 2.0, 0.02


class CloudScalingEnv(gym.Env):
    metadata = {"render_modes": []}

    def __init__(self, mode="k8s", patterns=None, continuous=False, randomize=True, fixed_pattern=None):
        super().__init__()
        self.mode, self.p = mode, PROFILES[mode]
        self.patterns = patterns or TRAIN_PATTERNS
        self.fixed_pattern, self.randomize, self.continuous = fixed_pattern, randomize, continuous
        self.observation_space = spaces.Box(low=-1.0, high=5.0, shape=(9,), dtype=np.float32)
        self.action_space = (spaces.Box(-1.0, 1.0, shape=(1,), dtype=np.float32) if continuous
                             else spaces.Discrete(len(ACTION_DELTAS)))

    # ---------- helpers ----------
    def _sample(self, rng, rangepair):
        lo, hi = rangepair
        return float(lo if not self.randomize else rng.uniform(lo, hi))

    def _delta_from_action(self, action):
        if self.continuous:
            return int(np.clip(np.rint(float(np.asarray(action).ravel()[0]) * 2), -2, 2))
        return int(ACTION_DELTAS[int(action)])

    @property
    def total_target(self):
        return self.units + len(self.pending)

    # ---------- gym API ----------
    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        rng, p = self.np_random, self.p
        self.mu = self._sample(rng, p["mu"])
        self.overhead = self._sample(rng, p["overhead_ms"])
        lo, hi = p["startup_steps"]
        self.startup = int(rng.integers(lo, hi + 1)) if self.randomize else lo
        self.cold_ms = self._sample(rng, p["cold_ms"])
        self.keepalive = int(rng.integers(*p["keepalive_steps"])) if self.mode == "function" else 0
        self.pattern = self.fixed_pattern or str(rng.choice(self.patterns))
        cap = p["max_units"] * self.mu
        # peak load is servable within SLA at max allocation (~80 % utilisation)
        self.rps = gen_workload(self.pattern, EPISODE_STEPS, cap * 0.8, rng)
        self.t = 0
        need = math.ceil(self.rps[0] / (self.mu * 0.6))
        self.units = int(np.clip(need + rng.integers(0, 2), p["min_units"], p["max_units"]))
        self.pending = []           # countdowns for units still starting
        self.queue = 0.0
        self.ondemand = 0.0         # function mode: warm on-demand instances
        self.ondemand_age = 0
        self.last = dict(cpu=0.3, mem=0.3, p95=self.overhead, cold_frac=0.0, cpu_demand=0.3)
        # What the agent sees is what the real controller sees: request rate and p95 of the *past*
        # OBS_WINDOW_STEPS steps (Prometheus window), never the rate of the step about to happen.
        self.win = [(float(self.rps[0]), float(self.overhead), 1.0)] * OBS_WINDOW_STEPS
        self.prev_obs_rps = float(self.rps[0])
        return self._obs(), {}

    def step(self, action):
        return self._advance(self.total_target + self._delta_from_action(action))

    def step_target(self, target):
        """Used by baselines: jump straight to a target allocation."""
        return self._advance(int(target))

    def _advance(self, target):
        p = self.p
        target = int(np.clip(target, p["min_units"], p["max_units"]))
        delta = target - self.total_target
        # units started earlier that are now ready
        self.pending = [c - 1 for c in self.pending]
        self.units += sum(1 for c in self.pending if c <= 0)
        self.pending = [c for c in self.pending if c > 0]
        if delta > 0:
            if self.startup == 0:          # e.g. cgroup quota change: instant
                self.units += delta
            else:                          # ready after `startup` steps (5 s each)
                self.pending += [self.startup] * delta
        elif delta < 0:
            remove = -delta
            while remove and self.pending:
                self.pending.pop(); remove -= 1
            self.units = max(p["min_units"], self.units - remove)

        # ---- traffic for this step ----
        rps = float(self.rps[self.t])
        arrivals = float(self.np_random.poisson(rps * DT))
        demand = arrivals + self.queue
        cold_frac, ondemand_cost = 0.0, 0.0

        if self.mode == "function":
            warm_units = self.units + self.ondemand
            warm_cap = warm_units * self.mu * DT
            limit_cap = p["concurrency_limit"] * self.mu * DT
            overflow = max(0.0, demand - warm_cap)
            cold_served = min(overflow, max(0.0, limit_cap - warm_cap))
            cold_frac = cold_served / max(demand, 1.0)
            served = min(demand, warm_cap + cold_served)
            needed = cold_served / (self.mu * DT)
            if needed > 0:
                self.ondemand += needed; self.ondemand_age = 0
            else:
                self.ondemand_age += 1
                if self.ondemand_age > self.keepalive:
                    self.ondemand = max(0.0, self.ondemand * 0.5 - 0.5)
            ondemand_cost = p["ondemand_cost"] * (served - min(served, self.units * self.mu * DT)) / (self.mu * DT)
            eff_units = max(warm_units + needed, 1e-6)          # instances doing work (for CPU %)
            lat_units = float(p["concurrency_limit"])            # functions scale out on demand: queueing
                                                                 # only near the concurrency limit
        else:
            cap = self.units * self.mu * DT
            served = min(demand, cap)
            eff_units = lat_units = max(self.units, 1e-6)

        self.queue = demand - served
        max_q = p["max_queue_s"] * rps
        dropped = max(0.0, self.queue - max_q)
        self.queue = min(self.queue, max_q)

        # ---- latency model: M/M/c queue (Sakasegawa approximation for the waiting time) ----
        # p95 ≈ overhead + k_s × service time + k_q × mean queueing delay + backlog drain time
        # "parallel" (k8s pods, function instances): c = units servers of rate mu each.
        # "shared" (vm: one container whose CPU quota grows): one server of rate units × mu.
        if p.get("latency") == "shared":
            c, rate = 1.0, max(lat_units, 1e-6) * self.mu
        else:
            c, rate = max(lat_units, 1.0), self.mu
        rho = min((arrivals / DT) / (c * rate), 0.98)
        service_ms = 1000.0 / rate
        wq_ms = service_ms * rho ** (math.sqrt(2.0 * (c + 1.0)) - 1.0) / (c * (1.0 - rho))
        p95 = self.overhead + p.get("k_service", 2.3) * service_ms + p.get("k_queue", 3.0) * wq_ms
        p95 += 1000.0 * self.queue / max(lat_units * self.mu, 1e-6)          # backlog wait
        if cold_frac >= 0.05:
            p95 = max(p95, self.cold_ms + service_ms + self.overhead)
        p95 = min(p95, CLIENT_TIMEOUT_MS)
        cpu = float(min(1.0, served / max(eff_units * self.mu * DT, 1e-6)))
        # utilisation vs CPU *request* (pods may burst to 3x request) — what HPA actually sees
        # (function mode: = provisioned-concurrency utilisation, the metric AWS target tracking uses)
        cpu_demand = float(min(3.0, (arrivals / DT) / (max(self.units, 1) * self.mu)))
        mem = float(min(1.0, 0.25 + 0.6 * cpu + 0.1 * min(self.queue / max(rps * DT, 1.0), 1.0)))

        # ---- reward ----
        violated = p95 > SLA_P95_MS
        violation = float(violated) + min(2.0, max(0.0, (p95 - SLA_P95_MS) / SLA_P95_MS))
        cost = (self.units + len(self.pending)) * p["unit_cost"] + ondemand_cost
        cost_norm = cost / (p["max_units"] * p["unit_cost"])
        drop_frac = dropped / max(arrivals, 1.0)
        reward = -(W_SLA * violation + W_COST * cost_norm + W_DROP * drop_frac + W_ACT * abs(delta))

        self.last = dict(cpu=cpu, mem=mem, p95=p95, cold_frac=cold_frac, cpu_demand=cpu_demand)
        info = dict(t=self.t, rps=rps, units=self.units, pending=len(self.pending), p95=p95,
                    violated=violated, cost=cost, dropped=dropped, arrivals=arrivals,
                    cpu=cpu, delta=delta, pattern=self.pattern)
        self.win = (self.win + [(arrivals / DT, p95, max(arrivals, 1.0))])[-OBS_WINDOW_STEPS:]
        self.t += 1
        terminated = False
        truncated = self.t >= EPISODE_STEPS
        return self._obs(), float(reward), terminated, truncated, info

    def _obs(self):
        p = self.p
        cap = p["max_units"] * self.mu
        # 15 s window: mean request rate, request-weighted p95 (what a histogram over the window yields)
        rps = float(np.mean([w[0] for w in self.win]))
        weights = np.array([w[2] for w in self.win])
        p95_win = float(np.dot([w[1] for w in self.win], weights) / weights.sum())
        trend = (rps - self.prev_obs_rps) / cap
        self.prev_obs_rps = rps
        obs = np.array([
            self.last["cpu"],
            self.last["mem"],
            min(self.queue / (cap * DT), 3.0),
            min(p95_win / SLA_P95_MS, 4.0),
            rps / cap,
            np.clip(trend, -1.0, 1.0),
            self.units / p["max_units"],
            len(self.pending) / p["max_units"],
            self.last["cold_frac"],
        ], dtype=np.float32)
        return np.clip(obs, -1.0, 5.0)

# %% [markdown]
# ## 4. Baselines (same simulator)
#
# * **HPA-like**: `desired = ceil(units × cpu / 0.70)`, 10 % tolerance, 30 s scale-down
#   stabilisation (6 steps) — the shortened settings used in the 5-minute tests.
# * **Static (Round Robin)**: fixed allocation (half of max), load spread evenly.

# %%
class HPABaseline:
    name = "HPA"
    def __init__(self, target=0.70, window=6):
        self.target, self.window = target, window
    def reset(self):
        self.history = []
    def act(self, env):
        cpu = env.last["cpu_demand"]          # utilisation relative to CPU request (can exceed 100 %)
        cur = max(env.units, 1)
        desired = cur if abs(cpu / self.target - 1.0) <= 0.10 else math.ceil(cur * cpu / self.target)
        desired = max(desired, env.total_target if cpu > self.target else 0)  # don't cancel pods starting
        self.history = (self.history + [desired])[-self.window:]
        return max(self.history)          # scale-down only after the window agrees


class StaticBaseline:
    name = "RoundRobin-Static"
    def reset(self):
        pass
    def act(self, env):
        return max(env.p["min_units"], env.p["max_units"] // 2)

# %% [markdown]
# ## 5. Quick sanity checks of the simulator

# %%
def run_episode(env, policy=None, model=None, seed=0):
    obs, _ = env.reset(seed=seed)
    if policy: policy.reset()
    rows, total = [], 0.0
    while True:
        if model is not None:
            a, _ = model.predict(obs, deterministic=True)
            obs, r, term, trunc, info = env.step(a)
        else:
            obs, r, term, trunc, info = env.step_target(policy.act(env))
        total += r
        rows.append({**info, "reward": r})
        if term or trunc:
            break
    return total, pd.DataFrame(rows)


def summarize(df):
    return dict(
        reward=float(df["reward"].sum()),
        sla_violation_pct=100.0 * float(df["violated"].mean()),
        mean_p95_ms=float(df["p95"].mean()),
        p99_of_p95_ms=float(df["p95"].quantile(0.99)),
        avg_units=float(df["units"].mean()),
        cost=float(df["cost"].sum()),
        drop_pct=100.0 * float(df["dropped"].sum() / max(df["arrivals"].sum(), 1.0)),
        scaling_actions=int((df["delta"] != 0).sum()),
    )


for mode in MODES:
    for pol in (HPABaseline(), StaticBaseline()):
        env = CloudScalingEnv(mode, fixed_pattern="paper5min")
        _, df = run_episode(env, policy=pol, seed=123)
        s = summarize(df)
        print(f"{mode:9s} {pol.name:18s} SLA viol {s['sla_violation_pct']:5.1f}%  "
              f"avg units {s['avg_units']:5.2f}  mean p95 {s['mean_p95_ms']:7.1f} ms")

from gymnasium.utils.env_checker import check_env
check_env(CloudScalingEnv("k8s"), skip_render_check=True)
check_env(CloudScalingEnv("k8s", continuous=True), skip_render_check=True)
print("Environment passes gymnasium checks.")

# %% [markdown]
# ## 6. Training: PPO vs DQN vs DDPG (same network, several seeds)

# %%
import torch
from stable_baselines3 import PPO, DQN, DDPG
from stable_baselines3.common.env_util import make_vec_env
from stable_baselines3.common.monitor import Monitor
from stable_baselines3.common.noise import NormalActionNoise

import multiprocessing as mp
from concurrent.futures import ProcessPoolExecutor, as_completed


def make_model(algo, mode, seed, log_dir):
    pk = dict(net_arch=NET_ARCH)
    if algo == "PPO":   # small MLP on-policy: CPU is faster than GPU (SB3 recommendation)
        venv = make_vec_env(lambda: CloudScalingEnv(mode), n_envs=8, seed=seed, monitor_dir=log_dir)
        return PPO("MlpPolicy", venv, n_steps=512, batch_size=256, learning_rate=3e-4, gamma=0.99,
                   gae_lambda=0.95, ent_coef=0.01, clip_range=0.2, policy_kwargs=pk, seed=seed, verbose=0,
                   device="cpu")
    if algo == "DQN":
        env = Monitor(CloudScalingEnv(mode), os.path.join(log_dir, "0"))
        return DQN("MlpPolicy", env, learning_rate=5e-4, buffer_size=100_000, learning_starts=2_000,
                   batch_size=128, gamma=0.99, train_freq=4, target_update_interval=1_000,
                   exploration_fraction=0.3, exploration_final_eps=0.02, policy_kwargs=pk, seed=seed, verbose=0,
                   device=DEVICE)
    if algo == "DDPG":
        # train_freq=4 / gradient_steps=1: same update ratio as DQN. SB3's default (update every step) made
        # DDPG ~6x slower than DQN without a fair-comparison reason.
        env = Monitor(CloudScalingEnv(mode, continuous=True), os.path.join(log_dir, "0"))
        noise = NormalActionNoise(mean=np.zeros(1), sigma=0.3 * np.ones(1))
        return DDPG("MlpPolicy", env, learning_rate=1e-3, buffer_size=100_000, learning_starts=2_000,
                    batch_size=128, gamma=0.99, action_noise=noise, train_freq=4, gradient_steps=1,
                    policy_kwargs=dict(net_arch=dict(pi=NET_ARCH, qf=NET_ARCH)), seed=seed, verbose=0,
                    device=DEVICE)
    raise ValueError(algo)


def train_one(job):
    mode, algo, seed = job
    torch.set_num_threads(1)   # one core per process; parallelism comes from the process pool
    run = f"{mode}_{algo}_s{seed}"
    log_dir = os.path.join(OUT_DIR, "logs", run)
    os.makedirs(log_dir, exist_ok=True)
    t0 = time.time()
    model = make_model(algo, mode, seed, log_dir)
    model.learn(total_timesteps=TIMESTEPS, progress_bar=False)
    path = os.path.join(OUT_DIR, "models", f"{run}.zip")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    model.save(path)
    mins = (time.time() - t0) / 60
    return dict(mode=mode, algo=algo, seed=seed, device=str(model.device), minutes=round(mins, 2), path=path)


# slowest algorithms first so the pool stays busy at the end
COST_ORDER = {"DDPG": 0, "DQN": 1, "PPO": 2}
jobs = sorted([(m, a, s) for m in MODES for a in ALGOS for s in SEEDS], key=lambda j: COST_ORDER[j[1]])
print(f"{len(jobs)} training runs on {N_WORKERS} worker(s), DQN/DDPG device={DEVICE}")
t_all = time.time()
train_log = []
if N_WORKERS > 1:
    # fork keeps everything defined above (works in notebooks); CUDA is never initialised in the parent
    with ProcessPoolExecutor(max_workers=N_WORKERS, mp_context=mp.get_context("fork")) as pool:
        futures = [pool.submit(train_one, j) for j in jobs]
        for fut in as_completed(futures):
            r = fut.result()
            train_log.append(r)
            print(f"[{len(train_log):2d}/{len(jobs)}] trained {r['mode']}_{r['algo']}_s{r['seed']:<3} "
                  f"on {r['device']:4s} in {r['minutes']:5.1f} min", flush=True)
else:
    for j in jobs:
        r = train_one(j)
        train_log.append(r)
        print(f"[{len(train_log):2d}/{len(jobs)}] trained {r['mode']}_{r['algo']}_s{r['seed']:<3} "
              f"on {r['device']:4s} in {r['minutes']:5.1f} min", flush=True)
print(f"all training done in {(time.time() - t_all) / 60:.1f} min")

pd.DataFrame(train_log).sort_values(["mode", "algo", "seed"]).to_csv(os.path.join(OUT_DIR, "train_log.csv"), index=False)

# %% [markdown]
# ## 7. Evaluation in simulation (seen + held-out patterns, same seeds for every method)

# %%
ALGO_CLS = {"PPO": PPO, "DQN": DQN, "DDPG": DDPG}
EVAL_PATTERNS = TRAIN_PATTERNS + HELDOUT_PATTERNS
rows = []
for mode in MODES:
    methods = [("HPA", HPABaseline(), None, False), ("RoundRobin-Static", StaticBaseline(), None, False)]
    for algo in ALGOS:
        for seed in SEEDS:
            m = ALGO_CLS[algo].load(os.path.join(OUT_DIR, "models", f"{mode}_{algo}_s{seed}.zip"), device="cpu")
            methods.append((f"{algo}", None, m, algo == "DDPG", seed))
    for entry in methods:
        name, pol, model, cont = entry[:4]
        train_seed = entry[4] if len(entry) > 4 else -1
        for pattern in EVAL_PATTERNS:
            for ep in range(EVAL_EPISODES):
                env = CloudScalingEnv(mode, continuous=cont, fixed_pattern=pattern)
                _, df = run_episode(env, policy=pol, model=model, seed=10_000 + ep)
                rows.append(dict(mode=mode, method=name, train_seed=train_seed, pattern=pattern,
                                 heldout=pattern in HELDOUT_PATTERNS, episode=ep, **summarize(df)))

results = pd.DataFrame(rows)
results.to_csv(os.path.join(OUT_DIR, "sim_eval_results.csv"), index=False)
summary = (results.groupby(["mode", "method"])[["reward", "sla_violation_pct", "mean_p95_ms", "avg_units",
                                                "cost", "drop_pct", "scaling_actions"]]
           .mean().round(2))
summary.to_csv(os.path.join(OUT_DIR, "sim_eval_summary.csv"))
summary

# %% [markdown]
# ## 8. Pick the best algorithm per mode and export it for the controller

# %%
best = {}
for mode in MODES:
    rl = results[(results["mode"] == mode) & results["method"].isin(ALGOS)]
    by_algo = rl.groupby("method")["reward"].mean().sort_values(ascending=False)
    algo = by_algo.index[0]
    by_seed = rl[rl["method"] == algo].groupby("train_seed")["reward"].mean().sort_values(ascending=False)
    seed = int(by_seed.index[0])
    src = os.path.join(OUT_DIR, "models", f"{mode}_{algo}_s{seed}.zip")
    dst_dir = os.path.join(OUT_DIR, "release")
    os.makedirs(dst_dir, exist_ok=True)
    shutil.copy(src, os.path.join(dst_dir, f"{mode}_best.zip"))
    best[mode] = dict(algo=algo, seed=seed, mean_eval_reward=float(by_seed.iloc[0]))
    print(f"{mode}: best = {algo} (seed {seed})  algo ranking: {by_algo.round(2).to_dict()}")

metadata = dict(
    version="v0.2-vm-calibrated-aws-azure-trace",
    obs_window_steps=OBS_WINDOW_STEPS,
    train_patterns=TRAIN_PATTERNS, heldout_patterns=HELDOUT_PATTERNS,
    trace_dataset=dict(name="Azure Functions Invocation Trace 2021", license="CC-BY 4.0", url=TRACE_URL,
                       windows={k: len(v) for k, v in TRACE_WINDOWS.items()},
                       citation="Zhang et al., Faster and Cheaper Serverless Computing on Harvested Resources, "
                                "SOSP 2021") if TRACE_WINDOWS else None,
    created=time.strftime("%Y-%m-%d %H:%M:%S"),
    decision_interval_s=DT, episode_steps=EPISODE_STEPS, sla_p95_ms=SLA_P95_MS,
    action_deltas=ACTION_DELTAS.tolist(),
    ddpg_action_mapping="delta = clip(round(a*2), -2, 2)",
    observation=["cpu_util", "mem_util", "queue_norm", "p95_over_sla", "rps_over_capacity",
                 "rps_trend", "units_over_max", "pending_over_max", "cold_start_frac"],
    reward_weights=dict(W_SLA=W_SLA, W_COST=W_COST, W_DROP=W_DROP, W_ACT=W_ACT),
    profiles=PROFILES, net_arch=NET_ARCH, timesteps=TIMESTEPS, seeds=SEEDS, best=best,
)
with open(os.path.join(OUT_DIR, "release", "metadata.json"), "w") as f:
    json.dump(metadata, f, indent=2)

# %% [markdown]
# ## 9. Plots for the paper

# %%
import matplotlib.pyplot as plt
from stable_baselines3.common.results_plotter import load_results, ts2xy

FIG = os.path.join(OUT_DIR, "figures")
os.makedirs(FIG, exist_ok=True)
COLORS = {"PPO": "#2a6fdb", "DQN": "#e08a1e", "DDPG": "#7a4cc2", "HPA": "#d64545", "RoundRobin-Static": "#6b7280"}

# 9a. learning curves (mean ± std over seeds)
for mode in MODES:
    fig, ax = plt.subplots(figsize=(7, 4))
    for algo in ALGOS:
        curves = []
        for seed in SEEDS:
            df = load_results(os.path.join(OUT_DIR, "logs", f"{mode}_{algo}_s{seed}"))
            x, y = ts2xy(df, "timesteps")
            if len(x) < 10:
                continue
            grid = np.linspace(0, TIMESTEPS, 100)
            ys = pd.Series(y).rolling(20, min_periods=1).mean().to_numpy()
            curves.append(np.interp(grid, x, ys))
        if curves:
            c = np.array(curves)
            ax.plot(grid, c.mean(0), label=algo, color=COLORS[algo])
            ax.fill_between(grid, c.mean(0) - c.std(0), c.mean(0) + c.std(0), color=COLORS[algo], alpha=0.15)
    ax.set_title(f"Learning curves — {mode}"); ax.set_xlabel("timesteps"); ax.set_ylabel("episode reward")
    ax.grid(alpha=0.3); ax.legend()
    fig.tight_layout(); fig.savefig(os.path.join(FIG, f"learning_{mode}.png"), dpi=150); plt.close(fig)

# 9b. SLA violation and cost per method (seen vs held-out)
for mode in MODES:
    sub = results[results["mode"] == mode]
    fig, axes = plt.subplots(1, 2, figsize=(11, 4))
    for ax, metric, label in ((axes[0], "sla_violation_pct", "SLA violation (%)"), (axes[1], "avg_units", "Avg allocated units (cost)")):
        g = sub.groupby(["method", "heldout"])[metric].mean().unstack()
        g = g.reindex([m for m in COLORS if m in g.index])
        g.columns = ["seen patterns" if not c else "held-out patterns" for c in g.columns]
        g.plot(kind="bar", ax=ax, rot=0, color=["#94a3b8", "#334155"])
        ax.set_title(label); ax.grid(axis="y", alpha=0.3); ax.set_xlabel("")
    fig.suptitle(f"Simulation benchmark — {mode}")
    fig.tight_layout(); fig.savefig(os.path.join(FIG, f"benchmark_{mode}.png"), dpi=150); plt.close(fig)

# 9c. timeline of the 5-minute paper scenario: best RL vs HPA vs static
for mode in MODES:
    algo, seed = best[mode]["algo"], best[mode]["seed"]
    rl_model = ALGO_CLS[algo].load(os.path.join(OUT_DIR, "release", f"{mode}_best.zip"), device="cpu")
    runs = {
        f"RL ({algo})": run_episode(CloudScalingEnv(mode, continuous=algo == "DDPG", fixed_pattern="paper5min"), model=rl_model, seed=777)[1],
        "HPA": run_episode(CloudScalingEnv(mode, fixed_pattern="paper5min"), policy=HPABaseline(), seed=777)[1],
        "RoundRobin-Static": run_episode(CloudScalingEnv(mode, fixed_pattern="paper5min"), policy=StaticBaseline(), seed=777)[1],
    }
    fig, axes = plt.subplots(3, 1, figsize=(9, 8), sharex=True)
    tsec = np.arange(EPISODE_STEPS) * DT
    axes[0].plot(tsec, runs["HPA"]["rps"], color="#111827"); axes[0].set_ylabel("requests/s")
    for name, df in runs.items():
        col = COLORS.get(name, COLORS[algo])
        axes[1].step(tsec, df["units"], where="post", label=name, color=col)
        axes[2].plot(tsec, df["p95"], label=name, color=col)
    axes[2].axhline(SLA_P95_MS, color="#d64545", ls="--", lw=1, label="SLA 500 ms")
    axes[1].set_ylabel("allocated units"); axes[2].set_ylabel("p95 latency (ms)"); axes[2].set_xlabel("time (s)")
    axes[2].set_yscale("log"); axes[1].legend(); axes[2].legend(fontsize=8)
    for ax in axes: ax.grid(alpha=0.3)
    fig.suptitle(f"5-minute scenario — {mode}")
    fig.tight_layout(); fig.savefig(os.path.join(FIG, f"timeline_{mode}.png"), dpi=150); plt.close(fig)

print("Figures:", sorted(os.listdir(FIG)))
try:  # show the figures inline when running as a notebook
    from IPython.display import Image, display
    if "ipykernel" in sys.modules:
        for fn in sorted(os.listdir(FIG)):
            display(Image(os.path.join(FIG, fn)))
except ImportError:
    pass

# %% [markdown]
# ## 10. Statistical test: best RL vs HPA (Mann-Whitney U on SLA violation %)

# %%
from scipy.stats import mannwhitneyu

stats_rows = []
for mode in MODES:
    algo = best[mode]["algo"]
    for pattern in EVAL_PATTERNS:
        a = results[(results["mode"] == mode) & (results["method"] == algo) & (results["pattern"] == pattern)]["sla_violation_pct"]
        b = results[(results["mode"] == mode) & (results["method"] == "HPA") & (results["pattern"] == pattern)]["sla_violation_pct"]
        try:
            u, pval = mannwhitneyu(a, b, alternative="two-sided")
        except ValueError:          # all values identical
            u, pval = np.nan, 1.0
        stats_rows.append(dict(mode=mode, pattern=pattern, rl=algo, rl_mean=a.mean(), hpa_mean=b.mean(), U=u, p_value=pval))
stats = pd.DataFrame(stats_rows).round(4)
stats.to_csv(os.path.join(OUT_DIR, "stats_rl_vs_hpa.csv"), index=False)
stats

# %% [markdown]
# ## 11. Package outputs
# Download `outputs.zip` from the Kaggle **Output** panel. Upload `release/*_best.zip` and
# `release/metadata.json` to a GitHub Release (e.g. tag `model-v0.1`).

# %%
archive = shutil.make_archive(os.path.join(os.path.dirname(OUT_DIR), "outputs"), "zip", OUT_DIR)
print("Saved", archive)
