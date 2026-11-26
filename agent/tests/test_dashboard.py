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
    r = client.get("/api/config")
    assert r.status_code == 401 and "WWW-Authenticate" not in r.headers      # no browser pop-up
    assert client.get("/api/config", headers=auth(password="wrong")).status_code == 401
    cfg = client.get("/api/config", headers=auth()).get_json()               # scripts: basic auth
    assert cfg["mu_per_unit"] == 6.4 and "spike" in cfg["scenarios"] and "manual" in cfg["scenarios"]


def test_login_page_session_and_logout(demo, monkeypatch):
    monkeypatch.setattr("dashboard.app.time.sleep", lambda s: None)
    client = create_app(demo, "examiner", "pw", secret_key=b"test").test_client()
    r = client.get("/")
    assert r.status_code == 302 and r.headers["Location"].endswith("/login")
    assert client.get("/login").status_code == 200
    r = client.post("/login", data={"username": "examiner", "password": "wrong"})
    assert r.status_code == 303 and "error=1" in r.headers["Location"]
    assert client.get("/api/config").status_code == 401
    r = client.post("/login", data={"username": "examiner", "password": "pw"})
    assert r.status_code == 303 and r.headers["Location"].endswith("/")
    assert client.get("/").status_code == 200                                # session cookie now
    assert client.get("/api/config").status_code == 200
    assert client.get("/login").status_code == 302                           # already signed in
    client.post("/logout")
    assert client.get("/api/config").status_code == 401


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


def test_export_zip_contains_results_and_demo(demo, tmp_path):
    import io
    import sqlite3
    import zipfile

    from dashboard.app import export_zip
    home = tmp_path / "home"
    (home / "results").mkdir(parents=True)
    with sqlite3.connect(home / "results" / "decisions.sqlite") as db:
        db.execute("CREATE TABLE t (x)")
        db.execute("INSERT INTO t VALUES (1)")
    (home / "results" / "summary.csv").write_text("run_id\n")
    (home / "notes").mkdir()                               # not a results folder: left out
    data = export_zip(home, Path(demo.db_path))
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert "results/decisions.sqlite" in names and "results/summary.csv" in names
    assert "demo/decisions.sqlite" in names and not any(n.startswith("notes") for n in names)

    client = create_app(demo, "examiner", "pw").test_client()
    assert client.get("/api/export").status_code == 401
    r = client.get("/api/export", headers=auth())
    assert r.status_code == 200 and r.mimetype == "application/zip"


def test_bad_method_rejected(demo):
    client = create_app(demo, "examiner", "pw").test_client()
    assert client.post("/api/start", json=dict(method="magic"), headers=auth()).status_code == 400
