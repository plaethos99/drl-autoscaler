"""SQLite log of every decision (one row per 5 s step), used for the paper's comparisons."""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    run_id TEXT, ts REAL, step INTEGER, tier TEXT, policy TEXT, shadow INTEGER,
    rps REAL, p95_ms REAL, inflight REAL, cpu_cores REAL, mem_bytes REAL,
    units_before INTEGER, proposed INTEGER, units_after INTEGER, applied INTEGER,
    raw_action REAL, obs TEXT, note TEXT, pending INTEGER
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, started REAL, tier TEXT, policy TEXT, shadow INTEGER,
    model TEXT, settings TEXT
);
"""


def migrate(db: sqlite3.Connection) -> None:
    """Add columns that newer versions log to a database created by an older one."""
    cols = {r[1] for r in db.execute("PRAGMA table_info(decisions)")}
    if "pending" not in cols:      # units asked for but not ready yet (k8s pods starting)
        db.execute("ALTER TABLE decisions ADD COLUMN pending INTEGER")
        db.commit()


class DecisionLog:
    def __init__(self, path: str):
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        migrate(self.db)

    def start_run(self, run_id: str, tier: str, policy: str, shadow: bool, model: str, settings: dict):
        self.db.execute("INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?)",
                        (run_id, time.time(), tier, policy, int(shadow), model, json.dumps(settings, default=str)))
        self.db.commit()

    def record(self, **row):
        row.setdefault("ts", time.time())
        if row.get("obs") is not None and not isinstance(row["obs"], str):
            row["obs"] = json.dumps([round(float(x), 4) for x in row["obs"]])
        cols = ",".join(row)
        self.db.execute(f"INSERT INTO decisions ({cols}) VALUES ({','.join('?' * len(row))})", list(row.values()))
        self.db.commit()

    def close(self):
        self.db.close()
