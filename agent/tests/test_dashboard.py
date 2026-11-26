"""Dashboard tests with a fake Locust and a fake controller process (no Docker, Locust or PyTorch)."""
import base64
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "loadgen"))

pytest.importorskip("flask")
from dashboard.app import Demo, create_app  # noqa: E402
from shapes import users_at  # noqa: E402


class FakeLocust:
    def __init__(self):
        self.calls = []

    def set_users(self, users):
        self.calls.append(("users", users))

    def stop(self):
        self.calls.append(("stop",))

    def stats(self):
        return dict(ok=True, state="running", users=self.calls[-1][1] if self.calls and self.calls[-1][0] == "users" else 0,
                    rps=0.0, fail_ratio=0.0)


@pytest.fixture
def demo(tmp_path):
    fake_controller = tmp_path / "fake-python"
    fake_controller.write_text("#!/bin/sh\nsleep 30\n")
    fake_controller.chmod(0o755)
    d = Demo(FakeLocust(), str(tmp_path / "demo" / "decisions.sqlite"), mu=6.4, python=str(fake_controller))
    yield d
    d.stop()


def auth(user="examiner", password="pw"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()}


def test_login_required(demo):
    client = create_app(demo, "examiner", "pw").test_client()
    assert client.get("/api/config").status_code == 401
    assert client.get("/api/config", headers=auth(password="wrong")).status_code == 401
    cfg = client.get("/api/config", headers=auth()).get_json()
    assert cfg["mu_per_unit"] == 6.4 and "spike" in cfg["scenarios"] and "manual" in cfg["scenarios"]


def test_scenario_run_drives_locust_and_stops(demo):
    client = create_app(demo, "examiner", "pw").test_client()
    r = client.post("/api/start", json=dict(method="rl", scenario="spike", peak_users=40, duration_s=300),
                    headers=auth())
    assert r.status_code == 200 and r.get_json()["run_id"].startswith("demo-rl-spike-")
    assert client.post("/api/start", json=dict(method="rl"), headers=auth()).status_code == 400  # one at a time
    for _ in range(20):
        if demo.locust.calls:
            break
        time.sleep(0.1)
    assert demo.locust.calls[0] == ("users", round(40 * users_at(0, "spike", 300)))
    state = client.get("/api/state", headers=auth()).get_json()
    assert state["running"] and state["run"]["scenario"] == "spike"
    client.post("/api/stop", headers=auth())
    assert not demo.running and demo.locust.calls[-1] == ("stop",)
    assert demo.proc.poll() is not None                   # controller process was terminated


def test_manual_users_only_in_manual_runs(demo):
    client = create_app(demo, "examiner", "pw").test_client()
    assert client.post("/api/users", json={"users": 5}, headers=auth()).status_code == 400
    client.post("/api/start", json=dict(method="threshold", scenario="manual", peak_users=40), headers=auth())
    assert client.post("/api/users", json={"users": 25}, headers=auth()).get_json()["users"] == 25
    assert ("users", 25) in demo.locust.calls


def test_bad_method_rejected(demo):
    client = create_app(demo, "examiner", "pw").test_client()
    assert client.post("/api/start", json=dict(method="magic"), headers=auth()).status_code == 400
