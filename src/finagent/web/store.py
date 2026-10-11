"""Small durable HTTP catalogue; agent memory and accounting keep their own ledgers."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock


def now():
    return datetime.now(UTC).isoformat()


class WebStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = RLock()
        with self.connection() as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS objects (kind TEXT, id TEXT, payload TEXT NOT NULL, PRIMARY KEY(kind,id))")

    @contextmanager
    def connection(self):
        with self.lock:
            conn = sqlite3.connect(self.path, timeout=30)
            try:
                yield conn
                conn.commit()
            finally:
                conn.close()

    def put(self, kind, value):
        with self.connection() as conn:
            conn.execute("INSERT OR REPLACE INTO objects VALUES (?,?,?)",
                         (kind, value["id"], json.dumps(value, ensure_ascii=False, allow_nan=False)))

    def get(self, kind, identity):
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM objects WHERE kind=? AND id=?", (kind, identity)).fetchone()
        return json.loads(row[0]) if row else None

    def delete(self, kind, identity):
        """Remove a catalogue entry without touching historical run files."""
        with self.connection() as conn:
            conn.execute("DELETE FROM objects WHERE kind=? AND id=?", (kind, identity))

    def insert_run(self, value):
        """Atomically admit a session turn and at most one live run across servers."""
        with self.connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute("SELECT payload FROM objects WHERE kind='run'").fetchall()
            active = [json.loads(row[0]) for row in rows if json.loads(row[0])["status"] in {"queued", "running"}]
            if any(run["session_id"] == value["session_id"] for run in active):
                raise ValueError("当前会话已有正在运行的任务")
            if value["mode"] == "live" and any(run["mode"] == "live" for run in active):
                raise ValueError("已有真实 API 任务运行，请等待完成")
            conn.execute("INSERT INTO objects VALUES ('run',?,?)", (value["id"], json.dumps(value, ensure_ascii=False)))

    def all(self, kind):
        with self.connection() as conn:
            rows = conn.execute("SELECT payload FROM objects WHERE kind=?", (kind,)).fetchall()
        return sorted((json.loads(row[0]) for row in rows), key=lambda row: row.get("created_at", ""))

    def update(self, kind, identity, mutate):
        with self.connection() as conn:
            row = conn.execute("SELECT payload FROM objects WHERE kind=? AND id=?", (kind, identity)).fetchone()
            if not row:
                return None
            value = json.loads(row[0])
            mutate(value)
            conn.execute("UPDATE objects SET payload=? WHERE kind=? AND id=?",
                         (json.dumps(value, ensure_ascii=False, allow_nan=False), kind, identity))
        return value
