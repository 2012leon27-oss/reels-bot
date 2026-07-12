"""Schema DDL for the neuroagent Store (PostgreSQL and SQLite variants)."""

from __future__ import annotations

SCHEMA_STATEMENTS_POSTGRES: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS business_connections (
        id BIGSERIAL PRIMARY KEY,
        business_connection_id TEXT NOT NULL UNIQUE,
        owner_telegram_id BIGINT NOT NULL,
        enabled BOOLEAN NOT NULL DEFAULT TRUE,
        rights_json TEXT NOT NULL DEFAULT '{}',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS contacts (
        id BIGSERIAL PRIMARY KEY,
        business_connection_id TEXT NOT NULL
            REFERENCES business_connections(business_connection_id)
            ON DELETE CASCADE,
        telegram_user_id BIGINT NOT NULL,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        owner_alias TEXT,
        importance TEXT,
        relationship TEXT,
        ai_mode TEXT,
        known_facts TEXT NOT NULL DEFAULT '',
        communication_rules TEXT NOT NULL DEFAULT '',
        preferred_tone TEXT,
        sensitive_topics TEXT NOT NULL DEFAULT '',
        manual_owner_notes TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (business_connection_id, telegram_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id BIGSERIAL PRIMARY KEY,
        contact_id BIGINT NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        telegram_chat_id BIGINT NOT NULL,
        business_connection_id TEXT NOT NULL
            REFERENCES business_connections(business_connection_id)
            ON DELETE CASCADE,
        syntx_chat_url TEXT,
        syntx_chat_created_at TIMESTAMPTZ,
        syntx_message_count INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL DEFAULT 'ai'
            CHECK (mode IN ('ai', 'human')),
        handoff_reason TEXT,
        handoff_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        UNIQUE (business_connection_id, telegram_chat_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id BIGSERIAL PRIMARY KEY,
        conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        telegram_message_id BIGINT NOT NULL,
        author_type TEXT NOT NULL
            CHECK (author_type IN (
                'contact', 'owner', 'ai_assistant', 'other_bot', 'automatic'
            )),
        text TEXT NOT NULL DEFAULT '',
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        edited_at TIMESTAMPTZ,
        deleted_at TIMESTAMPTZ,
        UNIQUE (conversation_id, telegram_message_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
    ON messages(conversation_id, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_summaries (
        id BIGSERIAL PRIMARY KEY,
        conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        period_start TIMESTAMPTZ NOT NULL,
        period_end TIMESTAMPTZ NOT NULL,
        summary TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_summaries_conversation
    ON memory_summaries(conversation_id, period_end DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS processed_updates (
        update_id BIGINT PRIMARY KEY,
        telegram_message_id BIGINT,
        processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id BIGSERIAL PRIMARY KEY,
        conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        level TEXT NOT NULL,
        reason TEXT NOT NULL,
        acknowledged_at TIMESTAMPTZ,
        repeat_count INTEGER NOT NULL DEFAULT 1,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_alerts_unacked
    ON alerts(conversation_id, level, acknowledged_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_name_bindings (
        id BIGSERIAL PRIMARY KEY,
        normalized_name TEXT NOT NULL,
        candidate_telegram_user_id BIGINT,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'confirmed', 'rejected')),
        notified_at TIMESTAMPTZ,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pending_name_bindings_name
    ON pending_name_bindings(normalized_name, status)
    """,
)

SCHEMA_STATEMENTS_SQLITE: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS business_connections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_connection_id TEXT NOT NULL UNIQUE,
        owner_telegram_id INTEGER NOT NULL,
        enabled INTEGER NOT NULL DEFAULT 1,
        rights_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS contacts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        business_connection_id TEXT NOT NULL
            REFERENCES business_connections(business_connection_id)
            ON DELETE CASCADE,
        telegram_user_id INTEGER NOT NULL,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        owner_alias TEXT,
        importance TEXT,
        relationship TEXT,
        ai_mode TEXT,
        known_facts TEXT NOT NULL DEFAULT '',
        communication_rules TEXT NOT NULL DEFAULT '',
        preferred_tone TEXT,
        sensitive_topics TEXT NOT NULL DEFAULT '',
        manual_owner_notes TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (business_connection_id, telegram_user_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS conversations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        contact_id INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
        telegram_chat_id INTEGER NOT NULL,
        business_connection_id TEXT NOT NULL
            REFERENCES business_connections(business_connection_id)
            ON DELETE CASCADE,
        syntx_chat_url TEXT,
        syntx_chat_created_at TEXT,
        syntx_message_count INTEGER NOT NULL DEFAULT 0,
        mode TEXT NOT NULL DEFAULT 'ai'
            CHECK (mode IN ('ai', 'human')),
        handoff_reason TEXT,
        handoff_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (business_connection_id, telegram_chat_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        telegram_message_id INTEGER NOT NULL,
        author_type TEXT NOT NULL
            CHECK (author_type IN (
                'contact', 'owner', 'ai_assistant', 'other_bot', 'automatic'
            )),
        text TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        edited_at TEXT,
        deleted_at TEXT,
        UNIQUE (conversation_id, telegram_message_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_messages_conversation_created
    ON messages(conversation_id, created_at DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS memory_summaries (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        period_start TEXT NOT NULL,
        period_end TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_memory_summaries_conversation
    ON memory_summaries(conversation_id, period_end DESC)
    """,
    """
    CREATE TABLE IF NOT EXISTS processed_updates (
        update_id INTEGER PRIMARY KEY,
        telegram_message_id INTEGER,
        processed_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conversation_id INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
        level TEXT NOT NULL,
        reason TEXT NOT NULL,
        acknowledged_at TEXT,
        repeat_count INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_alerts_unacked
    ON alerts(conversation_id, level, acknowledged_at)
    """,
    """
    CREATE TABLE IF NOT EXISTS pending_name_bindings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        normalized_name TEXT NOT NULL,
        candidate_telegram_user_id INTEGER,
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'confirmed', 'rejected')),
        notified_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_pending_name_bindings_name
    ON pending_name_bindings(normalized_name, status)
    """,
)
