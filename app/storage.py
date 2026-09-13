"""SQLite 持久化：输入哈希、映射、代价与裁决。"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT PRIMARY KEY,
    input_hash    TEXT NOT NULL,
    status        TEXT NOT NULL,              -- created / published
    blocked       INTEGER NOT NULL,           -- 1 = 不可发布
    needle_count  INTEGER NOT NULL,
    result        TEXT NOT NULL,              -- 完整裁决、映射、代价（JSON）
    created_at    TEXT NOT NULL,
    published_at  TEXT
);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def db_path() -> str:
    return os.environ.get("DB_PATH", "/data/jacquard.db")


def _connect(path: str) -> sqlite3.Connection:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path: str | None = None) -> None:
    with _connect(path or db_path()) as conn:
        conn.executescript(SCHEMA)


@contextmanager
def connect(path: str | None = None) -> Iterator[sqlite3.Connection]:
    conn = _connect(path or db_path())
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def insert_task(task_id: str, result: dict[str, Any], path: str | None = None) -> str:
    created_at = utc_now_iso()
    with connect(path) as conn:
        conn.execute(
            """
            INSERT INTO tasks
                (id, input_hash, status, blocked, needle_count, result,
                 created_at, published_at)
            VALUES (?, ?, 'created', ?, ?, ?, ?, NULL)
            """,
            (
                task_id,
                result["input_hash"],
                1 if result["status"] == "blocked" else 0,
                result["needle_count"],
                json.dumps(result, ensure_ascii=True, sort_keys=True),
                created_at,
            ),
        )
    return created_at


def get_task_row(task_id: str, path: str | None = None) -> dict[str, Any] | None:
    with connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["result"] = json.loads(d["result"])
    return d


def mark_published(task_id: str, path: str | None = None) -> str | None:
    """幂等发布；返回发布时间，任务不存在返回 None。"""
    published_at = utc_now_iso()
    with connect(path) as conn:
        cur = conn.execute(
            """
            UPDATE tasks
               SET status = 'published', published_at = COALESCE(published_at, ?)
             WHERE id = ?
            """,
            (published_at, task_id),
        )
        if cur.rowcount == 0:
            return None
        row = conn.execute(
            "SELECT published_at FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
    return row["published_at"]
