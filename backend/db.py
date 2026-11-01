"""SQLite storage for jobs, stages and events. Everything survives a restart."""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY, company TEXT NOT NULL, objective TEXT NOT NULL DEFAULT '',
  params TEXT NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS stages (
  job_id TEXT NOT NULL, name TEXT NOT NULL, status TEXT NOT NULL,
  started_at TEXT, finished_at TEXT, error TEXT, meta TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (job_id, name));
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL, ts TEXT NOT NULL,
  level TEXT NOT NULL, message TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS idx_events_job ON events(job_id, id);
"""

_STAGE_FIELDS = {"status", "started_at", "finished_at", "error", "meta"}


class DB:
    def __init__(self, path):
        self.path = str(path)
        self._lock = threading.Lock()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.tx() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        with self._lock:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    # ---- jobs
    def create_job(self, job_id: str, company: str, objective: str, params: dict) -> None:
        t = now()
        with self.tx() as c:
            c.execute("INSERT INTO jobs (id, company, objective, params, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                      (job_id, company, objective, json.dumps(params), t, t))

    @staticmethod
    def _job(row) -> dict:
        d = dict(row)
        d["params"] = json.loads(d["params"])
        d["cancelled"] = bool(d["cancelled"])
        return d

    def get_job(self, job_id: str) -> Optional[dict]:
        with self.tx() as c:
            row = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return self._job(row) if row else None

    def list_jobs(self, limit: int = 50, offset: int = 0) -> list:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM jobs ORDER BY created_at DESC, rowid DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [self._job(r) for r in rows]

    def update_params(self, job_id: str, params: dict) -> None:
        with self.tx() as c:
            c.execute("UPDATE jobs SET params=?, updated_at=? WHERE id=?", (json.dumps(params), now(), job_id))

    def set_cancelled(self, job_id: str, value: bool) -> None:
        with self.tx() as c:
            c.execute("UPDATE jobs SET cancelled=?, updated_at=? WHERE id=?", (1 if value else 0, now(), job_id))

    def touch_job(self, job_id: str) -> None:
        with self.tx() as c:
            c.execute("UPDATE jobs SET updated_at=? WHERE id=?", (now(), job_id))

    def delete_job(self, job_id: str) -> None:
        with self.tx() as c:
            for table in ("events", "stages"):
                c.execute(f"DELETE FROM {table} WHERE job_id=?", (job_id,))
            c.execute("DELETE FROM jobs WHERE id=?", (job_id,))

    # ---- stages
    def ensure_stage(self, job_id: str, name: str, status: str = "pending") -> None:
        with self.tx() as c:
            c.execute("INSERT OR IGNORE INTO stages (job_id, name, status) VALUES (?,?,?)", (job_id, name, status))

    def update_stage(self, job_id: str, name: str, **fields) -> None:
        bad = set(fields) - _STAGE_FIELDS
        if bad:
            raise ValueError(f"unknown stage fields: {bad}")
        if "meta" in fields:
            fields["meta"] = json.dumps(fields["meta"] or {})
        sets = ", ".join(f"{k}=?" for k in fields)
        with self.tx() as c:
            c.execute(f"UPDATE stages SET {sets} WHERE job_id=? AND name=?", (*fields.values(), job_id, name))

    def get_stages(self, job_id: str) -> dict:
        with self.tx() as c:
            rows = c.execute("SELECT * FROM stages WHERE job_id=?", (job_id,)).fetchall()
        out = {}
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d["meta"] or "{}")
            out[d["name"]] = d
        return out

    def recover_interrupted(self) -> int:
        """Stages left 'running' by a previous process can never finish: mark them failed so they can be retried."""
        with self.tx() as c:
            cur = c.execute("UPDATE stages SET status='failed', finished_at=?, error='interrupted by a server restart' "
                            "WHERE status='running'", (now(),))
            return cur.rowcount

    # ---- events
    def add_event(self, job_id: str, level: str, message: str) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO events (job_id, ts, level, message) VALUES (?,?,?,?)", (job_id, now(), level, message[:4000]))

    def get_events(self, job_id: str, after: int = 0, limit: int = 200) -> list:
        with self.tx() as c:
            rows = c.execute("SELECT id, ts, level, message FROM events WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                             (job_id, after, limit)).fetchall()
        return [dict(r) for r in rows]
