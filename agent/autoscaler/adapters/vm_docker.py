"""VM tier: the classifier container's CPU quota (cgroup), changed live with `docker update --cpus`.
1 unit = 0.5 vCPU, so 1..8 units = 0.5..4 cores. The host needs at least 4 cores for the classifier
plus room for the other containers (see agent/README.md for the instance size)."""
from __future__ import annotations

import subprocess


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
        self._run("update", "--cpus", f"{units * self.cpus_per_unit:.2f}", self.container)
