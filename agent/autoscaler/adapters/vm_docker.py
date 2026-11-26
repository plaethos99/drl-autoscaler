"""VM tier: the classifier container's CPU quota (cgroup), changed live with `docker update --cpus`.
1 unit = 0.5 vCPU, so 1..8 units = 0.5..4 cores. The host needs at least 4 cores for the classifier
plus room for the other containers (see agent/README.md for the instance size)."""
from __future__ import annotations

import os
import subprocess
import time


class DockerCpuAdapter:
    def __init__(self, container: str, cpus_per_unit: float, docker: str = "docker"):
        self.container, self.cpus_per_unit, self.docker = container, cpus_per_unit, docker

    def _run(self, *args: str) -> str:
        out = subprocess.run([self.docker, *args], capture_output=True, text=True, timeout=15)
        if out.returncode != 0:
            raise RuntimeError(f"docker {' '.join(args)} failed: {out.stderr.strip()}")
        return out.stdout.strip()

    def current_units(self) -> int:
        nano = int(self._run("inspect", "-f", "{{.HostConfig.NanoCpus}}", self.container) or 0)
        if nano == 0:
            raise RuntimeError(f"{self.container} has no CPU limit; start it with the compose file (cpus: ...)")
        return max(1, round(nano / 1e9 / self.cpus_per_unit))

    def set_units(self, units: int) -> None:
        self._run("update", "--cpus", f"{units * self.cpus_per_unit:.3f}", self.container)


class CgroupUsage:
    """The container's CPU (cores, averaged since the previous read) and memory, read straight from
    cgroup v2. Used instead of cAdvisor, which on recent Docker/Ubuntu 24.04 may not label containers by
    name (Prometheus then has no CPU series for the classifier). Same host as the controller."""

    def __init__(self, container: str, docker: str = "docker", root: str = "/sys/fs/cgroup"):
        cid = subprocess.run([docker, "inspect", "-f", "{{.Id}}", container], capture_output=True,
                             text=True, timeout=15).stdout.strip()
        candidates = [f"{root}/system.slice/docker-{cid}.scope", f"{root}/docker/{cid}"]
        self.path = next((c for c in candidates if cid and os.path.exists(f"{c}/cpu.stat")), None)
        if self.path is None:
            raise RuntimeError(f"no cgroup v2 directory for {container} (tried {candidates})")
        self._last = (time.monotonic(), self._usage_s())

    def _usage_s(self) -> float:
        with open(f"{self.path}/cpu.stat") as f:
            for line in f:
                key, value = line.split()
                if key == "usage_usec":
                    return int(value) / 1e6
        raise RuntimeError(f"usage_usec missing in {self.path}/cpu.stat")

    def read(self) -> tuple[float, float]:
        now, used = time.monotonic(), self._usage_s()
        t0, u0 = self._last
        self._last = (now, used)
        cores = (used - u0) / max(now - t0, 1e-3)
        with open(f"{self.path}/memory.current") as f:
            mem = float(f.read().strip() or 0)
        return cores, mem
