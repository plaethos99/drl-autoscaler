"""Download a trained model from a GitHub Release and load it with Stable-Baselines3."""
from __future__ import annotations

import json
import os

import numpy as np
import requests

ACTION_DELTAS = [-2, -1, 0, 1, 2]


def release_url(repo: str, tag: str, name: str) -> str:
    return f"https://github.com/{repo}/releases/download/{tag}/{name}"


def fetch(repo: str, tag: str, name: str, cache_dir: str) -> str:
    """Download once into cache_dir/<tag>/<name>; later runs reuse the file."""
    path = os.path.join(cache_dir, tag, name)
    if os.path.exists(path):
        return path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    r = requests.get(release_url(repo, tag, name), timeout=60)
    r.raise_for_status()
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(r.content)
    os.replace(tmp, path)
    return path


def delta_from_action(action, continuous: bool) -> int:
    """Same mapping as the simulator / metadata["ddpg_action_mapping"]."""
    if continuous:
        return int(np.clip(np.rint(float(np.asarray(action).ravel()[0]) * 2), -2, 2))
    return ACTION_DELTAS[int(np.asarray(action).ravel()[0])]


class TrainedModel:
    def __init__(self, tier: str, repo: str, tag: str, cache_dir: str):
        meta_path = fetch(repo, tag, "metadata.json", cache_dir)
        with open(meta_path, encoding="utf-8") as f:
            self.metadata = json.load(f)
        best = self.metadata["best"][tier]
        self.algo, self.seed = best["algo"], best["seed"]
        self.continuous = self.algo == "DDPG"
        self.version = self.metadata.get("version", tag)
        model_path = fetch(repo, tag, f"{tier}_best.zip", cache_dir)

        import stable_baselines3 as sb3   # heavy import (torch): only when the RL policy is used
        cls = {"PPO": sb3.PPO, "DQN": sb3.DQN, "DDPG": sb3.DDPG}[self.algo]
        # custom_objects: skip unpickling training-only schedules (they can differ across SB3/Python versions)
        self.model = cls.load(model_path, device="cpu",
                              custom_objects={"learning_rate": 0.0, "lr_schedule": lambda _: 0.0,
                                              "exploration_schedule": lambda _: 0.0, "clip_range": lambda _: 0.0})

    def decide(self, obs: np.ndarray) -> tuple[int, float]:
        """Return (delta in units, raw action value) for one observation."""
        action, _ = self.model.predict(obs, deterministic=True)
        raw = float(np.asarray(action).ravel()[0])
        return delta_from_action(action, self.continuous), raw
