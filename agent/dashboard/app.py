"""Demo dashboard: pick a method and a traffic scenario, press Start, watch traffic, latency and the
allocation live. Runs on the app box next to the controller.

* Load: drives Locust's web API on the load-generator box (LOADGEN_URL, Locust started with
  SCENARIO=manual so the dashboard sets the user count every 2 s from loadgen/shapes.py).
* Control: starts `python -m autoscaler run` as a subprocess with the chosen policy.
* Data: the controller's SQLite log (DASH_DB, separate from the paper's ~/results database).
* Access: a login page (DASH_USER / DASH_PASSWORD) that sets a session cookie; scripts can send the same
  credentials with HTTP basic auth (`curl -u`). Port DASH_PORT (8501).
"""
from __future__ import annotations

import datetime
import hmac
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path

import requests
from flask import Flask, jsonify, redirect, request, send_from_directory, session

AGENT_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = AGENT_DIR.parent
for p in (str(AGENT_DIR), str(REPO_DIR / "loadgen")):
    if p not in sys.path:
        sys.path.insert(0, p)

from autoscaler.config import SLA_P95_MS, TIERS  # noqa: E402
from autoscaler.decisionlog import SCHEMA  # noqa: E402
from autoscaler.report import summarise  # noqa: E402
from shapes import SCENARIOS, users_at  # noqa: E402

METHODS = ["rl", "threshold", "static"]
TICK_S = 2.0


def measured_mu(default: float = 9.5) -> float:
    """MU_PER_UNIT from the environment, else from the last calibration (~/results/mu.txt)."""
    if os.getenv("MU_PER_UNIT"):
        return float(os.environ["MU_PER_UNIT"])
    f = Path.home() / "results" / "mu.txt"
    try:
        return float(f.read_text().strip().split("=")[-1])
    except (OSError, ValueError):
        return default


