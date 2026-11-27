"""The 5-second control loop: read metrics -> policy -> safety guards -> apply -> log."""
from __future__ import annotations

import logging
import signal
import subprocess
import time
import uuid
from dataclasses import asdict

from .adapters.vm_docker import CgroupUsage, DockerCpuAdapter
from .config import DT_S, SLA_P95_MS, Settings
from .decisionlog import DecisionLog
from .guards import Guards
from .policies import ObservePolicy, RLPolicy, StaticPolicy, ThresholdPolicy
from .prom import Prometheus, PromError
from .state import StateBuilder

log = logging.getLogger("autoscaler")


def make_adapter(s: Settings):
    if s.tier == "vm":
        return DockerCpuAdapter(s.container, s.profile["cpus_per_unit"])
    if s.tier == "k8s":
        from .adapters.k8s import KubectlAdapter
        return KubectlAdapter(s.k8s_namespace, s.k8s_deployment)
    raise ValueError(f"tier {s.tier!r} has no adapter yet")


def make_policy(s: Settings):
    p = s.profile
    if s.policy == "rl":
        from .modelstore import TrainedModel
        model = TrainedModel(s.tier, s.model_repo, s.release, s.model_dir)
        meta = model.metadata
        if meta.get("decision_interval_s", DT_S) != DT_S or meta.get("sla_p95_ms", SLA_P95_MS) != SLA_P95_MS:
            raise ValueError("model was trained with a different decision interval or SLA")
        trained = meta.get("profiles", {}).get(s.tier, {})
        if trained and trained.get("max_units") != p["max_units"]:
            raise ValueError(f"model trained with max_units={trained.get('max_units')}, tier has {p['max_units']}")
        state = StateBuilder(p["max_units"], p["cpus_per_unit"], s.mu_per_unit, meta.get("observation"))
        log.info("model %s: %s seed %s (%s)", s.release, model.algo, model.seed, model.version)
        return RLPolicy(model, state), f"{s.release}:{s.tier}_best({model.algo})"
    if s.policy == "threshold":
        return ThresholdPolicy(p["cpus_per_unit"], s.hpa_target, s.hpa_tolerance, s.hpa_window), ""
    if s.policy == "static":
        return StaticPolicy(s.static_units or max(p["min_units"], p["max_units"] // 2)), ""
    if s.policy == "hpa":
        if s.tier != "k8s":
            raise ValueError("--policy hpa observes the Kubernetes HPA; it only applies to --tier k8s")
        return ObservePolicy(), "kubernetes-hpa"
    raise ValueError(f"unknown policy {s.policy!r}")


def run(s: Settings) -> str:
    p = s.profile
    s.run_id = s.run_id or f"{s.tier}-{s.policy}{'-shadow' if s.shadow else ''}-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
    adapter = make_adapter(s)
    usage = None
    if s.tier == "vm":       # CPU/memory from the container's cgroup when possible, else from cAdvisor
        try:
            usage = CgroupUsage(s.container)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            log.warning("cgroup CPU reader unavailable (%s); using cAdvisor via Prometheus", exc)
    elif s.tier == "k8s":    # pods' cgroups on this node (the controller runs on the k3s node)
        from .adapters.k8s import PodCgroupUsage
        usage = PodCgroupUsage(adapter)
    prom = Prometheus(s.prom_url, s.window, s.container, usage=usage)
    policy, model_name = make_policy(s)
    guards = Guards(p["min_units"], p["max_units"], s.cooldown_down_s)
    db = DecisionLog(s.db_path)
    db.start_run(s.run_id, s.tier, s.policy, s.shadow, model_name, asdict(s))

    if s.start_units:
        adapter.set_units(max(p["min_units"], min(p["max_units"], s.start_units)))
    log.info("run %s: tier=%s policy=%s shadow=%s start units=%d", s.run_id, s.tier, s.policy, s.shadow,
             adapter.current_units())

    stop = {"flag": False}
    def _stop(*_):
        stop["flag"] = True
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    t0 = time.monotonic()
    next_tick, step, failed = t0, 0, 0
    while not stop["flag"] and (not s.duration_s or time.monotonic() - t0 < s.duration_s):
        units = adapter.current_units()
        pending = adapter.pending() if hasattr(adapter, "pending") else 0
        row = dict(run_id=s.run_id, step=step, tier=s.tier, policy=s.policy, shadow=int(s.shadow),
                   units_before=units, pending=pending)
        try:
            m = prom.read()
            if m.p95_ms != m.p95_ms:      # NaN: traffic without latency data
                raise PromError("p95 missing while traffic is flowing")
            failed = 0
            proposed, raw, obs = policy.decide(m, units, pending)
            target, note = guards.apply(units, proposed)
            applied = 0
            if target != units and not s.shadow:
                adapter.set_units(target)
                applied = 1
            row.update(rps=m.rps, p95_ms=m.p95_ms, inflight=m.inflight, cpu_cores=m.cpu_cores,
                       mem_bytes=m.mem_bytes, proposed=proposed, units_after=target if applied else units,
                       applied=applied, raw_action=raw, obs=obs or None, note=note)
            log.info("step %3d  rps %6.1f  p95 %7.1f ms  cpu %.2f cores  units %d%s -> %d%s%s", step, m.rps,
                     m.p95_ms, m.cpu_cores, units, f" ({pending} pending)" if pending else "", target,
                     " (shadow)" if s.shadow else "",
                     f"  [{note}]" if note else "")
        except (PromError, ValueError) as exc:
            failed += 1
            note = f"metrics unavailable ({exc}); holding at {units} units"
            if failed >= s.max_failed_reads:
                note += f" [{failed} in a row]"
            row.update(units_after=units, applied=0, note=note)
            log.warning("step %3d  %s", step, note)
        db.record(**row)
        step += 1
        next_tick += DT_S
        time.sleep(max(0.0, next_tick - time.monotonic()))
    db.close()
    log.info("run %s finished after %d steps", s.run_id, step)
    return s.run_id
