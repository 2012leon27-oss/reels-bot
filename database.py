"""Persistent storage for source transcripts and structured notes.

PostgreSQL is used when ``DATABASE_URL`` is configured. A local SQLite
database keeps development and single-server installations fully functional.
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

import asyncpg

from config import settings

_pool: asyncpg.Pool | None = None
_sqlite_lock = asyncio.Lock()
_backend = "uninitialized"


def _json_dump(value: dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False)


def _serialize_row(row: dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    structured = result.get("structured_json")
    if isinstance(structured, str):
        result["structured"] = json.loads(structured)
    elif structured is not None:
        result["structured"] = structured
    result.pop("structured_json", None)
    created_at = result.get("created_at")
    if isinstance(created_at, datetime):
        result["created_at"] = created_at.astimezone(timezone.utc).isoformat()
    return result


def _sqlite_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(settings.sqlite_path)
    connection.row_factory = sqlite3.Row
    return connection


def _init_sqlite() -> None:
    directory = os.path.dirname(settings.sqlite_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with _sqlite_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS thought_notes (
                id TEXT PRIMARY KEY,
                share_id TEXT UNIQUE NOT NULL,
                owner_id TEXT NOT NULL,
                title TEXT NOT NULL,
                source_text TEXT NOT NULL,
                structured_json TEXT NOT NULL,
                structured_markdown TEXT NOT NULL,
                mode TEXT NOT NULL,
                created_at TEXT NOT NULL,
                share_enabled INTEGER NOT NULL DEFAULT 0,
                share_source INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(thought_notes)").fetchall()
        }
        if "share_source" not in columns:
            connection.execute(
                "ALTER TABLE thought_notes ADD COLUMN share_source INTEGER NOT NULL DEFAULT 0"
            )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_thought_notes_owner_created
            ON thought_notes(owner_id, created_at DESC)
            """
        )


async def init_db() -> None:
    """Initialize the configured database backend and schema."""
    global _pool, _backend
    if settings.database_url:
        _pool = await asyncpg.create_pool(
            settings.database_url, min_size=1, max_size=5
        )
        async with _pool.acquire() as connection:
            await connection.execute(
                """
                CREATE TABLE IF NOT EXISTS thought_notes (
                    id TEXT PRIMARY KEY,
                    share_id TEXT UNIQUE NOT NULL,
                    owner_id TEXT NOT NULL,
                    title TEXT NOT NULL,
                    source_text TEXT NOT NULL,
                    structured_json TEXT NOT NULL,
                    structured_markdown TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    share_enabled BOOLEAN NOT NULL DEFAULT FALSE,
                    share_source BOOLEAN NOT NULL DEFAULT FALSE
                )
                """
            )
            await connection.execute(
                """
                ALTER TABLE thought_notes
                ADD COLUMN IF NOT EXISTS share_source BOOLEAN NOT NULL DEFAULT FALSE
                """
            )
            await connection.execute(
                """
                ALTER TABLE thought_notes
                ALTER COLUMN share_enabled SET DEFAULT FALSE
                """
            )
            await connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_thought_notes_owner_created
                ON thought_notes(owner_id, created_at DESC)
                """
            )
        _backend = "postgresql"
    else:
        await asyncio.to_thread(_init_sqlite)
        _backend = "sqlite"


async def close_db() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None


async def database_health() -> dict[str, str]:
    try:
        if _pool:
            async with _pool.acquire() as connection:
                await connection.fetchval("SELECT 1")
        else:
            def check_sqlite() -> None:
                with _sqlite_connection() as connection:
                    connection.execute("SELECT 1").fetchone()

            await asyncio.to_thread(check_sqlite)
        return {"status": "ok", "backend": _backend}
    except Exception:
        return {"status": "error", "backend": _backend}


async def create_note(
    *,
    owner_id: str,
    source_text: str,
    structured: dict[str, Any],
    structured_markdown: str,
    mode: str,
) -> dict[str, Any]:
    note_id = str(uuid.uuid4())
    share_id = secrets.token_urlsafe(9)
    title = str(structured.get("title") or "Без названия")[:240]
    payload = (
        note_id,
        share_id,
        owner_id,
        title,
        source_text,
        _json_dump(structured),
        structured_markdown,
        mode,
        False,
        False,
    )

    if _pool:
        async with _pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                INSERT INTO thought_notes (
                    id, share_id, owner_id, title, source_text,
                    structured_json, structured_markdown, mode,
                    share_enabled, share_source
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                RETURNING *
                """,
                *payload,
            )
            return _serialize_row(dict(row))

    created_at = datetime.now(timezone.utc).isoformat()

    def insert() -> dict[str, Any]:
        with _sqlite_connection() as connection:
            connection.execute(
                """
                INSERT INTO thought_notes (
                    id, share_id, owner_id, title, source_text,
                    structured_json, structured_markdown, mode,
                    share_enabled, share_source, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*payload, created_at),
            )
            row = connection.execute(
                "SELECT * FROM thought_notes WHERE id = ?", (note_id,)
            ).fetchone()
            return _serialize_row(dict(row))

    async with _sqlite_lock:
        return await asyncio.to_thread(insert)


async def get_note(note_id: str, owner_id: str) -> dict[str, Any] | None:
    if _pool:
        async with _pool.acquire() as connection:
            row = await connection.fetchrow(
                "SELECT * FROM thought_notes WHERE id = $1 AND owner_id = $2",
                note_id,
                owner_id,
            )
            return _serialize_row(dict(row)) if row else None

    def select() -> dict[str, Any] | None:
        with _sqlite_connection() as connection:
            row = connection.execute(
                "SELECT * FROM thought_notes WHERE id = ? AND owner_id = ?",
                (note_id, owner_id),
            ).fetchone()
            return _serialize_row(dict(row)) if row else None

    return await asyncio.to_thread(select)


async def get_public_note(share_id: str) -> dict[str, Any] | None:
    if _pool:
        async with _pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                SELECT * FROM thought_notes
                WHERE share_id = $1 AND share_enabled = TRUE
                """,
                share_id,
            )
            return _serialize_row(dict(row)) if row else None

    def select() -> dict[str, Any] | None:
        with _sqlite_connection() as connection:
            row = connection.execute(
                """
                SELECT * FROM thought_notes
                WHERE share_id = ? AND share_enabled = 1
                """,
                (share_id,),
            ).fetchone()
            return _serialize_row(dict(row)) if row else None

    return await asyncio.to_thread(select)


