"""Controller settings. Defaults match the simulator (training/drl_autoscaler_training.py) and the VM tier
in apps/image-service/docker-compose.yml. Every field can be overridden with an env var or a CLI flag."""
from __future__ import annotations

import os
from dataclasses import dataclass, field

# Must match the simulator: one decision every 5 s, SLA = p95 <= 500 ms.
DT_S = 5.0
SLA_P95_MS = 500.0

# Per-tier constants from the simulator's PROFILES (the model was trained with these).
TIERS = {
    # unit = 0.5 vCPU of the classifier container's cgroup quota; 1..8 units = 0.5..4 cores.
    # CPUS_PER_UNIT shrinks the unit on a small box (ec2-setup.sh sets 0.125 on a 2-vCPU instance, so
    # 8 units = 1 core). The model only sees normalised values, so it is unaffected once MU_PER_UNIT
    # is calibrated at the same unit size.
    "vm": dict(min_units=1, max_units=8, cpus_per_unit=float(os.getenv("CPUS_PER_UNIT", "0.5")),
               unit_cost=0.5),
    # unit = 1 classifier pod with a fixed CPU (request = limit, POD_CPUS; 0.1 vCPU on the AWS k3s testbed,
    # apps/image-service/k8s/testbed). New pods take time to start: the controller sees them as "pending".
    "k8s": dict(min_units=1, max_units=8, cpus_per_unit=float(os.getenv("POD_CPUS", "0.1")), unit_cost=1.0),
}


# Released model per tier (GitHub Release tag). MODEL_RELEASE overrides it for every tier.
DEFAULT_RELEASE = {"vm": "model-v0.2", "k8s": "model-v0.3"}


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


@dataclass
class Settings:
    tier: str = "vm"
    policy: str = "rl"                 # rl | threshold | static | hpa (k8s: the cluster's own HPA scales)
    shadow: bool = False               # rl: compute + log decisions, but never apply them
    duration_s: float = 0.0            # 0 = run until stopped
    run_id: str = ""

    # model (GitHub Release asset + metadata.json)
    model_release: str = field(default_factory=lambda: _env("MODEL_RELEASE", ""))   # "" = DEFAULT_RELEASE[tier]
    model_repo: str = field(default_factory=lambda: _env("MODEL_REPO", "plaethos99/drl-autoscaler"))
    model_dir: str = field(default_factory=lambda: _env("MODEL_DIR", os.path.expanduser("~/.cache/drl-autoscaler")))

    # Prometheus + what to read
    prom_url: str = field(default_factory=lambda: _env("PROM_URL", "http://127.0.0.1:9090"))
    window: str = field(default_factory=lambda: _env("PROM_WINDOW", "15s"))   # >= 3 scrapes at 5 s
    container: str = field(default_factory=lambda: _env("TARGET_CONTAINER", "imgsvc-classifier"))
    k8s_namespace: str = field(default_factory=lambda: _env("K8S_NAMESPACE", "imgsvc"))
    k8s_deployment: str = field(default_factory=lambda: _env("K8S_DEPLOYMENT", "classifier"))

    # Requests/s one unit serves within the SLA; set the measured value (scripts/run_test.sh calibrate).
    # AWS 2-vCPU testbed (0.05-vCPU units): 3.5. The simulator's VM profile (v0.2+) samples 3.5..4.1.
    mu_per_unit: float = field(default_factory=lambda: float(_env("MU_PER_UNIT", "9.5")))

    # safety guards
    cooldown_down_s: float = field(default_factory=lambda: float(_env("COOLDOWN_DOWN_S", "0")))
    max_failed_reads: int = 3          # after this many bad Prometheus reads in a row, hold + log
    start_units: int = 0               # 0 = keep the current allocation at start

    # threshold (HPA-like) baseline
    hpa_target: float = 0.70
    hpa_tolerance: float = 0.10
    hpa_window: int = 6                # scale down only after 6 steps (30 s) agree
    static_units: int = 0              # 0 = half of max (as in the simulator)

    db_path: str = field(default_factory=lambda: _env("DECISION_DB", "decisions.sqlite"))

    @property
    def profile(self) -> dict:
        return TIERS[self.tier]

    @property
    def release(self) -> str:
        return self.model_release or DEFAULT_RELEASE[self.tier]
