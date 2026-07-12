"""Async persistence layer for the neuroagent (PostgreSQL or SQLite)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal, TYPE_CHECKING

from db_schema import SCHEMA_STATEMENTS_POSTGRES, SCHEMA_STATEMENTS_SQLITE

if TYPE_CHECKING:
    import asyncpg

Backend = Literal["postgresql", "sqlite"]

CONTACT_UPDATABLE_FIELDS = frozenset({
    "username",
    "first_name",
    "last_name",
    "owner_alias",
    "importance",
    "relationship",
    "ai_mode",
    "known_facts",
    "communication_rules",
    "preferred_tone",
    "sensitive_topics",
    "manual_owner_notes",
})


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_sqlite_url(database_url: str) -> Path:
    if database_url.startswith("sqlite:///"):
        raw = database_url[len("sqlite:///") :]
    elif database_url.startswith("sqlite://"):
        raw = database_url[len("sqlite://") :]
    elif database_url.startswith("sqlite:"):
        raw = database_url[len("sqlite:") :]
    else:
        raise ValueError(f"Unsupported SQLite URL: {database_url}")
    return Path(raw)


def detect_backend(database_url: str) -> Backend:
    lowered = database_url.strip().lower()
    if lowered.startswith("sqlite"):
        return "sqlite"
    if lowered.startswith("postgresql://") or lowered.startswith("postgres://"):
        return "postgresql"
    raise ValueError(
        "DATABASE_URL must start with sqlite: or postgresql:// (legacy bot uses database.py)"
    )


class Store:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url.strip()
        self.backend = detect_backend(self.database_url)
        self.pool: Any | None = None
        self._sqlite_conn: Any | None = None
        self._sqlite_path: Path | None = None

    async def connect(self) -> None:
        if self.backend == "postgresql":
            import asyncpg

            self.pool = await asyncpg.create_pool(
                self.database_url,
                min_size=1,
                max_size=10,
                command_timeout=30,
            )
            async with self.pool.acquire() as connection:
                async with connection.transaction():
                    for statement in SCHEMA_STATEMENTS_POSTGRES:
                        await connection.execute(statement)
            return

        import aiosqlite

        self._sqlite_path = _parse_sqlite_url(self.database_url)
        self._sqlite_path.parent.mkdir(parents=True, exist_ok=True)
        self._sqlite_conn = await aiosqlite.connect(self._sqlite_path)
        self._sqlite_conn.row_factory = aiosqlite.Row
        await self._sqlite_conn.execute("PRAGMA journal_mode=WAL")
        await self._sqlite_conn.execute("PRAGMA foreign_keys=ON")
        for statement in SCHEMA_STATEMENTS_SQLITE:
            await self._sqlite_conn.execute(statement)
        await self._sqlite_conn.commit()

    async def close(self) -> None:
        if self.pool is not None:
            await self.pool.close()
            self.pool = None
        if self._sqlite_conn is not None:
            await self._sqlite_conn.close()
            self._sqlite_conn = None

    async def ping(self) -> bool:
        try:
            if self.backend == "postgresql":
                return bool(await self._pg_fetchval("SELECT TRUE"))
            return bool(await self._sqlite_fetchval("SELECT 1"))
        except Exception:
            return False

    def _pg_pool(self) -> "asyncpg.Pool":
        if self.pool is None:
            raise RuntimeError("Store has not been initialized")
        return self.pool

    def _sqlite(self) -> Any:
        if self._sqlite_conn is None:
            raise RuntimeError("Store has not been initialized")
        return self._sqlite_conn

    async def _pg_execute(self, query: str, *args: Any) -> str:
        return await self._pg_pool().execute(query, *args)

    async def _pg_fetchrow(self, query: str, *args: Any) -> Any:
        return await self._pg_pool().fetchrow(query, *args)

    async def _pg_fetch(self, query: str, *args: Any) -> list[Any]:
        return await self._pg_pool().fetch(query, *args)

    async def _pg_fetchval(self, query: str, *args: Any) -> Any:
        return await self._pg_pool().fetchval(query, *args)

    async def _sqlite_execute(self, query: str, *args: Any) -> None:
        await self._sqlite().execute(query, args)
        await self._sqlite().commit()

    async def _sqlite_fetchrow(self, query: str, *args: Any) -> dict[str, Any] | None:
        cursor = await self._sqlite().execute(query, args)
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def _sqlite_fetch(self, query: str, *args: Any) -> list[dict[str, Any]]:
        cursor = await self._sqlite().execute(query, args)
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]

    async def _sqlite_fetchval(self, query: str, *args: Any) -> Any:
        cursor = await self._sqlite().execute(query, args)
        row = await cursor.fetchone()
        return row[0] if row else None

    # --- business connections ---

    async def upsert_business_connection(
        self,
        *,
        business_connection_id: str,
        owner_telegram_id: int,
        enabled: bool = True,
        rights_json: dict[str, Any] | str | None = None,
    ) -> dict[str, Any]:
        rights = (
            rights_json
            if isinstance(rights_json, str)
            else json.dumps(rights_json or {}, ensure_ascii=False)
        )
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                INSERT INTO business_connections (
                    business_connection_id, owner_telegram_id, enabled, rights_json
                )
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (business_connection_id) DO UPDATE SET
                    owner_telegram_id = EXCLUDED.owner_telegram_id,
                    enabled = EXCLUDED.enabled,
                    rights_json = EXCLUDED.rights_json,
                    updated_at = NOW()
                RETURNING *
                """,
                business_connection_id,
                owner_telegram_id,
                enabled,
                rights,
            )
            return dict(row)

        await self._sqlite_execute(
            """
            INSERT INTO business_connections (
                business_connection_id, owner_telegram_id, enabled, rights_json
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT (business_connection_id) DO UPDATE SET
                owner_telegram_id = excluded.owner_telegram_id,
                enabled = excluded.enabled,
                rights_json = excluded.rights_json,
                updated_at = datetime('now')
            """,
            business_connection_id,
            int(owner_telegram_id),
            1 if enabled else 0,
            rights,
        )
        row = await self._sqlite_fetchrow(
            "SELECT * FROM business_connections WHERE business_connection_id = ?",
            business_connection_id,
        )
        return row or {}

    async def get_business_connection(
        self, business_connection_id: str
    ) -> dict[str, Any] | None:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                "SELECT * FROM business_connections WHERE business_connection_id = $1",
                business_connection_id,
            )
            return dict(row) if row else None
        return await self._sqlite_fetchrow(
            "SELECT * FROM business_connections WHERE business_connection_id = ?",
            business_connection_id,
        )

    # --- contacts ---

    async def get_or_create_contact(
        self,
        *,
        business_connection_id: str,
        telegram_user_id: int,
        username: str | None = None,
        first_name: str | None = None,
        last_name: str | None = None,
    ) -> dict[str, Any]:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                INSERT INTO contacts (
                    business_connection_id, telegram_user_id,
                    username, first_name, last_name
                )
                VALUES ($1, $2, $3, $4, $5)
                ON CONFLICT (business_connection_id, telegram_user_id) DO UPDATE SET
                    username = COALESCE(EXCLUDED.username, contacts.username),
                    first_name = COALESCE(EXCLUDED.first_name, contacts.first_name),
                    last_name = COALESCE(EXCLUDED.last_name, contacts.last_name),
                    updated_at = NOW()
                RETURNING *
                """,
                business_connection_id,
                telegram_user_id,
                username,
                first_name,
                last_name,
            )
            return dict(row)

        await self._sqlite_execute(
            """
            INSERT INTO contacts (
                business_connection_id, telegram_user_id,
                username, first_name, last_name
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (business_connection_id, telegram_user_id) DO UPDATE SET
                username = COALESCE(excluded.username, contacts.username),
                first_name = COALESCE(excluded.first_name, contacts.first_name),
                last_name = COALESCE(excluded.last_name, contacts.last_name),
                updated_at = datetime('now')
            """,
            business_connection_id,
            int(telegram_user_id),
            username,
            first_name,
            last_name,
        )
        row = await self._sqlite_fetchrow(
            """
            SELECT * FROM contacts
            WHERE business_connection_id = ? AND telegram_user_id = ?
            """,
            business_connection_id,
            int(telegram_user_id),
        )
        return row or {}

    async def update_contact_fields(
        self,
        contact_id: int,
        **fields: Any,
    ) -> dict[str, Any] | None:
        unknown = set(fields) - CONTACT_UPDATABLE_FIELDS
        if unknown:
            raise ValueError(f"Unknown contact fields: {sorted(unknown)}")
        if not fields:
            return await self.get_contact_by_id(contact_id)

        assignments_pg: list[str] = []
        assignments_sqlite: list[str] = []
        values: list[Any] = []
        for key, value in fields.items():
            idx = len(values) + 1
            assignments_pg.append(f"{key} = ${idx}")
            assignments_sqlite.append(f"{key} = ?")
            values.append(value)

        if self.backend == "postgresql":
            query = (
                f"UPDATE contacts SET {', '.join(assignments_pg)}, updated_at = NOW() "
                f"WHERE id = ${len(values) + 1} RETURNING *"
            )
            row = await self._pg_fetchrow(query, *values, contact_id)
            return dict(row) if row else None

        query = (
            f"UPDATE contacts SET {', '.join(assignments_sqlite)}, "
            f"updated_at = datetime('now') WHERE id = ?"
        )
        await self._sqlite_execute(query, *values, contact_id)
        return await self.get_contact_by_id(contact_id)

    async def get_contact_by_id(self, contact_id: int) -> dict[str, Any] | None:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                "SELECT * FROM contacts WHERE id = $1", contact_id
            )
            return dict(row) if row else None
        return await self._sqlite_fetchrow(
            "SELECT * FROM contacts WHERE id = ?", contact_id
        )

    async def get_contact_by_telegram_id(
        self,
        telegram_user_id: int,
        *,
        business_connection_id: str | None = None,
    ) -> dict[str, Any] | None:
        if business_connection_id is not None:
            if self.backend == "postgresql":
                row = await self._pg_fetchrow(
                    """
                    SELECT * FROM contacts
                    WHERE business_connection_id = $1 AND telegram_user_id = $2
                    """,
                    business_connection_id,
                    telegram_user_id,
                )
                return dict(row) if row else None
            return await self._sqlite_fetchrow(
                """
                SELECT * FROM contacts
                WHERE business_connection_id = ? AND telegram_user_id = ?
                """,
                business_connection_id,
                int(telegram_user_id),
            )

        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                SELECT * FROM contacts
                WHERE telegram_user_id = $1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                telegram_user_id,
            )
            return dict(row) if row else None
        return await self._sqlite_fetchrow(
            """
            SELECT * FROM contacts
            WHERE telegram_user_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            int(telegram_user_id),
        )

    async def get_contact_profile(
        self,
        telegram_user_id: int,
        *,
        business_connection_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Return DB contact row (YAML merge lives in profiles.py)."""
        return await self.get_contact_by_telegram_id(
            telegram_user_id,
            business_connection_id=business_connection_id,
        )

    # --- conversations ---

    async def get_or_create_conversation(
        self,
        *,
        business_connection_id: str,
        telegram_chat_id: int,
        contact_id: int,
    ) -> dict[str, Any]:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                INSERT INTO conversations (
                    contact_id, telegram_chat_id, business_connection_id
                )
                VALUES ($1, $2, $3)
                ON CONFLICT (business_connection_id, telegram_chat_id) DO UPDATE SET
                    contact_id = EXCLUDED.contact_id,
                    updated_at = NOW()
                RETURNING *
                """,
                contact_id,
                telegram_chat_id,
                business_connection_id,
            )
            return dict(row)

        await self._sqlite_execute(
            """
            INSERT INTO conversations (
                contact_id, telegram_chat_id, business_connection_id
            )
            VALUES (?, ?, ?)
            ON CONFLICT (business_connection_id, telegram_chat_id) DO UPDATE SET
                contact_id = excluded.contact_id,
                updated_at = datetime('now')
            """,
            int(contact_id),
            int(telegram_chat_id),
            business_connection_id,
        )
        row = await self._sqlite_fetchrow(
            """
            SELECT * FROM conversations
            WHERE business_connection_id = ? AND telegram_chat_id = ?
            """,
            business_connection_id,
            int(telegram_chat_id),
        )
        return row or {}

    async def get_conversation_by_id(
        self, conversation_id: int
    ) -> dict[str, Any] | None:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                "SELECT * FROM conversations WHERE id = $1", conversation_id
            )
            return dict(row) if row else None
        return await self._sqlite_fetchrow(
            "SELECT * FROM conversations WHERE id = ?", conversation_id
        )

    async def get_conversation_for_contact(
        self, contact_id: int
    ) -> dict[str, Any] | None:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                SELECT * FROM conversations
                WHERE contact_id = $1
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                contact_id,
            )
            return dict(row) if row else None
        return await self._sqlite_fetchrow(
            """
            SELECT * FROM conversations
            WHERE contact_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            contact_id,
        )

    async def set_conversation_mode(
        self,
        conversation_id: int,
        mode: Literal["ai", "human"],
        *,
        handoff_reason: str | None = None,
    ) -> dict[str, Any] | None:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                UPDATE conversations
                SET mode = $2,
                    handoff_reason = COALESCE($3, handoff_reason),
                    handoff_at = CASE
                        WHEN $2 = 'human' THEN COALESCE(handoff_at, NOW())
                        ELSE handoff_at
                    END,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                conversation_id,
                mode,
                handoff_reason,
            )
            return dict(row) if row else None

        await self._sqlite_execute(
            """
            UPDATE conversations
            SET mode = ?,
                handoff_reason = COALESCE(?, handoff_reason),
                handoff_at = CASE
                    WHEN ? = 'human' THEN COALESCE(handoff_at, datetime('now'))
                    ELSE handoff_at
                END,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            mode,
            handoff_reason,
            mode,
            conversation_id,
        )
        return await self.get_conversation_by_id(conversation_id)

    async def set_syntx_chat_url(
        self,
        conversation_id: int,
        syntx_chat_url: str,
        *,
        reset_message_count: bool = True,
    ) -> dict[str, Any] | None:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                UPDATE conversations
                SET syntx_chat_url = $2,
                    syntx_chat_created_at = NOW(),
                    syntx_message_count = CASE WHEN $3 THEN 0 ELSE syntx_message_count END,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                conversation_id,
                syntx_chat_url,
                reset_message_count,
            )
            return dict(row) if row else None

        await self._sqlite_execute(
            """
            UPDATE conversations
            SET syntx_chat_url = ?,
                syntx_chat_created_at = datetime('now'),
                syntx_message_count = CASE WHEN ? THEN 0 ELSE syntx_message_count END,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            syntx_chat_url,
            1 if reset_message_count else 0,
            conversation_id,
        )
        return await self.get_conversation_by_id(conversation_id)

    async def increment_syntx_message_count(
        self, conversation_id: int, *, by: int = 1
    ) -> dict[str, Any] | None:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                UPDATE conversations
                SET syntx_message_count = syntx_message_count + $2,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                conversation_id,
                by,
            )
            return dict(row) if row else None

        await self._sqlite_execute(
            """
            UPDATE conversations
            SET syntx_message_count = syntx_message_count + ?,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            by,
            conversation_id,
        )
        return await self.get_conversation_by_id(conversation_id)

    async def clear_syntx_chat(self, conversation_id: int) -> dict[str, Any] | None:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                UPDATE conversations
                SET syntx_chat_url = NULL,
                    syntx_chat_created_at = NULL,
                    syntx_message_count = 0,
                    updated_at = NOW()
                WHERE id = $1
                RETURNING *
                """,
                conversation_id,
            )
            return dict(row) if row else None

        await self._sqlite_execute(
            """
            UPDATE conversations
            SET syntx_chat_url = NULL,
                syntx_chat_created_at = NULL,
                syntx_message_count = 0,
                updated_at = datetime('now')
            WHERE id = ?
            """,
            conversation_id,
        )
        return await self.get_conversation_by_id(conversation_id)

    # --- messages ---

    async def save_message(
        self,
        *,
        conversation_id: int,
        telegram_message_id: int,
        author_type: str,
        text: str,
        created_at: datetime | None = None,
    ) -> dict[str, Any]:
        if self.backend == "postgresql":
            if created_at is None:
                row = await self._pg_fetchrow(
                    """
                    INSERT INTO messages (
                        conversation_id, telegram_message_id, author_type, text
                    )
                    VALUES ($1, $2, $3, $4)
                    ON CONFLICT (conversation_id, telegram_message_id) DO UPDATE SET
                        author_type = EXCLUDED.author_type,
                        text = EXCLUDED.text
                    RETURNING *
                    """,
                    conversation_id,
                    telegram_message_id,
                    author_type,
                    text,
                )
            else:
                row = await self._pg_fetchrow(
                    """
                    INSERT INTO messages (
                        conversation_id, telegram_message_id, author_type, text, created_at
                    )
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (conversation_id, telegram_message_id) DO UPDATE SET
                        author_type = EXCLUDED.author_type,
                        text = EXCLUDED.text
                    RETURNING *
                    """,
                    conversation_id,
                    telegram_message_id,
                    author_type,
                    text,
                    created_at,
                )
            return dict(row)

        await self._sqlite_execute(
            """
            INSERT INTO messages (
                conversation_id, telegram_message_id, author_type, text
            )
            VALUES (?, ?, ?, ?)
            ON CONFLICT (conversation_id, telegram_message_id) DO UPDATE SET
                author_type = excluded.author_type,
                text = excluded.text
            """,
            conversation_id,
            telegram_message_id,
            author_type,
            text,
        )
        row = await self._sqlite_fetchrow(
            """
            SELECT * FROM messages
            WHERE conversation_id = ? AND telegram_message_id = ?
            """,
            conversation_id,
            telegram_message_id,
        )
        return row or {}

    async def get_recent_messages(
        self, conversation_id: int, limit: int = 30
    ) -> list[dict[str, Any]]:
        if self.backend == "postgresql":
            rows = await self._pg_fetch(
                """
                SELECT * FROM messages
                WHERE conversation_id = $1 AND deleted_at IS NULL
                ORDER BY created_at DESC
                LIMIT $2
                """,
                conversation_id,
                limit,
            )
            return [dict(row) for row in reversed(rows)]

        rows = await self._sqlite_fetch(
            """
            SELECT * FROM messages
            WHERE conversation_id = ? AND deleted_at IS NULL
            ORDER BY created_at DESC
            LIMIT ?
            """,
            conversation_id,
            limit,
        )
        return list(reversed(rows))

    async def mark_message_edited(
        self, conversation_id: int, telegram_message_id: int, *, text: str
    ) -> bool:
        if self.backend == "postgresql":
            result = await self._pg_execute(
                """
                UPDATE messages
                SET text = $3, edited_at = NOW()
                WHERE conversation_id = $1 AND telegram_message_id = $2
                """,
                conversation_id,
                telegram_message_id,
                text,
            )
            return result.endswith("1")

        await self._sqlite_execute(
            """
            UPDATE messages
            SET text = ?, edited_at = datetime('now')
            WHERE conversation_id = ? AND telegram_message_id = ?
            """,
            text,
            conversation_id,
            telegram_message_id,
        )
        row = await self._sqlite_fetchrow(
            """
            SELECT changes() AS c
            """,
        )
        return bool(row and row.get("c"))

    async def mark_message_deleted(
        self, conversation_id: int, telegram_message_id: int
    ) -> bool:
        if self.backend == "postgresql":
            result = await self._pg_execute(
                """
                UPDATE messages
                SET deleted_at = NOW()
                WHERE conversation_id = $1 AND telegram_message_id = $2
                  AND deleted_at IS NULL
                """,
                conversation_id,
                telegram_message_id,
            )
            return result.endswith("1")

        await self._sqlite_execute(
            """
            UPDATE messages
            SET deleted_at = datetime('now')
            WHERE conversation_id = ? AND telegram_message_id = ?
              AND deleted_at IS NULL
            """,
            conversation_id,
            telegram_message_id,
        )
        return True

    # --- memory summaries ---

    async def save_memory_summary(
        self,
        *,
        conversation_id: int,
        period_start: datetime,
        period_end: datetime,
        summary: str,
    ) -> dict[str, Any]:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                INSERT INTO memory_summaries (
                    conversation_id, period_start, period_end, summary
                )
                VALUES ($1, $2, $3, $4)
                RETURNING *
                """,
                conversation_id,
                period_start,
                period_end,
                summary,
            )
            return dict(row)

        await self._sqlite_execute(
            """
            INSERT INTO memory_summaries (
                conversation_id, period_start, period_end, summary
            )
            VALUES (?, ?, ?, ?)
            """,
            conversation_id,
            period_start.isoformat(),
            period_end.isoformat(),
            summary,
        )
        row = await self._sqlite_fetchrow(
            """
            SELECT * FROM memory_summaries
            WHERE conversation_id = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            conversation_id,
        )
        return row or {}

    async def get_memory_summaries(
        self, conversation_id: int, days: int
    ) -> list[dict[str, Any]]:
        cutoff = _utc_now() - timedelta(days=days)
        if self.backend == "postgresql":
            rows = await self._pg_fetch(
                """
                SELECT * FROM memory_summaries
                WHERE conversation_id = $1 AND period_end >= $2
                ORDER BY period_end ASC
                """,
                conversation_id,
                cutoff,
            )
            return [dict(row) for row in rows]

        rows = await self._sqlite_fetch(
            """
            SELECT * FROM memory_summaries
            WHERE conversation_id = ? AND period_end >= ?
            ORDER BY period_end ASC
            """,
            conversation_id,
            cutoff.isoformat(),
        )
        return rows

    # --- processed updates ---

    async def mark_update_processed(
        self,
        update_id: int,
        *,
        telegram_message_id: int | None = None,
    ) -> None:
        if self.backend == "postgresql":
            await self._pg_execute(
                """
                INSERT INTO processed_updates (update_id, telegram_message_id)
                VALUES ($1, $2)
                ON CONFLICT (update_id) DO NOTHING
                """,
                update_id,
                telegram_message_id,
            )
            return

        await self._sqlite_execute(
            """
            INSERT OR IGNORE INTO processed_updates (update_id, telegram_message_id)
            VALUES (?, ?)
            """,
            update_id,
            telegram_message_id,
        )

    async def is_update_processed(self, update_id: int) -> bool:
        if self.backend == "postgresql":
            return bool(
                await self._pg_fetchval(
                    "SELECT 1 FROM processed_updates WHERE update_id = $1",
                    update_id,
                )
            )
        return bool(
            await self._sqlite_fetchval(
                "SELECT 1 FROM processed_updates WHERE update_id = ?",
                update_id,
            )
        )

    # --- alerts ---

    async def create_alert(
        self,
        *,
        conversation_id: int,
        level: str,
        reason: str,
    ) -> dict[str, Any]:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                INSERT INTO alerts (conversation_id, level, reason)
                VALUES ($1, $2, $3)
                RETURNING *
                """,
                conversation_id,
                level,
                reason,
            )
            return dict(row)

        await self._sqlite_execute(
            """
            INSERT INTO alerts (conversation_id, level, reason)
            VALUES (?, ?, ?)
            """,
            conversation_id,
            level,
            reason,
        )
        row = await self._sqlite_fetchrow(
            "SELECT * FROM alerts WHERE id = last_insert_rowid()"
        )
        return row or {}

    async def acknowledge_alert(self, alert_id: int) -> bool:
        if self.backend == "postgresql":
            result = await self._pg_execute(
                """
                UPDATE alerts SET acknowledged_at = NOW()
                WHERE id = $1 AND acknowledged_at IS NULL
                """,
                alert_id,
            )
            return result.endswith("1")

        await self._sqlite_execute(
            """
            UPDATE alerts SET acknowledged_at = datetime('now')
            WHERE id = ? AND acknowledged_at IS NULL
            """,
            alert_id,
        )
        return True

    async def list_unacked_critical(self) -> list[dict[str, Any]]:
        if self.backend == "postgresql":
            rows = await self._pg_fetch(
                """
                SELECT * FROM alerts
                WHERE level = 'critical' AND acknowledged_at IS NULL
                ORDER BY created_at ASC
                """
            )
            return [dict(row) for row in rows]

        return await self._sqlite_fetch(
            """
            SELECT * FROM alerts
            WHERE level = 'critical' AND acknowledged_at IS NULL
            ORDER BY created_at ASC
            """
        )

    async def increment_alert_repeat(self, alert_id: int) -> dict[str, Any] | None:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                UPDATE alerts
                SET repeat_count = repeat_count + 1
                WHERE id = $1
                RETURNING *
                """,
                alert_id,
            )
            return dict(row) if row else None

        await self._sqlite_execute(
            "UPDATE alerts SET repeat_count = repeat_count + 1 WHERE id = ?",
            alert_id,
        )
        return await self._sqlite_fetchrow(
            "SELECT * FROM alerts WHERE id = ?", alert_id
        )

    # --- pending name bindings ---

    async def create_pending_name_binding(
        self,
        *,
        normalized_name: str,
        candidate_telegram_user_id: int | None = None,
    ) -> dict[str, Any]:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                INSERT INTO pending_name_bindings (
                    normalized_name, candidate_telegram_user_id, status
                )
                VALUES ($1, $2, 'pending')
                RETURNING *
                """,
                normalized_name,
                candidate_telegram_user_id,
            )
            return dict(row)

        await self._sqlite_execute(
            """
            INSERT INTO pending_name_bindings (
                normalized_name, candidate_telegram_user_id, status
            )
            VALUES (?, ?, 'pending')
            """,
            normalized_name,
            candidate_telegram_user_id,
        )
        row = await self._sqlite_fetchrow(
            "SELECT * FROM pending_name_bindings WHERE id = last_insert_rowid()"
        )
        return row or {}

    async def get_pending_bindings_by_name(
        self, normalized_name: str
    ) -> list[dict[str, Any]]:
        if self.backend == "postgresql":
            rows = await self._pg_fetch(
                """
                SELECT * FROM pending_name_bindings
                WHERE normalized_name = $1 AND status = 'pending'
                ORDER BY created_at ASC
                """,
                normalized_name,
            )
            return [dict(row) for row in rows]

        return await self._sqlite_fetch(
            """
            SELECT * FROM pending_name_bindings
            WHERE normalized_name = ? AND status = 'pending'
            ORDER BY created_at ASC
            """,
            normalized_name,
        )

    async def set_pending_binding_status(
        self,
        binding_id: int,
        status: Literal["pending", "confirmed", "rejected"],
    ) -> dict[str, Any] | None:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                UPDATE pending_name_bindings
                SET status = $2
                WHERE id = $1
                RETURNING *
                """,
                binding_id,
                status,
            )
            return dict(row) if row else None

        await self._sqlite_execute(
            "UPDATE pending_name_bindings SET status = ? WHERE id = ?",
            status,
            binding_id,
        )
        return await self._sqlite_fetchrow(
            "SELECT * FROM pending_name_bindings WHERE id = ?", binding_id
        )

    async def mark_pending_binding_notified(self, binding_id: int) -> None:
        if self.backend == "postgresql":
            await self._pg_execute(
                """
                UPDATE pending_name_bindings
                SET notified_at = NOW()
                WHERE id = $1
                """,
                binding_id,
            )
            return

        await self._sqlite_execute(
            """
            UPDATE pending_name_bindings
            SET notified_at = datetime('now')
            WHERE id = ?
            """,
            binding_id,
        )

    async def get_confirmed_binding_for_user(
        self, candidate_telegram_user_id: int
    ) -> dict[str, Any] | None:
        if self.backend == "postgresql":
            row = await self._pg_fetchrow(
                """
                SELECT * FROM pending_name_bindings
                WHERE candidate_telegram_user_id = $1 AND status = 'confirmed'
                ORDER BY created_at DESC
                LIMIT 1
                """,
                candidate_telegram_user_id,
            )
            return dict(row) if row else None

        return await self._sqlite_fetchrow(
            """
            SELECT * FROM pending_name_bindings
            WHERE candidate_telegram_user_id = ? AND status = 'confirmed'
            ORDER BY created_at DESC
            LIMIT 1
            """,
            int(candidate_telegram_user_id),
        )

    # --- maintenance ---

    async def prune_old_working_memory(self, retention_days: int) -> dict[str, int]:
        """Delete old messages and summaries; never touches manual_owner_notes."""
        cutoff = _utc_now() - timedelta(days=retention_days)
        deleted_messages = 0
        deleted_summaries = 0

        if self.backend == "postgresql":
            msg_result = await self._pg_execute(
                "DELETE FROM messages WHERE created_at < $1",
                cutoff,
            )
            sum_result = await self._pg_execute(
                "DELETE FROM memory_summaries WHERE period_end < $1",
                cutoff,
            )
            deleted_messages = int(re.search(r"\d+", msg_result).group()) if msg_result else 0
            deleted_summaries = int(re.search(r"\d+", sum_result).group()) if sum_result else 0
            return {
                "deleted_messages": deleted_messages,
                "deleted_summaries": deleted_summaries,
            }

        await self._sqlite_execute(
            "DELETE FROM messages WHERE created_at < ?",
            cutoff.isoformat(),
        )
        cursor = await self._sqlite().execute("SELECT changes()")
        row = await cursor.fetchone()
        deleted_messages = int(row[0]) if row else 0

        await self._sqlite_execute(
            "DELETE FROM memory_summaries WHERE period_end < ?",
            cutoff.isoformat(),
        )
        cursor = await self._sqlite().execute("SELECT changes()")
        row = await cursor.fetchone()
        deleted_summaries = int(row[0]) if row else 0

        return {
            "deleted_messages": deleted_messages,
            "deleted_summaries": deleted_summaries,
        }