async def list_notes(owner_id: str, limit: int = 30) -> list[dict[str, Any]]:
    limit = max(1, min(limit, 100))
    if _pool:
        async with _pool.acquire() as connection:
            rows = await connection.fetch(
                """
                SELECT id, share_id, owner_id, title, mode, created_at,
                       share_enabled
                FROM thought_notes
                WHERE owner_id = $1
                ORDER BY created_at DESC
                LIMIT $2
                """,
                owner_id,
                limit,
            )
            return [_serialize_row(dict(row)) for row in rows]

    def select() -> list[dict[str, Any]]:
        with _sqlite_connection() as connection:
            rows = connection.execute(
                """
                SELECT id, share_id, owner_id, title, mode, created_at,
                       share_enabled
                FROM thought_notes
                WHERE owner_id = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (owner_id, limit),
            ).fetchall()
            return [_serialize_row(dict(row)) for row in rows]

    return await asyncio.to_thread(select)


async def delete_note(note_id: str, owner_id: str) -> bool:
    if _pool:
        async with _pool.acquire() as connection:
            result = await connection.execute(
                "DELETE FROM thought_notes WHERE id = $1 AND owner_id = $2",
                note_id,
                owner_id,
            )
            return result == "DELETE 1"

    def delete() -> bool:
        with _sqlite_connection() as connection:
            cursor = connection.execute(
                "DELETE FROM thought_notes WHERE id = ? AND owner_id = ?",
                (note_id, owner_id),
            )
            return cursor.rowcount == 1

    async with _sqlite_lock:
        return await asyncio.to_thread(delete)


async def set_note_sharing(
    note_id: str,
    owner_id: str,
    *,
    enabled: bool,
    include_source: bool,
) -> dict[str, Any] | None:
    rotated_share_id = secrets.token_urlsafe(9)
    if _pool:
        async with _pool.acquire() as connection:
            row = await connection.fetchrow(
                """
                UPDATE thought_notes
                SET share_enabled = $3,
                    share_source = $4,
                    share_id = CASE WHEN $3 THEN share_id ELSE $5 END
                WHERE id = $1 AND owner_id = $2
                RETURNING *
                """,
                note_id,
                owner_id,
                enabled,
                include_source if enabled else False,
                rotated_share_id,
            )
            return _serialize_row(dict(row)) if row else None

    def update() -> dict[str, Any] | None:
        with _sqlite_connection() as connection:
            connection.execute(
                """
                UPDATE thought_notes
                SET share_enabled = ?,
                    share_source = ?,
                    share_id = CASE WHEN ? THEN share_id ELSE ? END
                WHERE id = ? AND owner_id = ?
                """,
                (
                    int(enabled),
                    int(include_source if enabled else False),
                    int(enabled),
                    rotated_share_id,
                    note_id,
                    owner_id,
                ),
            )
            row = connection.execute(
                "SELECT * FROM thought_notes WHERE id = ? AND owner_id = ?",
                (note_id, owner_id),
            ).fetchone()
            return _serialize_row(dict(row)) if row else None

    async with _sqlite_lock:
        return await asyncio.to_thread(update)
