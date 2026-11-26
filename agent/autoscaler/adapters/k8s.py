"""Kubernetes tier: the classifier Deployment's replica count, changed with `kubectl scale`.
1 unit = 1 pod with a fixed CPU (request = limit). A new pod is "pending" until its readiness probe passes,
like the simulator's startup delay; the controller reports ready and pending pods separately.

The controller runs on the cluster node (k3s on the AWS testbed), so it can read each pod's cgroup directly
for CPU and memory, the same way the VM tier reads its container's cgroup."""
from __future__ import annotations

import glob
import json
import os
import subprocess
import time
from dataclasses import dataclass, field


@dataclass
class Pod:
    uid: str
    name: str
    ready: bool


@dataclass
class Snapshot:
    spec: int                  # replicas asked for (what is paid for)
    ready: int                 # replicas passing their readiness probe
    pods: list[Pod] = field(default_factory=list)

    @property
    def pending(self) -> int:
        return max(0, self.spec - self.ready)


class KubectlAdapter:
    def __init__(self, namespace: str, deployment: str, kubectl: str = "kubectl", selector: str | None = None):
        self.namespace, self.deployment, self.kubectl = namespace, deployment, kubectl
        self.selector = selector or f"app={deployment}"
        self.last: Snapshot | None = None

    def _run(self, *args: str) -> str:
        out = subprocess.run([self.kubectl, "-n", self.namespace, *args], capture_output=True, text=True,
                             timeout=15)
        if out.returncode != 0:
            raise RuntimeError(f"kubectl {' '.join(args)} failed: {out.stderr.strip()}")
        return out.stdout

    def refresh(self) -> Snapshot:
        """One kubectl call: the Deployment's replica counts and its live (not terminating) pods."""
        items = json.loads(self._run("get", "deployment,pod", "-l", self.selector, "-o", "json"))["items"]
        spec = ready = None
        pods = []
        for it in items:
            meta = it.get("metadata", {})
            if it.get("kind") == "Deployment" and meta.get("name") == self.deployment:
                spec = int(it.get("spec", {}).get("replicas", 1))
                ready = int(it.get("status", {}).get("readyReplicas") or 0)
            elif it.get("kind") == "Pod" and not meta.get("deletionTimestamp"):
                conds = {c.get("type"): c.get("status") for c in it.get("status", {}).get("conditions", [])}
                pods.append(Pod(meta.get("uid", ""), meta.get("name", ""), conds.get("Ready") == "True"))
        if spec is None:
            raise RuntimeError(f"deployment {self.deployment} (label {self.selector}) not found in {self.namespace}")
        # While scaling down, terminating pods can still count as ready for a moment.
        self.last = Snapshot(spec, min(ready, spec), pods)
        return self.last

    def current_units(self) -> int:
        return self.refresh().spec

    def pending(self) -> int:
        return (self.last or self.refresh()).pending

    def set_units(self, units: int) -> None:
        self._run("scale", f"deployment/{self.deployment}", f"--replicas={units}")


def pod_cgroup(uid: str, root: str = "/sys/fs/cgroup") -> str | None:
    """cgroup v2 directory of a pod (systemd or cgroupfs driver, any QoS class)."""
    u = uid.replace("-", "_")
    candidates = [
        f"{root}/kubepods.slice/kubepods-pod{u}.slice",
        f"{root}/kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod{u}.slice",
        f"{root}/kubepods.slice/kubepods-besteffort.slice/kubepods-besteffort-pod{u}.slice",
        f"{root}/kubepods/pod{uid}",
        f"{root}/kubepods/burstable/pod{uid}",
        f"{root}/kubepods/besteffort/pod{uid}",
    ]
    for c in candidates:
        if os.path.exists(f"{c}/cpu.stat"):
            return c
    for pattern in (f"{root}/kubepods*/**/*pod{u}*", f"{root}/kubepods*/**/*pod{uid}*"):
        hits = [h for h in glob.glob(pattern, recursive=True) if os.path.exists(f"{h}/cpu.stat")]
        if hits:
            return min(hits, key=len)
    return None


def _usage_s(path: str) -> float | None:
    try:
        with open(f"{path}/cpu.stat") as f:
            for line in f:
                key, value = line.split()
                if key == "usage_usec":
                    return int(value) / 1e6
    except OSError:
        return None
    return None


class PodCgroupUsage:
    """CPU (cores, averaged since the previous read) and memory of the adapter's *ready* pods, from cgroup v2.
    Uses the pod list of the adapter's latest refresh(). A pod that appeared since the previous read has no
    baseline yet and is left out for that one interval (it is normally still starting)."""

    def __init__(self, adapter: KubectlAdapter, root: str = "/sys/fs/cgroup", clock=time.monotonic):
        self.adapter, self.root, self.clock = adapter, root, clock
        self.paths: dict[str, str] = {}
        self._last_t: float | None = None
        self._last: dict[str, float] = {}
        if not os.path.isdir(root):
            raise RuntimeError(f"{root} not found (the controller must run on the cluster node)")

    def _path(self, uid: str) -> str | None:
        if uid not in self.paths:
            p = pod_cgroup(uid, self.root)
            if p is None:
                return None
            self.paths[uid] = p
        return self.paths[uid]

    def read(self) -> tuple[float, float]:
        snap = self.adapter.last or self.adapter.refresh()
        now = self.clock()
        usage, ready_uids = {}, set()
        for pod in snap.pods:
            path = self._path(pod.uid)
            if path is None:
                continue
            u = _usage_s(path)
            if u is not None:
                usage[pod.uid] = u
                if pod.ready:
                    ready_uids.add(pod.uid)
        cores = 0.0
        if self._last_t is not None:
            dt = max(now - self._last_t, 1e-3)
            cores = sum(max(0.0, usage[uid] - self._last[uid]) for uid in ready_uids if uid in self._last) / dt
        mem = 0.0
        for uid in ready_uids:
            try:
                with open(f"{self.paths[uid]}/memory.current") as f:
                    mem += float(f.read().strip() or 0)
            except OSError:
                pass
        self._last_t, self._last = now, usage
        self.paths = {uid: p for uid, p in self.paths.items() if uid in usage}
        return cores, mem
