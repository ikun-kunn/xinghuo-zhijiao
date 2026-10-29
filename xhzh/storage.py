"""SQLite 存储：生成记录与教学包索引（赛事版三张表之二，review_notes 预留）。

Notes
-----
每次操作独立连接、用完即关：``with connection`` 只提交事务不关闭连接，
长驻服务下会泄漏文件句柄（Windows 上最终锁库），必须配 ``closing``。
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from typing import Any

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS generations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    grade TEXT NOT NULL,
    subject TEXT NOT NULL,
    textbook TEXT NOT NULL,
    topic TEXT NOT NULL,
    status TEXT NOT NULL,
    detail TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS packages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    generation_id INTEGER NOT NULL REFERENCES generations(id),
    zip_path TEXT NOT NULL,
    docx_path TEXT NOT NULL,
    pptx_path TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def _connect() -> sqlite3.Connection:
    """Open a database connection with the schema applied once per process.

    Returns
    -------
    sqlite3.Connection
        Caller is responsible for closing (use :func:`_tx`).

    Notes
    -----
    两处与并发相关的修正（MCP 服务会引入第二个写线程）：

    1. **DDL 只执行一次**：``executescript`` 本身是写操作，早期实现每次开连接
       都跑一遍 ``CREATE TABLE IF NOT EXISTS``，与写线程并发时会升级事务失败；
    2. **回到回滚日志（DELETE）**：WAL 在同步盘目录下需要稳定的 ``-shm``
       共享内存，实测并发写会退化为 ``attempt to write a readonly database``。
       ``busy_timeout`` 足以覆盖本项目"单机双进程 + 双线程"的并发量。
    """
    global _SCHEMA_READY
    config.ensure_dirs()
    connection = sqlite3.connect(config.DB_PATH, timeout=5.0)
    connection.execute("PRAGMA busy_timeout=5000")
    if not _SCHEMA_READY:
        with _SCHEMA_LOCK:
            if not _SCHEMA_READY:
                connection.executescript(_SCHEMA)
                _SCHEMA_READY = True
    return connection


@contextlib.contextmanager
def _tx() -> "contextlib.AbstractContextManager[sqlite3.Connection]":
    """Yield an auto-committing, always-closing connection.

    Yields
    ------
    sqlite3.Connection
    """
    with contextlib.closing(_connect()) as connection, connection:
        yield connection


def save_generation(params: dict[str, Any], status: str, detail: dict[str, Any] | None = None) -> int:
    """Insert a generation record and return its id.

    Parameters
    ----------
    params : dict
        Keys grade / subject / textbook / topic.
    status : str
        ``"running"`` / ``"done"`` / ``"failed"``.
    detail : dict or None
        Extra info stored as JSON (node trace, warnings...).

    Returns
    -------
    int
        The new row id.
    """
    with _tx() as connection:
        cursor = connection.execute(
            "INSERT INTO generations (grade, subject, textbook, topic, status, detail) VALUES (?,?,?,?,?,?)",
            (
                params["grade"],
                params["subject"],
                params["textbook"],
                params["topic"],
                status,
                json.dumps(detail, ensure_ascii=False) if detail else None,
            ),
        )
        return int(cursor.lastrowid)


def update_generation_status(
    generation_id: int, status: str, detail: dict[str, Any] | None = None
) -> None:
    """Update the status of an existing generation record (merge detail).

    Parameters
    ----------
    generation_id : int
        Row id returned by :func:`save_generation`.
    status : str
        New status, e.g. ``"done"`` / ``"failed"``.
    detail : dict or None
        Merged into the existing ``detail`` JSON (old keys kept unless
        overwritten), so node traces and error messages accumulate.

    Returns
    -------
    None
    """
    with _tx() as connection:
        if detail is None:
            connection.execute(
                "UPDATE generations SET status=? WHERE id=?", (status, generation_id)
            )
            return
        row = connection.execute(
            "SELECT detail FROM generations WHERE id=?", (generation_id,)
        ).fetchone()
        merged: dict[str, Any] = {}
        if row and row[0]:
            try:
                parsed = json.loads(row[0])
                if isinstance(parsed, dict):
                    merged = parsed
            except json.JSONDecodeError:
                merged = {}
        merged.update(detail)
        connection.execute(
            "UPDATE generations SET status=?, detail=? WHERE id=?",
            (status, json.dumps(merged, ensure_ascii=False), generation_id),
        )


def save_package(generation_id: int, package: dict[str, str]) -> None:
    """Attach generated package paths to a generation record.

    Parameters
    ----------
    generation_id : int
    package : dict
        Keys zip / docx / pptx.

    Returns
    -------
    None
    """
    with _tx() as connection:
        connection.execute(
            "INSERT INTO packages (generation_id, zip_path, docx_path, pptx_path) VALUES (?,?,?,?)",
            (generation_id, package["zip"], package["docx"], package["pptx"]),
        )


def mark_stale_running_failed(reason: str = "服务重启，生成中断") -> int:
    """Mark orphaned ``running`` records as failed (called at startup).

    Parameters
    ----------
    reason : str
        Error message merged into the record's ``detail``.

    Returns
    -------
    int
        Number of records cleaned; ``0`` on a healthy database.
    """
    with _tx() as connection:
        rows = connection.execute(
            "SELECT id, detail FROM generations WHERE status='running'"
        ).fetchall()
        for rid, detail_json in rows:
            merged: dict[str, Any] = {}
            if detail_json:
                try:
                    parsed = json.loads(detail_json)
                    if isinstance(parsed, dict):
                        merged = parsed
                except json.JSONDecodeError:
                    merged = {}
            merged.setdefault("error", reason)
            connection.execute(
                "UPDATE generations SET status='failed', detail=? WHERE id=?",
                (json.dumps(merged, ensure_ascii=False), rid),
            )
        return len(rows)


def list_recent_generations(limit: int = 10) -> list[tuple[Any, ...]]:
    """Return the most recent generation records for the history panel.

    Parameters
    ----------
    limit : int
        Max rows to return.

    Returns
    -------
    list of tuple
        ``(id, grade, subject, topic, status, created_at)`` newest first.
    """
    with _tx() as connection:
        return connection.execute(
            "SELECT id, grade, subject, topic, status, created_at "
            "FROM generations ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_generation(generation_id: int) -> dict[str, Any] | None:
    """Fetch one generation record by id (MCP 通道只读入口).

    Parameters
    ----------
    generation_id : int
        Row id returned by :func:`save_generation`.

    Returns
    -------
    dict or None
        ``{id, grade, subject, textbook, topic, status, detail, created_at}``;
        ``detail`` 已解析为 dict（损坏的 JSON 归一为空 dict）。不存在返回 None。

    Notes
    -----
    只读查询，不改变既有函数语义；供 MCP 的 ``get_generation_status`` /
    ``resources/read`` 使用，避免协议层直接拼 SQL。
    """
    with _tx() as connection:
        row = connection.execute(
            "SELECT id, grade, subject, textbook, topic, status, detail, created_at "
            "FROM generations WHERE id=?",
            (int(generation_id),),
        ).fetchone()
    if row is None:
        return None
    detail: dict[str, Any] = {}
    if row[6]:
        try:
            parsed = json.loads(row[6])
            if isinstance(parsed, dict):
                detail = parsed
        except (json.JSONDecodeError, TypeError):
            detail = {}
    return {
        "id": int(row[0]),
        "grade": row[1],
        "subject": row[2],
        "textbook": row[3],
        "topic": row[4],
        "status": row[5],
        "detail": detail,
        "created_at": row[7],
    }


def get_package(generation_id: int) -> dict[str, str] | None:
    """Fetch the package paths attached to a generation.

    Parameters
    ----------
    generation_id : int

    Returns
    -------
    dict or None
        ``{"zip": ..., "docx": ..., "pptx": ...}``（绝对路径，调用方必须做
        白名单校验后再使用）；该任务尚无产物时返回 None。

    Notes
    -----
    同一任务可能因重试写入多行，取 id 最大（最新）的一行。
    """
    with _tx() as connection:
        row = connection.execute(
            "SELECT zip_path, docx_path, pptx_path FROM packages "
            "WHERE generation_id=? ORDER BY id DESC LIMIT 1",
            (int(generation_id),),
        ).fetchone()
    if row is None:
        return None
    return {"zip": row[0], "docx": row[1], "pptx": row[2]}
