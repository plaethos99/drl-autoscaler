"""Kubernetes-tier parts without a cluster: kubectl output parsing, pod cgroups, pending-aware policies,
the decision-log migration and the testbed manifests (they run in GitHub Actions)."""
import json
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autoscaler.adapters.k8s import KubectlAdapter, PodCgroupUsage, Snapshot, pod_cgroup  # noqa: E402
from autoscaler.config import TIERS  # noqa: E402
from autoscaler.decisionlog import DecisionLog  # noqa: E402
from autoscaler.policies import ObservePolicy, RLPolicy, ThresholdPolicy  # noqa: E402
from autoscaler.prom import Metrics  # noqa: E402
from autoscaler.report import report  # noqa: E402
from autoscaler.state import StateBuilder  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
TESTBED = REPO / "apps" / "image-service" / "k8s" / "testbed"


def m(rps=20.0, p95=250.0, inflight=2.0, cpu=0.2, mem=1e8):
    return Metrics(rps=rps, p95_ms=p95, inflight=inflight, cpu_cores=cpu, mem_bytes=mem)


def pod(uid, ready=True, deleting=False):
    meta = {"uid": uid, "name": f"classifier-{uid}"}
    if deleting:
        meta["deletionTimestamp"] = "2026-11-27T00:00:00Z"
    return {"kind": "Pod", "metadata": meta,
            "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}}


KUBECTL_JSON = {"items": [
    {"kind": "Deployment", "metadata": {"name": "classifier"}, "spec": {"replicas": 4},
     "status": {"readyReplicas": 2, "replicas": 4}},
    pod("a"), pod("b"), pod("c", ready=False), pod("d", ready=False), pod("e", deleting=True),
]}


class FakeKubectl(KubectlAdapter):
    def __init__(self, payload):
        super().__init__("imgsvc", "classifier")
        self.payload, self.calls = payload, []

    def _run(self, *args):
        self.calls.append(args)
        return json.dumps(self.payload) if args[0] == "get" else ""


def test_kubectl_snapshot_counts_ready_and_pending_pods():
    a = FakeKubectl(KUBECTL_JSON)
    assert a.current_units() == 4 and a.pending() == 2
    assert [p.uid for p in a.last.pods] == ["a", "b", "c", "d"]        # terminating pod left out
    assert [p.ready for p in a.last.pods] == [True, True, False, False]
    assert a.calls[0] == ("get", "deployment,pod", "-l", "app=classifier", "-o", "json")
    a.set_units(6)
    assert a.calls[-1] == ("scale", "deployment/classifier", "--replicas=6")


def test_ready_never_exceeds_spec_while_scaling_down():
    payload = {"items": [{"kind": "Deployment", "metadata": {"name": "classifier"}, "spec": {"replicas": 2},
                          "status": {"readyReplicas": 4}}]}
    snap = FakeKubectl(payload).refresh()
    assert snap.ready == 2 and snap.pending == 0


def test_missing_deployment_is_an_error():
    with pytest.raises(RuntimeError):
        FakeKubectl({"items": []}).refresh()


@pytest.mark.parametrize("rel", ["kubepods.slice/kubepods-pod{u}.slice",
                                 "kubepods.slice/kubepods-burstable.slice/kubepods-burstable-pod{u}.slice",
                                 "kubepods/pod{uid}", "kubepods/besteffort/pod{uid}"])
def test_pod_cgroup_drivers_and_qos_classes(tmp_path, rel):
    uid = "1234-abcd"
    d = tmp_path / rel.format(u=uid.replace("-", "_"), uid=uid)
    d.mkdir(parents=True)
    (d / "cpu.stat").write_text("usage_usec 1\n")
    assert pod_cgroup(uid, str(tmp_path)) == str(d)
    assert pod_cgroup("other", str(tmp_path)) is None


def _cg(root, uid, usec, mem=100):
    d = root / "kubepods.slice" / f"kubepods-pod{uid}.slice"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cpu.stat").write_text(f"usage_usec {usec}\nuser_usec 0\n")
    (d / "memory.current").write_text(f"{mem}\n")


def test_pod_usage_sums_ready_pods_with_a_baseline(tmp_path):
    clock = iter([100.0, 105.0])
    a = FakeKubectl(KUBECTL_JSON)
    a.refresh()
    for uid in "abc":
        _cg(tmp_path, uid, 1_000_000)
    usage = PodCgroupUsage(a, root=str(tmp_path), clock=lambda: next(clock))
    assert usage.read()[0] == 0.0                        # first read only sets the baseline
    _cg(tmp_path, "a", 1_500_000)                        # +0.5 s CPU in 5 s
    _cg(tmp_path, "b", 1_250_000)                        # +0.25 s
    _cg(tmp_path, "c", 3_000_000)                        # not ready (starting): ignored
    _cg(tmp_path, "d", 9_000_000)                        # new and not ready: ignored
    cores, mem = usage.read()
    assert cores == pytest.approx(0.75 / 5.0) and mem == 200


def test_threshold_policy_uses_ready_pods_and_keeps_starting_ones():
    p = ThresholdPolicy(cpus_per_unit=0.1, target=0.7, window=3)
    # 4 asked for, 2 ready and fully busy (0.2 cores): wants ceil(2 / 0.7) = 3, but never cancels the 2 starting
    assert p.decide(m(cpu=0.2), units=4, pending=2)[0] == 4
    p2 = ThresholdPolicy(cpus_per_unit=0.1, target=0.7, window=3)
    assert p2.decide(m(cpu=0.4), units=4, pending=0)[0] == 6      # 100 % of 4 pods -> ceil(4 / 0.7)


def test_observe_policy_never_changes_the_allocation():
    assert ObservePolicy().decide(m(), units=5, pending=1)[0] == 5


def test_rl_policy_sees_ready_and_pending_units_apart():
    class Model:
        def decide(self, obs):
            self.obs = obs
            return 1, 0.4
    model = Model()
    sb = StateBuilder(max_units=8, cpus_per_unit=0.1, mu_per_unit=7.0)
    target, raw, obs = RLPolicy(model, sb).decide(m(cpu=0.2), units=4, pending=1)
    assert target == 5 and raw == 0.4
    assert obs[0] == pytest.approx(min(1.0, 0.2 / 0.3))       # CPU over the 3 ready pods
    assert obs[6] == pytest.approx(3 / 8) and obs[7] == pytest.approx(1 / 8)


def test_decision_log_adds_pending_column_to_old_databases(tmp_path):
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE decisions (run_id TEXT, ts REAL, step INTEGER, tier TEXT, policy TEXT, shadow INTEGER,"
               " rps REAL, p95_ms REAL, inflight REAL, cpu_cores REAL, mem_bytes REAL, units_before INTEGER,"
               " proposed INTEGER, units_after INTEGER, applied INTEGER, raw_action REAL, obs TEXT, note TEXT)")
    db.commit()
    db.close()
    log = DecisionLog(str(path))
    log.record(run_id="r", step=0, units_before=2, units_after=2, applied=0, pending=1)
    log.close()
    assert sqlite3.connect(path).execute("SELECT pending FROM decisions").fetchone() == (1,)


def test_report_counts_changes_made_by_the_cluster_hpa(tmp_path):
    db_path = str(tmp_path / "d.sqlite")
    log = DecisionLog(db_path)
    log.start_run("h", "k8s", "hpa", False, "kubernetes-hpa", {})
    for i, units in enumerate([2, 2, 4, 4, 3]):         # HPA changed it twice; the controller never did
        log.record(run_id="h", step=i, tier="k8s", policy="hpa", shadow=0, rps=10, p95_ms=200,
                   units_before=units, proposed=units, units_after=units, applied=0, pending=0)
    log.close()
    (s,) = report(db_path)
    assert s["scaling_actions"] == 2 and s["avg_units"] == 3.0 and s["cost"] == 15.0   # unit_cost 1.0


def _docs(name):
    yaml = pytest.importorskip("yaml")
    return [d for d in yaml.safe_load_all((TESTBED / name).read_text(encoding="utf-8")) if d]


def test_testbed_manifests_match_the_controller():
    k8s = TIERS["k8s"]
    docs = _docs("app.yaml")
    cls = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "classifier")
    res = cls["spec"]["template"]["spec"]["containers"][0]["resources"]
    assert res["requests"]["cpu"] == res["limits"]["cpu"] == f"{round(k8s['cpus_per_unit'] * 1000)}m"
    assert cls["metadata"]["labels"]["app"] == "classifier"      # the adapter's label selector
    gw = next(d for d in docs if d["kind"] == "Deployment" and d["metadata"]["name"] == "gateway")
    env = {e["name"]: e["value"] for e in gw["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["UPSTREAM_KEEPALIVE"] == "0"
    svc = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == "gateway")
    assert svc["spec"]["ports"][0]["port"] == 8080
    (hpa,) = _docs("hpa.yaml")
    assert hpa["spec"]["minReplicas"] == k8s["min_units"] and hpa["spec"]["maxReplicas"] == k8s["max_units"]
    prom = next(d for d in _docs("prometheus.yaml") if d["kind"] == "Service")
    assert prom["spec"]["ports"][0]["nodePort"] == 30090


def test_app_url_for_the_load_generator(monkeypatch):
    from dashboard.app import Locust, app_url
    monkeypatch.delenv("APP_URL", raising=False)
    assert app_url("http://127.0.0.1:8089") == "http://127.0.0.1:8080"
    monkeypatch.setenv("APP_URL", "http://10.0.0.5:8080")
    assert app_url("http://172.31.4.183:8089") == "http://10.0.0.5:8080"
    assert Locust("http://x:8089/", "http://10.0.0.5:8080").target == "http://10.0.0.5:8080"
    assert Snapshot(3, 1).pending == 2 and Snapshot(1, 1).pending == 0


def test_model_release_is_chosen_per_tier(monkeypatch):
    from autoscaler.config import Settings
    monkeypatch.delenv("MODEL_RELEASE", raising=False)
    assert Settings(tier="vm").release == "model-v0.2" and Settings(tier="k8s").release == "model-v0.3"
    monkeypatch.setenv("MODEL_RELEASE", "model-v9")
    assert Settings(tier="k8s").release == "model-v9"
