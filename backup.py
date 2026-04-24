#!/usr/bin/env python3
"""
SQLite online backup — WAL-safe; keeps last 30 daily snapshots.
Called by systemd timer or via POST /api/admin/backup.
"""
import sqlite3
from datetime import date
from pathlib import Path

DB_SRC    = Path("race.db")
BAK_DIR   = Path("backups")
KEEP_DAYS = 30


def run() -> Path:
    BAK_DIR.mkdir(exist_ok=True)
    dst = BAK_DIR / f"race_{date.today().isoformat()}.db"

    # sqlite3.Connection.backup() is WAL-aware and consistent
    src = sqlite3.connect(str(DB_SRC))
    out = sqlite3.connect(str(dst))
    src.backup(out)
    out.close()
    src.close()

    # Prune old backups beyond retention window
    backups = sorted(BAK_DIR.glob("race_*.db"))
    for old in backups[:-KEEP_DAYS]:
        old.unlink()

    return dst


if __name__ == "__main__":
    path = run()
    print(f"Backup written: {path}")
