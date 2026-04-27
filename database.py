import sqlite3
import uuid
from pathlib import Path
from typing import List, Optional

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
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            registration_id TEXT UNIQUE NOT NULL,
            name            TEXT NOT NULL,
            email           TEXT DEFAULT '',
            category        TEXT NOT NULL,
            bib_number      TEXT DEFAULT '',
            bib_photo       TEXT DEFAULT '',
            checkin_time    TEXT DEFAULT '',
            tshirt          INTEGER DEFAULT 0,
            tshirt_price    REAL DEFAULT 5.0,
            checkin_photo   TEXT DEFAULT ''
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

        CREATE TABLE IF NOT EXISTS crossing_events (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       REAL NOT NULL,
            ribbon_crop     TEXT DEFAULT '',
            matched         INTEGER DEFAULT 0,
            matched_bib     TEXT DEFAULT '',
            finish_event_id INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_fe_status     ON finish_events(status);
        CREATE INDEX IF NOT EXISTS idx_fe_bib        ON finish_events(bib_number);
        CREATE INDEX IF NOT EXISTS idx_fe_timestamp   ON finish_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_cx_matched     ON crossing_events(matched);
        CREATE INDEX IF NOT EXISTS idx_cx_timestamp   ON crossing_events(timestamp);
        CREATE INDEX IF NOT EXISTS idx_r_bib          ON runners(bib_number);
        CREATE INDEX IF NOT EXISTS idx_r_regid        ON runners(registration_id);
    """)
    conn.commit()
    conn.close()


def seed_runners():
    """Seed sample runners only when the table is completely empty."""
    conn = get_db()
    count = conn.execute("SELECT COUNT(*) FROM runners").fetchone()[0]
    if count > 0:
        conn.close()
        return

    sample = [
        ("Alice Chen",     "Students", "alice@example.com"),
        ("Bob Martinez",   "Students", "bob@example.com"),
        ("Carol Smith",    "Alumni",   "carol@example.com"),
        ("David Lee",      "Alumni",   "david@example.com"),
        ("Eve Johnson",    "Parents",  "eve@example.com"),
        ("Frank Williams", "Parents",  "frank@example.com"),
        ("Grace Brown",    "Students", "grace@example.com"),
        ("Henry Davis",    "Alumni",   "henry@example.com"),
        ("Iris Thompson",  "Parents",  "iris@example.com"),
        ("James Wilson",   "Students", "james@example.com"),
    ]
    for name, cat, email in sample:
        reg_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO runners (registration_id, name, category, email) VALUES (?, ?, ?, ?)",
            (reg_id, name, cat, email),
        )
    conn.commit()
    conn.close()


def register_runner(name: str, email: str, category: str) -> dict:
    """Create a new runner registration. Returns the runner dict with registration_id."""
    reg_id = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO runners (registration_id, name, category, email) VALUES (?, ?, ?, ?)",
        (reg_id, name, category, email),
    )
    conn.commit()
    row = conn.execute(
        "SELECT * FROM runners WHERE registration_id=?", (reg_id,)
    ).fetchone()
    conn.close()
    return dict(row)


def lookup_by_registration(reg_id: str) -> Optional[dict]:
    conn = get_db()
    row = conn.execute(
        "SELECT * FROM runners WHERE registration_id=?", (reg_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def search_runners(query: str) -> List[dict]:
    """Search runners by name (case-insensitive partial match)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM runners WHERE name LIKE ? ORDER BY name LIMIT 20",
        (f"%{query}%",),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def assign_bib(runner_id: int, bib_number: str, bib_photo: str) -> Optional[dict]:
    """Assign a bib number and bib photo to a runner during check-in."""
    conn = get_db()
    conn.execute(
        "UPDATE runners SET bib_number=?, bib_photo=? WHERE id=?",
        (bib_number, bib_photo, runner_id),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM runners WHERE id=?", (runner_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_all_bib_photos() -> List[dict]:
    """Get all runners with assigned bibs and their bib photos (for visual matching)."""
    conn = get_db()
    rows = conn.execute(
        "SELECT id, bib_number, name, category, bib_photo FROM runners WHERE bib_number != '' AND bib_photo != ''"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_finish_event(event_id: int) -> Optional[dict]:
    conn = get_db()
    row = conn.execute("SELECT * FROM finish_events WHERE id=?", (event_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def get_race_start() -> Optional[float]:
    conn = get_db()
    row = conn.execute("SELECT value FROM race_meta WHERE key='race_start'").fetchone()
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
