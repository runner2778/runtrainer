"""SQLite 连接管理：每次调用短连接（pywebview js_api 各线程独立）+ WAL + 迁移执行。"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .. import config

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


@contextmanager
def get_conn():
    conn = sqlite3.connect(config.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def migrate() -> None:
    """按 PRAGMA user_version 依次执行 migrations/*.sql。"""
    config.ensure_dirs()
    with get_conn() as conn:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
            version = int(f.stem.split("_", 1)[0])
            if version <= current:
                continue
            conn.executescript(f.read_text(encoding="utf-8"))
            conn.execute(f"PRAGMA user_version = {version}")


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None
