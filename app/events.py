"""Event log: alerts are saved to SQLite and written to the log.

A cooldown per (camera, event type) prevents the same alert from flooding the
log, e.g. "motion detected" at most once every 30 seconds per camera.

Alerts can carry a snapshot: the annotated camera picture at that moment, saved as
data/snapshots/<event id>.jpg and shown as a thumbnail on the dashboard. Only the
newest KEEP_SNAPSHOTS pictures are kept, so the disk doesn't fill up.
"""
import csv
import io
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime

log = logging.getLogger("events")

KEEP_SNAPSHOTS = 500
COLUMNS = ("id", "ts", "time", "camera_id", "camera_name", "type", "message", "value", "has_snapshot")


class EventStore:
    def __init__(self, db_path="data/events.db", cooldown_s=30):
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.snapshot_dir = os.path.join(os.path.dirname(db_path) or ".", "snapshots")
        os.makedirs(self.snapshot_dir, exist_ok=True)
        # check_same_thread=False: all camera threads share this connection; the lock keeps it safe.
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS events (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   ts REAL, time TEXT, camera_id TEXT, camera_name TEXT,
                   type TEXT, message TEXT, value REAL)"""
        )
        # Databases from older versions don't have this column yet.
        existing = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
        if "has_snapshot" not in existing:
            self.db.execute("ALTER TABLE events ADD COLUMN has_snapshot INTEGER DEFAULT 0")
        self.db.commit()
        self.lock = threading.Lock()
        self.cooldown_s = cooldown_s
        self.last_sent = {}  # (camera_id, type) -> time of the last saved event

    def add(self, camera_id, camera_name, event_type, message, value=None, cooldown=True, snapshot=None):
        """Save an event. Returns False if it was skipped because of the cooldown.
        snapshot: optional function returning JPEG bytes. It is only called when the
        event is really saved, so no picture is encoded for alerts in their cooldown."""
        now = time.time()
        key = (camera_id, event_type)
        with self.lock:
            if cooldown and now - self.last_sent.get(key, 0) < self.cooldown_s:
                return False
            self.last_sent[key] = now
            jpeg = snapshot() if snapshot else None
            cur = self.db.execute(
                "INSERT INTO events (ts, time, camera_id, camera_name, type, message, value, has_snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (now, datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
                 camera_id, camera_name, event_type, message, value, 1 if jpeg else 0),
            )
            self.db.commit()
            event_id = cur.lastrowid
        if jpeg:
            with open(self.snapshot_path(event_id), "wb") as f:
                f.write(jpeg)
            old = self.snapshot_path(event_id - KEEP_SNAPSHOTS)
            if os.path.exists(old):
                os.remove(old)
        log.warning("[%s] %s: %s", camera_id, event_type, message)
        return True

    def snapshot_path(self, event_id):
        return os.path.join(self.snapshot_dir, "%d.jpg" % event_id)

    def recent(self, limit=50):
        with self.lock:
            rows = self.db.execute(
                "SELECT %s FROM events ORDER BY id DESC LIMIT ?" % ", ".join(COLUMNS), (limit,)
            ).fetchall()
        # ts = Unix timestamp, so the browser can show it in the viewer's own time zone
        # (inside Docker the server clock is UTC).
        events = [dict(zip(COLUMNS, r)) for r in rows]
        for e in events:  # the file may have been pruned
            e["has_snapshot"] = bool(e["has_snapshot"]) and os.path.exists(self.snapshot_path(e["id"]))
        return events

    def _all_rows(self):
        with self.lock:
            return self.db.execute(
                "SELECT time, camera_id, camera_name, type, message, value FROM events ORDER BY id"
            ).fetchall()

    def csv_text(self):
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["time", "camera_id", "camera_name", "type", "message", "value"])
        writer.writerows(self._all_rows())
        return out.getvalue()

    def export_csv(self, path):
        rows = self._all_rows()
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["time", "camera_id", "camera_name", "type", "message", "value"])
            writer.writerows(rows)
        return len(rows)