class Locust:
    """The few calls of Locust's web API the demo needs."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")

    def set_users(self, users: int) -> None:
        r = requests.post(f"{self.url}/swarm", data={"user_count": users, "spawn_rate": max(10, users)},
                          timeout=5)
        r.raise_for_status()

    def stop(self) -> None:
        r = requests.post(f"{self.url}/stop", timeout=5)          # Locust 2.3x: POST; older: GET
        if r.status_code == 405:
            r = requests.get(f"{self.url}/stop", timeout=5)
        r.raise_for_status()

    def stats(self) -> dict:
        try:
            s = requests.get(f"{self.url}/stats/requests", timeout=2).json()
            return dict(ok=True, state=s.get("state"), users=s.get("user_count", 0),
                        rps=round(s.get("total_rps", s.get("current_rps", 0.0)) or 0.0, 1),
                        fail_ratio=round(s.get("fail_ratio", 0.0) or 0.0, 3))
        except (requests.RequestException, ValueError) as exc:
            return dict(ok=False, error=str(exc)[:200])


class Demo:
    """One demo run at a time: controller subprocess + a thread that sets the Locust user count."""

    def __init__(self, locust: Locust, db_path: str, mu: float, python: str = sys.executable):
        self.locust, self.db_path, self.mu, self.python = locust, db_path, mu, python
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as db:
            db.executescript(SCHEMA)
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.run: dict | None = None
        self.last_run_id: str | None = None
        self.target_users = 0
        self.error = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.run is not None

    def start(self, method: str, scenario: str, peak_users: int, duration_s: int) -> str:
        if method not in METHODS:
            raise ValueError(f"method must be one of {METHODS}")
        if scenario not in SCENARIOS + ["manual"]:
            raise ValueError(f"scenario must be one of {SCENARIOS + ['manual']}")
        peak_users, duration_s = max(1, int(peak_users)), max(30, int(duration_s))
        with self.lock:
            if self.running:
                raise RuntimeError("a run is already in progress")
            run_id = f"demo-{method}-{scenario}-{time.strftime('%Y%m%d-%H%M%S')}"
            log = open(Path(self.db_path).parent / f"{run_id}.log", "w")
            cmd = [self.python, "-m", "autoscaler", "run", "--policy", method, "--start-units", "2",
                   "--duration", "0" if scenario == "manual" else str(duration_s), "--run-id", run_id,
                   "--db", self.db_path, "--mu", str(self.mu)]
            self.proc = subprocess.Popen(cmd, cwd=AGENT_DIR, stdout=log, stderr=subprocess.STDOUT)
            self.run = dict(run_id=run_id, method=method, scenario=scenario, peak_users=peak_users,
                            duration_s=duration_s, started=time.time())
            self.last_run_id, self.target_users, self.error = run_id, 0, ""
            self._stop.clear()
            self._thread = threading.Thread(target=self._drive, daemon=True)
            self._thread.start()
            return run_id

    def set_users(self, users: int) -> None:
        users = max(0, int(users))
        if users == self.target_users:
            return
        if users == 0:
            self.locust.stop()
        else:
            self.locust.set_users(users)
        self.target_users = users

    def _drive(self) -> None:
        run = self.run
        try:
            if run["scenario"] == "manual":
                self.set_users(max(1, run["peak_users"] // 4))
            while not self._stop.is_set():
                t = time.time() - run["started"]
                if run["scenario"] != "manual":
                    if t >= run["duration_s"]:
                        break
                    self.set_users(max(1, round(run["peak_users"] * users_at(t, run["scenario"],
                                                                             run["duration_s"]))))
                if self.proc.poll() is not None:
                    self.error = f"controller exited with code {self.proc.returncode}"
                    break
                self._stop.wait(TICK_S)
        except requests.RequestException as exc:
            self.error = f"load generator unreachable: {exc}"
        finally:
            self._finish()

    def _finish(self) -> None:
        try:
            self.locust.stop()
        except requests.RequestException:
            pass
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.target_users = 0
        self.run = None

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)

    # --- read side -------------------------------------------------------------------------------
    def _db(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.db_path)
        db.row_factory = sqlite3.Row
        return db

    def series(self, run_id: str | None) -> list[dict]:
        if not run_id:
            return []
        with self._db() as db:
            rows = db.execute("SELECT ts, rps, p95_ms, cpu_cores, units_after, note FROM decisions "
                              "WHERE run_id=? ORDER BY step", (run_id,)).fetchall()
        return [dict(r) for r in rows]

    def runs(self, limit: int = 12) -> list[dict]:
        unit_cost = TIERS["vm"]["unit_cost"]
        out = []
        with self._db() as db:
            for r in db.execute("SELECT run_id, policy, started FROM runs ORDER BY started DESC LIMIT ?",
                                (limit,)).fetchall():
                rows = [dict(x) for x in db.execute(
                    "SELECT p95_ms, units_after, applied FROM decisions WHERE run_id=? ORDER BY step",
                    (r["run_id"],))]
                if rows:
                    parts = r["run_id"].split("-")
                    out.append(dict(run_id=r["run_id"], method=r["policy"],
                                    scenario=parts[2] if len(parts) > 2 else "", **summarise(rows, unit_cost)))
        return out

    def state(self) -> dict:
        run = dict(self.run) if self.run else None
        if run:
            run["elapsed_s"] = round(time.time() - run["started"], 1)
        return dict(running=run is not None, run=run, last_run_id=self.last_run_id,
                    target_users=self.target_users, error=self.error, locust=self.locust.stats(),
                    series=self.series(self.last_run_id))


def create_app(demo: Demo, user: str, password: str, secret_key: bytes | None = None) -> Flask:
    app = Flask(__name__, static_folder=str(Path(__file__).with_name("static")))
    # A new random key per start is fine: a restart only means signing in again.
    app.config.update(SECRET_KEY=secret_key or os.urandom(32), SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Strict", PERMANENT_SESSION_LIFETIME=datetime.timedelta(hours=12))
    cpus_per_unit = TIERS["vm"]["cpus_per_unit"]

    def _valid(u: str | None, p: str | None) -> bool:
        return hmac.compare_digest(u or "", user) & hmac.compare_digest(p or "", password)

    @app.before_request
    def _auth():
        if request.endpoint in ("login_page", "login", "static"):
            return None
        if session.get("user") == user:
            return None
        a = request.authorization                   # scripts: curl -u examiner:<password> (no browser pop-up)
        if a and _valid(a.username, a.password):
            return None
        if request.path.startswith("/api/"):
            return jsonify(error="login required"), 401
        return redirect("/login")

    @app.get("/login")
    def login_page():
        if session.get("user") == user:
            return redirect("/")
        return send_from_directory(app.static_folder, "login.html")

    @app.post("/login")
    def login():
        if _valid(request.form.get("username"), request.form.get("password")):
            session.clear()
            session["user"] = user
            session.permanent = True
            return redirect("/", code=303)
        time.sleep(1.0)                             # slows down password guessing
        return redirect("/login?error=1", code=303)

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect("/login", code=303)

    @app.get("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    @app.get("/api/config")
    def config():
        mu = demo.mu
        return jsonify(methods=METHODS, scenarios=["manual"] + SCENARIOS, sla_ms=SLA_P95_MS,
                       max_units=TIERS["vm"]["max_units"], cpus_per_unit=cpus_per_unit, mu_per_unit=mu,
                       default_peak_users=round(0.8 * TIERS["vm"]["max_units"] * mu * 1.25))

    @app.get("/api/state")
    def state():
        return jsonify(demo.state())

    @app.get("/api/runs")
    def runs():
        return jsonify(demo.runs())

    @app.post("/api/start")
    def start():
        b = request.get_json(force=True) or {}
        try:
            run_id = demo.start(b.get("method", "rl"), b.get("scenario", "spike"),
                                int(b.get("peak_users", 40)), int(b.get("duration_s", 300)))
        except (ValueError, RuntimeError) as exc:
            return jsonify(error=str(exc)), 400
        return jsonify(run_id=run_id)

    @app.post("/api/users")
    def users():
        if not demo.running or demo.run["scenario"] != "manual":
            return jsonify(error="manual user count only applies to a running 'manual' scenario"), 400
        try:
            demo.set_users(int((request.get_json(force=True) or {}).get("users", 0)))
        except requests.RequestException as exc:
            return jsonify(error=f"load generator unreachable: {exc}"), 502
        return jsonify(users=demo.target_users)

    @app.post("/api/stop")
    def stop():
        demo.stop()
        return jsonify(stopped=True)

    @app.get("/api/export")
    def export():
        """All decision logs and summaries on this box as one zip (consistent SQLite copies)."""
        data = export_zip(Path.home(), Path(demo.db_path))
        name = f"drl-autoscaler-data-{time.strftime('%Y%m%d-%H%M%S')}.zip"
        return app.response_class(data, mimetype="application/zip",
                                  headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return app


def export_zip(home: Path, demo_db: Path) -> bytes:
    """Zip every results folder under `home` (results*/: decisions.sqlite, summary.csv, mu.txt, logs) plus the
    demo database. SQLite files are copied with the backup API so a run that is writing doesn't corrupt them."""
    import io
    import tempfile
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z, tempfile.TemporaryDirectory() as tmp:
        sources = [(d, d.name) for d in sorted(home.glob("results*")) if d.is_dir()]
        if demo_db.exists():
            sources.append((demo_db.parent, "demo"))
        for folder, arc in sources:
            for f in sorted(folder.iterdir()):
                if not f.is_file():
                    continue
                if f.suffix == ".sqlite":
                    copy = Path(tmp) / f"{arc}-{f.name}"
                    src, dst = sqlite3.connect(f), sqlite3.connect(copy)
                    with dst:
                        src.backup(dst)
                    src.close(); dst.close()
                    z.write(copy, f"{arc}/{f.name}")
                elif f.suffix in (".csv", ".txt", ".log", ".out"):
                    z.write(f, f"{arc}/{f.name}")
    return buf.getvalue()


def main() -> None:
    password = os.getenv("DASH_PASSWORD", "")
    if not password:
        sys.exit("set DASH_PASSWORD (scripts/install-dashboard.sh writes one to /etc/drl-dashboard.env)")
    db = os.getenv("DASH_DB", str(Path.home() / "demo" / "decisions.sqlite"))
    demo = Demo(Locust(os.getenv("LOADGEN_URL", "http://127.0.0.1:8089")), db, measured_mu())
    secret = os.getenv("DASH_SECRET", "").encode() or None
    app = create_app(demo, os.getenv("DASH_USER", "examiner"), password, secret)
    app.run(host="0.0.0.0", port=int(os.getenv("DASH_PORT", "8501")), threaded=True)
