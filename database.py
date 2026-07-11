"""PostgreSQL persistence for Telegram Business conversations."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import asyncpg


SCHEMA_STATEMENTS: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS business_connections (
        connection_id TEXT PRIMARY KEY,
        owner_user_id BIGINT NOT NULL,
        owner_chat_id BIGINT NOT NULL,
        is_enabled BOOLEAN NOT NULL,
        can_reply BOOLEAN NOT NULL DEFAULT FALSE,
        can_read_messages BOOLEAN NOT NULL DEFAULT FALSE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS business_contacts (
        connection_id TEXT NOT NULL REFERENCES business_connections(connection_id)
            ON DELETE CASCADE,
        chat_id BIGINT NOT NULL,
        telegram_user_id BIGINT,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        notes TEXT NOT NULL DEFAULT '',
        trusted_for_auto_reply BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        PRIMARY KEY (connection_id, chat_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS business_messages (
        id BIGSERIAL PRIMARY KEY,
        connection_id TEXT NOT NULL REFERENCES business_connections(connection_id)
            ON DELETE CASCADE,
        chat_id BIGINT NOT NULL,
        telegram_message_id BIGINT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('peer', 'owner')),
        content TEXT NOT NULL,
        is_text BOOLEAN NOT NULL DEFAULT TRUE,
        is_deleted BOOLEAN NOT NULL DEFAULT FALSE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        edited_at TIMESTAMPTZ,
        processed_at TIMESTAMPTZ,
        reply_reserved_at TIMESTAMPTZ,
        UNIQUE (connection_id, chat_id, telegram_message_id)
    )
    """,
    """
    ALTER TABLE business_messages
    ADD COLUMN IF NOT EXISTS processed_at TIMESTAMPTZ
    """,
    """
    ALTER TABLE business_messages
    ADD COLUMN IF NOT EXISTS is_text BOOLEAN NOT NULL DEFAULT TRUE
    """,
    """
    ALTER TABLE business_messages
    ADD COLUMN IF NOT EXISTS reply_reserved_at TIMESTAMPTZ
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_business_messages_history
    ON business_messages(connection_id, chat_id, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_reviews (
        id BIGSERIAL PRIMARY KEY,
        connection_id TEXT NOT NULL REFERENCES business_connections(connection_id)
            ON DELETE CASCADE,
        chat_id BIGINT NOT NULL,
        incoming_message_id BIGINT NOT NULL,
        contact_name TEXT NOT NULL,
        incoming_text TEXT NOT NULL,
        suggested_reply TEXT,
        reason TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN (
                'pending', 'processing', 'sent', 'ignored',
                'owner_handled', 'send_unknown'
            )),
        notification_message_id BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        processing_started_at TIMESTAMPTZ,
        resolved_at TIMESTAMPTZ,
        UNIQUE (connection_id, chat_id, incoming_message_id)
    )
    """,
    """
    ALTER TABLE pending_reviews
    ADD COLUMN IF NOT EXISTS processing_started_at TIMESTAMPTZ
    """,
    """
    ALTER TABLE pending_reviews DROP CONSTRAINT IF EXISTS pending_reviews_status_check
    """,
    """
    ALTER TABLE pending_reviews ADD CONSTRAINT pending_reviews_status_check
    CHECK (status IN (
        'pending', 'processing', 'sent', 'ignored',
        'owner_handled', 'send_unknown'
    ))
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pending_reviews_status
    ON pending_reviews(status, created_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS owner_settings (
        owner_user_id BIGINT PRIMARY KEY,
        is_paused BOOLEAN NOT NULL DEFAULT TRUE,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
)


class Database:
    def __init__(self, database_url: str) -> None:
        self.database_url = database_url
        self.pool: asyncpg.Pool | None = None
        self._instance_lock_connection: asyncpg.Connection | None = None

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Database has not been initialized")
        return self.pool

    async def connect(self, owner_id: int, initially_paused: bool) -> None:
        self.pool = await asyncpg.create_pool(
            self.database_url,
            min_size=1,
            max_size=10,
            command_timeout=30,
        )
        async with self.pool.acquire() as connection:
            async with connection.transaction():
                for statement in SCHEMA_STATEMENTS:
                    await connection.execute(statement)
                await connection.execute(
                    """
                    INSERT INTO owner_settings (owner_user_id, is_paused)
                    VALUES ($1, $2)
                    ON CONFLICT (owner_user_id) DO NOTHING
                    """,
                    owner_id,
                    initially_paused,
                )
        self._instance_lock_connection = await self.pool.acquire()
        acquired = await self._instance_lock_connection.fetchval(
            "SELECT pg_try_advisory_lock($1)", owner_id
        )
        if not acquired:
            await self.pool.release(self._instance_lock_connection)
            self._instance_lock_connection = None
            await self.pool.close()
            self.pool = None
            raise RuntimeError(
                "Another assistant instance already holds the owner lock"
            )

    async def close(self) -> None:
        if self.pool is not None:
            if self._instance_lock_connection is not None:
                await self._instance_lock_connection.execute(
                    "SELECT pg_advisory_unlock_all()"
                )
                await self.pool.release(self._instance_lock_connection)
                self._instance_lock_connection = None
            await self.pool.close()
            self.pool = None

    async def ping(self) -> bool:
        try:
            return await self._pool().fetchval("SELECT TRUE")
        except (asyncpg.PostgresError, RuntimeError):
            return False

    async def upsert_connection(
        self,
        *,
        connection_id: str,
        owner_user_id: int,
        owner_chat_id: int,
        is_enabled: bool,
        can_reply: bool,
        can_read_messages: bool,
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO business_connections (
                connection_id, owner_user_id, owner_chat_id, is_enabled,
                can_reply, can_read_messages
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (connection_id) DO UPDATE SET
                owner_user_id = EXCLUDED.owner_user_id,
                owner_chat_id = EXCLUDED.owner_chat_id,
                is_enabled = EXCLUDED.is_enabled,
                can_reply = EXCLUDED.can_reply,
                can_read_messages = EXCLUDED.can_read_messages,
                updated_at = NOW()
            """,
            connection_id,
            owner_user_id,
            owner_chat_id,
            is_enabled,
            can_reply,
            can_read_messages,
        )

    async def get_connection(self, connection_id: str) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            "SELECT * FROM business_connections WHERE connection_id = $1",
            connection_id,
        )
        return dict(row) if row else None

    async def upsert_contact(
        self,
        *,
        connection_id: str,
        chat_id: int,
        telegram_user_id: int | None,
        username: str | None,
        first_name: str | None,
        last_name: str | None,
    ) -> dict[str, Any]:
        row = await self._pool().fetchrow(
            """
            INSERT INTO business_contacts (
                connection_id, chat_id, telegram_user_id, username,
                first_name, last_name
            )
            VALUES ($1, $2, $3, $4, $5, $6)
            ON CONFLICT (connection_id, chat_id) DO UPDATE SET
                telegram_user_id = COALESCE(EXCLUDED.telegram_user_id,
                                            business_contacts.telegram_user_id),
                username = COALESCE(EXCLUDED.username, business_contacts.username),
                first_name = COALESCE(EXCLUDED.first_name, business_contacts.first_name),
                last_name = COALESCE(EXCLUDED.last_name, business_contacts.last_name),
                updated_at = NOW()
            RETURNING *
            """,
            connection_id,
            chat_id,
            telegram_user_id,
            username,
            first_name,
            last_name,
        )
        return dict(row)

    async def set_contact_notes(
        self, connection_id: str, chat_id: int, notes: str
    ) -> bool:
        result = await self._pool().execute(
            """
            UPDATE business_contacts
            SET notes = $3, updated_at = NOW()
            WHERE connection_id = $1 AND chat_id = $2
            """,
            connection_id,
            chat_id,
            notes,
        )
        return result == "UPDATE 1"

    async def set_contact_trusted(
        self, connection_id: str, chat_id: int, trusted: bool
    ) -> bool:
        result = await self._pool().execute(
            """
            UPDATE business_contacts
            SET trusted_for_auto_reply = $3, updated_at = NOW()
            WHERE connection_id = $1 AND chat_id = $2
            """,
            connection_id,
            chat_id,
            trusted,
        )
        return result == "UPDATE 1"

    async def find_contact(
        self, owner_id: int, chat_id: int
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            SELECT c.*
            FROM business_contacts c
            JOIN business_connections b USING (connection_id)
            WHERE b.owner_user_id = $1 AND c.chat_id = $2
            ORDER BY b.updated_at DESC
            LIMIT 1
            """,
            owner_id,
            chat_id,
        )
        return dict(row) if row else None

    async def insert_message(
        self,
        *,
        connection_id: str,
        chat_id: int,
        telegram_message_id: int,
        role: str,
        content: str,
    ) -> bool:
        row = await self._pool().fetchval(
            """
            INSERT INTO business_messages (
                connection_id, chat_id, telegram_message_id, role, content
            )
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (connection_id, chat_id, telegram_message_id) DO NOTHING
            RETURNING id
            """,
            connection_id,
            chat_id,
            telegram_message_id,
            role,
            content,
        )
        if row is not None and role == "owner":
            await self._pool().execute(
                """
                UPDATE pending_reviews
                SET status = 'owner_handled', resolved_at = NOW()
                WHERE connection_id = $1 AND chat_id = $2 AND status = 'pending'
                """,
                connection_id,
                chat_id,
            )
        return row is not None

    async def begin_incoming_message(
        self,
        *,
        connection_id: str,
        chat_id: int,
        telegram_message_id: int,
        content: str,
        is_text: bool,
    ) -> bool:
        """Persist an inbox item and return False only when it is completed."""
        row = await self._pool().fetchrow(
            """
            INSERT INTO business_messages (
                connection_id, chat_id, telegram_message_id, role, content, is_text
            )
            VALUES ($1, $2, $3, 'peer', $4, $5)
            ON CONFLICT (connection_id, chat_id, telegram_message_id)
            DO UPDATE SET content = business_messages.content
            RETURNING processed_at
            """,
            connection_id,
            chat_id,
            telegram_message_id,
            content,
            is_text,
        )
        return row["processed_at"] is None

    async def list_unprocessed_incoming(
        self, owner_id: int, limit: int = 20
    ) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT
                m.connection_id,
                m.chat_id,
                m.telegram_message_id,
                m.content,
                m.is_text,
                c.telegram_user_id,
                c.username,
                c.first_name,
                c.last_name,
                c.notes,
                c.trusted_for_auto_reply
            FROM business_messages m
            JOIN business_connections b USING (connection_id)
            JOIN business_contacts c
              ON c.connection_id = m.connection_id
             AND c.chat_id = m.chat_id
            WHERE b.owner_user_id = $1
              AND m.role = 'peer'
              AND m.processed_at IS NULL
              AND NOT m.is_deleted
            ORDER BY m.created_at ASC, m.id ASC
            LIMIT $2
            """,
            owner_id,
            limit,
        )
        return [dict(row) for row in rows]

    async def mark_message_processed(
        self, connection_id: str, chat_id: int, telegram_message_id: int
    ) -> None:
        await self._pool().execute(
            """
            UPDATE business_messages
            SET processed_at = NOW()
            WHERE connection_id = $1 AND chat_id = $2 AND telegram_message_id = $3
            """,
            connection_id,
            chat_id,
            telegram_message_id,
        )

    async def reserve_auto_reply(
        self,
        *,
        owner_id: int,
        connection_id: str,
        chat_id: int,
        incoming_message_id: int,
        require_trusted: bool,
    ) -> bool:
        return (
            await self._pool().fetchval(
                """
                UPDATE business_messages incoming
                SET reply_reserved_at = NOW()
                FROM business_connections b,
                     business_contacts c,
                     owner_settings settings
                WHERE incoming.connection_id = $2
                  AND incoming.chat_id = $3
                  AND incoming.telegram_message_id = $4
                  AND incoming.role = 'peer'
                  AND incoming.reply_reserved_at IS NULL
                  AND NOT incoming.is_deleted
                  AND incoming.edited_at IS NULL
                  AND b.connection_id = incoming.connection_id
                  AND b.owner_user_id = $1
                  AND b.is_enabled
                  AND b.can_reply
                  AND c.connection_id = b.connection_id
                  AND c.chat_id = incoming.chat_id
                  AND (NOT $5::BOOLEAN OR c.trusted_for_auto_reply)
                  AND settings.owner_user_id = b.owner_user_id
                  AND NOT settings.is_paused
                  AND NOT EXISTS (
                      SELECT 1
                      FROM business_messages newer
                      WHERE newer.connection_id = incoming.connection_id
                        AND newer.chat_id = incoming.chat_id
                        AND NOT newer.is_deleted
                        AND newer.id > incoming.id
                  )
                  AND NOT EXISTS (
                      SELECT 1
                      FROM pending_reviews pending
                      WHERE pending.connection_id = incoming.connection_id
                        AND pending.chat_id = incoming.chat_id
                        AND pending.status IN ('pending', 'processing')
                  )
                RETURNING incoming.id
                """,
                owner_id,
                connection_id,
                chat_id,
                incoming_message_id,
                require_trusted,
            )
            is not None
        )

    async def edit_message(
        self,
        connection_id: str,
        chat_id: int,
        telegram_message_id: int,
        content: str,
    ) -> None:
        await self._pool().execute(
            """
            UPDATE business_messages
            SET content = $4, edited_at = NOW()
            WHERE connection_id = $1 AND chat_id = $2 AND telegram_message_id = $3
            """,
            connection_id,
            chat_id,
            telegram_message_id,
            content,
        )
        await self._pool().execute(
            """
            UPDATE pending_reviews
            SET incoming_text = $4,
                suggested_reply = NULL,
                reason = CASE
                    WHEN reason LIKE '%сообщение отредактировано%' THEN reason
                    ELSE reason || '; сообщение отредактировано'
                END
            WHERE connection_id = $1 AND chat_id = $2
              AND incoming_message_id = $3 AND status = 'pending'
            """,
            connection_id,
            chat_id,
            telegram_message_id,
            content,
        )

    async def mark_messages_deleted(
        self, connection_id: str, chat_id: int, message_ids: Sequence[int]
    ) -> None:
        await self._pool().execute(
            """
            INSERT INTO business_messages (
                connection_id, chat_id, telegram_message_id, role, content,
                is_text, is_deleted, processed_at
            )
            SELECT $1, $2, message_id, 'peer', '[deleted]', FALSE, TRUE, NOW()
            FROM UNNEST($3::BIGINT[]) AS message_id
            ON CONFLICT (connection_id, chat_id, telegram_message_id)
            DO UPDATE SET is_deleted = TRUE, processed_at = NOW()
            """,
            connection_id,
            chat_id,
            list(message_ids),
        )
        await self._pool().execute(
            """
            UPDATE pending_reviews
            SET status = 'ignored',
                suggested_reply = NULL,
                reason = reason || '; исходное сообщение удалено',
                resolved_at = NOW()
            WHERE connection_id = $1 AND chat_id = $2
              AND incoming_message_id = ANY($3::BIGINT[])
              AND status = 'pending'
            """,
            connection_id,
            chat_id,
            list(message_ids),
        )

    async def get_history(
        self,
        *,
        connection_id: str,
        chat_id: int,
        limit: int,
        exclude_message_id: int | None = None,
    ) -> list[dict[str, str]]:
        rows = await self._pool().fetch(
            """
            SELECT role, content
            FROM business_messages
            WHERE connection_id = $1 AND chat_id = $2 AND NOT is_deleted
              AND ($4::BIGINT IS NULL OR telegram_message_id <> $4)
            ORDER BY created_at DESC, id DESC
            LIMIT $3
            """,
            connection_id,
            chat_id,
            limit,
            exclude_message_id,
        )
        return [
            {
                "role": "assistant" if row["role"] == "owner" else "user",
                "content": row["content"],
            }
            for row in reversed(rows)
        ]

    async def is_paused(self, owner_id: int) -> bool:
        value = await self._pool().fetchval(
            "SELECT is_paused FROM owner_settings WHERE owner_user_id = $1",
            owner_id,
        )
        return True if value is None else bool(value)

    async def set_paused(self, owner_id: int, paused: bool) -> None:
        await self._pool().execute(
            """
            INSERT INTO owner_settings (owner_user_id, is_paused)
            VALUES ($1, $2)
            ON CONFLICT (owner_user_id) DO UPDATE SET
                is_paused = EXCLUDED.is_paused,
                updated_at = NOW()
            """,
            owner_id,
            paused,
        )

    async def create_pending(
        self,
        *,
        connection_id: str,
        chat_id: int,
        incoming_message_id: int,
        contact_name: str,
        incoming_text: str,
        suggested_reply: str | None,
        reason: str,
    ) -> tuple[dict[str, Any], bool] | None:
        row = await self._pool().fetchrow(
            """
            INSERT INTO pending_reviews (
                connection_id, chat_id, incoming_message_id, contact_name,
                incoming_text, suggested_reply, reason
            )
            SELECT $1, $2, $3, $4, $5, $6, $7
            FROM business_messages current
            WHERE current.connection_id = $1
              AND current.chat_id = $2
              AND current.telegram_message_id = $3
              AND current.role = 'peer'
              AND current.content = $5
              AND NOT current.is_deleted
              AND NOT EXISTS (
                  SELECT 1 FROM business_messages newer
                  WHERE newer.connection_id = $1
                    AND newer.chat_id = $2
                    AND newer.role = 'owner'
                    AND newer.id > current.id
              )
            ON CONFLICT (connection_id, chat_id, incoming_message_id) DO NOTHING
            RETURNING *
            """,
            connection_id,
            chat_id,
            incoming_message_id,
            contact_name,
            incoming_text,
            suggested_reply,
            reason,
        )
        if row:
            return dict(row), True
        existing = await self._pool().fetchrow(
            """
            SELECT * FROM pending_reviews
            WHERE connection_id = $1 AND chat_id = $2 AND incoming_message_id = $3
            """,
            connection_id,
            chat_id,
            incoming_message_id,
        )
        if existing is None:
            return None
        return dict(existing), False

    async def set_pending_notification(
        self, pending_id: int, notification_message_id: int
    ) -> None:
        await self._pool().execute(
            """
            UPDATE pending_reviews SET notification_message_id = $2
            WHERE id = $1 AND status = 'pending'
            """,
            pending_id,
            notification_message_id,
        )

    async def get_pending(
        self, pending_id: int, owner_id: int
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            SELECT p.*
            FROM pending_reviews p
            JOIN business_connections b USING (connection_id)
            WHERE p.id = $1 AND b.owner_user_id = $2
            """,
            pending_id,
            owner_id,
        )
        return dict(row) if row else None

    async def get_pending_by_notification(
        self, notification_message_id: int, owner_id: int
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            SELECT p.*
            FROM pending_reviews p
            JOIN business_connections b USING (connection_id)
            WHERE p.notification_message_id = $1
              AND b.owner_user_id = $2
              AND p.status = 'pending'
            """,
            notification_message_id,
            owner_id,
        )
        return dict(row) if row else None

    async def list_pending(
        self, owner_id: int, limit: int = 10, only_unnotified: bool = False
    ) -> list[dict[str, Any]]:
        rows = await self._pool().fetch(
            """
            SELECT p.*
            FROM pending_reviews p
            JOIN business_connections b USING (connection_id)
            WHERE b.owner_user_id = $1
              AND p.status = 'pending'
              AND (NOT $3::BOOLEAN OR p.notification_message_id IS NULL)
            ORDER BY p.created_at ASC
            LIMIT $2
            """,
            owner_id,
            limit,
            only_unnotified,
        )
        return [dict(row) for row in rows]

    async def claim_pending(
        self, pending_id: int, owner_id: int
    ) -> dict[str, Any] | None:
        row = await self._pool().fetchrow(
            """
            UPDATE pending_reviews p
            SET status = 'processing', processing_started_at = NOW()
            FROM business_connections b, business_messages incoming
            WHERE p.id = $1
              AND p.status = 'pending'
              AND b.connection_id = p.connection_id
              AND b.owner_user_id = $2
              AND b.is_enabled
              AND b.can_reply
              AND incoming.connection_id = p.connection_id
              AND incoming.chat_id = p.chat_id
              AND incoming.telegram_message_id = p.incoming_message_id
              AND incoming.content = p.incoming_text
              AND NOT incoming.is_deleted
              AND NOT EXISTS (
                  SELECT 1
                  FROM business_messages newer
                  WHERE newer.connection_id = p.connection_id
                    AND newer.chat_id = p.chat_id
                    AND newer.role = 'owner'
                    AND newer.id > incoming.id
              )
            RETURNING p.*
            """,
            pending_id,
            owner_id,
        )
        return dict(row) if row else None

    async def release_pending(self, pending_id: int) -> None:
        await self._pool().execute(
            """
            UPDATE pending_reviews
            SET status = 'pending', processing_started_at = NULL
            WHERE id = $1 AND status = 'processing'
            """,
            pending_id,
        )

    async def resolve_pending(
        self, pending_id: int, status: str, owner_id: int
    ) -> bool:
        if status not in {"sent", "ignored", "owner_handled", "send_unknown"}:
            raise ValueError("Invalid resolution status")
        expected_status = (
            "processing" if status in {"sent", "send_unknown"} else "pending"
        )
        result = await self._pool().execute(
            """
            UPDATE pending_reviews p
            SET status = $2, resolved_at = NOW()
            FROM business_connections b
            WHERE p.id = $1
              AND p.status = $3
              AND b.connection_id = p.connection_id
              AND b.owner_user_id = $4
            """,
            pending_id,
            status,
            expected_status,
            owner_id,
        )
        return result == "UPDATE 1"

    async def recover_stale_pending(self, owner_id: int) -> int:
        result = await self._pool().execute(
            """
            UPDATE pending_reviews p
            SET status = 'send_unknown', resolved_at = NOW()
            FROM business_connections b
            WHERE p.status = 'processing'
              AND (
                  p.processing_started_at IS NULL
                  OR p.processing_started_at < NOW() - INTERVAL '10 minutes'
              )
              AND b.connection_id = p.connection_id
              AND b.owner_user_id = $1
            """,
            owner_id,
        )
        return int(result.split()[-1])

    async def stats(self, owner_id: int) -> dict[str, int]:
        row = await self._pool().fetchrow(
            """
            SELECT
                (SELECT COUNT(*) FROM business_contacts c
                 JOIN business_connections b USING (connection_id)
                 WHERE b.owner_user_id = $1) AS contacts,
                (SELECT COUNT(*) FROM business_messages m
                 JOIN business_connections b USING (connection_id)
                 WHERE b.owner_user_id = $1) AS messages,
                (SELECT COUNT(*) FROM pending_reviews p
                 JOIN business_connections b USING (connection_id)
                 WHERE b.owner_user_id = $1 AND p.status = 'pending') AS pending,
                (SELECT COUNT(*) FROM pending_reviews p
                 JOIN business_connections b USING (connection_id)
                 WHERE b.owner_user_id = $1 AND p.status = 'sent') AS approved
            """,
            owner_id,
        )
        return {key: int(value) for key, value in dict(row).items()}
