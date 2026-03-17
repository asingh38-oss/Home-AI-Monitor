"""
event_store.py
SQLite database storing all camera motion events and sensor events.
Used by the AI chat to answer questions about historical activity.
Retains 3 months of data, auto-purging older records nightly.
"""

import asyncio
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

log = logging.getLogger("event_store")

RETENTION_DAYS = 90  # 3 months

SCHEMA = """
CREATE TABLE IF NOT EXISTS camera_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    camera      TEXT NOT NULL,
    zone        TEXT,
    subject     TEXT,          -- human | animal | vehicle | object | empty
    description TEXT,          -- Claude's one-sentence description
    threat      TEXT,          -- none | low | medium | high
    priority    TEXT,          -- LOW | MEDIUM | HIGH
    known_faces TEXT,          -- comma-separated names, or empty
    confidence  REAL,
    unusual     INTEGER DEFAULT 0,
    clip_path   TEXT,
    snapshot_path TEXT
);

CREATE TABLE IF NOT EXISTS sensor_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp   TEXT NOT NULL,
    sensor_name TEXT NOT NULL,
    sensor_type TEXT,           -- door | window
    state       TEXT NOT NULL,  -- open | closed
    priority    TEXT
);

CREATE INDEX IF NOT EXISTS idx_camera_ts   ON camera_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_camera_subj ON camera_events(subject);
CREATE INDEX IF NOT EXISTS idx_camera_cam  ON camera_events(camera);
CREATE INDEX IF NOT EXISTS idx_sensor_ts   ON sensor_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_sensor_name ON sensor_events(sensor_name);
"""


class EventStore:
    def __init__(self, config: dict):
        storage_cfg = config.get("storage", {})
        db_path = storage_cfg.get("db_path", "events.db")
        self.db_path = Path(db_path)
        self._init_db()

    def _init_db(self):
        with self._conn() as conn:
            conn.executescript(SCHEMA)
        log.info("EventStore ready at %s", self.db_path)

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ── Write ─────────────────────────────────────────────────────────────────

    def record_camera_event(
        self,
        timestamp: datetime,
        camera: str,
        zone: str,
        confidence: float,
        ai_result: Optional[dict] = None,
        known_faces: Optional[list] = None,
        priority: str = "MEDIUM",
        clip_path: Optional[str] = None,
        snapshot_path: Optional[str] = None,
    ):
        subject     = ai_result.get("subject", "unknown")     if ai_result else "unknown"
        description = ai_result.get("description", "")        if ai_result else ""
        threat      = ai_result.get("threat_level", "none")   if ai_result else "none"
        unusual     = 1 if ai_result and ai_result.get("unusual") else 0
        faces       = ", ".join(known_faces) if known_faces else ""

        with self._conn() as conn:
            conn.execute(
                """INSERT INTO camera_events
                   (timestamp, camera, zone, subject, description, threat,
                    priority, known_faces, confidence, unusual, clip_path, snapshot_path)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (timestamp.isoformat(), camera, zone, subject, description,
                 threat, priority, faces, round(confidence, 3), unusual,
                 clip_path, snapshot_path),
            )

    def record_sensor_event(
        self,
        timestamp: datetime,
        sensor_name: str,
        sensor_type: str,
        state: str,
        priority: str,
    ):
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO sensor_events
                   (timestamp, sensor_name, sensor_type, state, priority)
                   VALUES (?,?,?,?,?)""",
                (timestamp.isoformat(), sensor_name, sensor_type, state, priority),
            )

    # ── Read ──────────────────────────────────────────────────────────────────

    def query_camera_events(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        camera: Optional[str] = None,
        subject: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        clauses = []
        params  = []

        if since:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        if until:
            clauses.append("timestamp <= ?")
            params.append(until.isoformat())
        if camera:
            clauses.append("camera LIKE ?")
            params.append(f"%{camera}%")
        if subject:
            clauses.append("subject = ?")
            params.append(subject)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM camera_events {where} ORDER BY timestamp DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def query_sensor_events(
        self,
        since: Optional[datetime] = None,
        until: Optional[datetime] = None,
        sensor_name: Optional[str] = None,
        state: Optional[str] = None,
        limit: int = 500,
    ) -> list[dict]:
        clauses = []
        params  = []

        if since:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        if until:
            clauses.append("timestamp <= ?")
            params.append(until.isoformat())
        if sensor_name:
            clauses.append("sensor_name LIKE ?")
            params.append(f"%{sensor_name}%")
        if state:
            clauses.append("state = ?")
            params.append(state)

        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM sensor_events {where} ORDER BY timestamp DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]

    def get_summary_stats(self, since: datetime) -> dict:
        """High-level counts used to prime the AI with context."""
        with self._conn() as conn:
            cam = conn.execute(
                "SELECT subject, COUNT(*) as cnt FROM camera_events "
                "WHERE timestamp >= ? GROUP BY subject",
                (since.isoformat(),),
            ).fetchall()

            sensors = conn.execute(
                "SELECT sensor_name, state, COUNT(*) as cnt FROM sensor_events "
                "WHERE timestamp >= ? GROUP BY sensor_name, state",
                (since.isoformat(),),
            ).fetchall()

            cameras = conn.execute(
                "SELECT DISTINCT camera FROM camera_events WHERE timestamp >= ?",
                (since.isoformat(),),
            ).fetchall()

        return {
            "by_subject":   {r["subject"]: r["cnt"] for r in cam},
            "by_sensor":    [dict(r) for r in sensors],
            "active_cameras": [r["camera"] for r in cameras],
        }

    # ── Maintenance ───────────────────────────────────────────────────────────

    def purge_old_events(self, retention_days: int = RETENTION_DAYS) -> int:
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        with self._conn() as conn:
            c1 = conn.execute("DELETE FROM camera_events WHERE timestamp < ?", (cutoff,)).rowcount
            c2 = conn.execute("DELETE FROM sensor_events WHERE timestamp < ?", (cutoff,)).rowcount
        total = c1 + c2
        if total:
            log.info("Purged %d old events (older than %d days)", total, retention_days)
        return total

    def get_event_by_id(self, event_id: int) -> Optional[dict]:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM camera_events WHERE id = ?", (event_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_events_by_ids(self, ids: list[int]) -> list[dict]:
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM camera_events WHERE id IN ({placeholders})", ids
            ).fetchall()
        return [dict(r) for r in rows]

    async def run(self, stop_event: asyncio.Event):
        """Nightly purge loop."""
        while not stop_event.is_set():
            self.purge_old_events()
            # Sleep 24h or until stop
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=86400)
            except asyncio.TimeoutError:
                pass
