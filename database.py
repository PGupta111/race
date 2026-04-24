import sqlite3
import time
from pathlib import Path

DB_PATH = "race.db"


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS runners (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            bib_number  TEXT UNIQUE NOT NULL,
            name        TEXT NOT NULL,
            category    TEXT NOT NULL,
            email       TEXT DEFAULT '',
            checkin_time TEXT DEFAULT '',
            tshirt      INTEGER DEFAULT 0,
            tshirt_price REAL DEFAULT 5.0,
            checkin_photo TEXT DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS race_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        CREATE TABLE IF NOT EXISTS finish_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            bib_number    TEXT NOT NULL,
            detected_bib  TEXT NOT NULL,
            timestamp     REAL NOT NULL,
            depth_mm      REAL DEFAULT 0,
            depth_ok      INTEGER DEFAULT 0,
            photo_path    TEXT DEFAULT '',
            video_path    TEXT DEFAULT '',
            status        TEXT DEFAULT 'pending',
            override_bib  TEXT DEFAULT '',
            validated_at  TEXT DEFAULT '',
            notes         TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_fe_status    ON finish_events(status);
        CREATE INDEX IF NOT EXISTS idx_fe_bib       ON finish_events(bib_number);
        CREATE INDEX IF NOT EXISTS idx_fe_timestamp ON finish_events(timestamp);
    """)
    conn.commit()
    conn.close()


def seed_runners():
    conn = get_db()
    sample = [
        ("101", "Alice Chen",      "Students"),
        ("102", "Bob Martinez",    "Students"),
        ("103", "Carol Smith",     "Alumni"),
        ("104", "David Lee",       "Alumni"),
        ("105", "Eve Johnson",     "Parents"),
        ("106", "Frank Williams",  "Parents"),
        ("107", "Grace Brown",     "Students"),
        ("108", "Henry Davis",     "Alumni"),
        ("109", "Iris Thompson",   "Parents"),
        ("110", "James Wilson",    "Students"),
    ]
    for bib, name, cat in sample:
        conn.execute(
            "INSERT OR IGNORE INTO runners (bib_number, name, category) VALUES (?, ?, ?)",
            (bib, name, cat),
        )
    conn.commit()
    conn.close()


def get_race_start() -> float | None:
    conn = get_db()
    row = conn.execute("SELECT value FROM race_meta WHERE key = 'race_start'").fetchone()
    conn.close()
    return float(row["value"]) if row else None


def set_race_start(ts: float):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO race_meta (key, value) VALUES ('race_start', ?)",
        (str(ts),),
    )
    conn.commit()
    conn.close()
