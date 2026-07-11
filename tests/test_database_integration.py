"""Optional PostgreSQL integration checks.

Run with TEST_DATABASE_URL set to an isolated PostgreSQL database.
"""

import os
import random
import unittest
from uuid import uuid4

from database import Database


@unittest.skipUnless(os.getenv("TEST_DATABASE_URL"), "TEST_DATABASE_URL is not set")
class DatabaseIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.owner_id = random.randint(10_000_000, 2_000_000_000)
        self.connection_id = f"test-{uuid4()}"
        self.db = Database(os.environ["TEST_DATABASE_URL"])
        await self.db.connect(self.owner_id, initially_paused=False)
        await self.db.upsert_connection(
            connection_id=self.connection_id,
            owner_user_id=self.owner_id,
            owner_chat_id=self.owner_id,
            is_enabled=True,
            can_reply=True,
            can_read_messages=True,
        )
        await self.db.upsert_contact(
            connection_id=self.connection_id,
            chat_id=456,
            telegram_user_id=456,
            username="peer",
            first_name="Peer",
            last_name=None,
        )
        await self.db.set_contact_trusted(self.connection_id, 456, True)

    async def asyncTearDown(self) -> None:
        if self.db.pool is not None:
            await self.db.pool.execute(
                "DELETE FROM business_connections WHERE connection_id = $1",
                self.connection_id,
            )
        await self.db.close()

    async def test_newer_message_invalidates_stale_reply_and_review(self) -> None:
        await self.db.begin_incoming_message(
            connection_id=self.connection_id,
            chat_id=456,
            telegram_message_id=1,
            content="first",
            is_text=True,
        )
        await self.db.begin_incoming_message(
            connection_id=self.connection_id,
            chat_id=456,
            telegram_message_id=2,
            content="second",
            is_text=True,
        )

        reserved = await self.db.reserve_auto_reply(
            owner_id=self.owner_id,
            connection_id=self.connection_id,
            chat_id=456,
            incoming_message_id=1,
            require_trusted=True,
        )
        self.assertFalse(reserved)

        stale_pending = await self.db.create_pending(
            connection_id=self.connection_id,
            chat_id=456,
            incoming_message_id=1,
            contact_name="Peer",
            incoming_text="first",
            suggested_reply="old draft",
            reason="stale",
        )
        self.assertIsNone(stale_pending)

        latest_reserved = await self.db.reserve_auto_reply(
            owner_id=self.owner_id,
            connection_id=self.connection_id,
            chat_id=456,
            incoming_message_id=2,
            require_trusted=True,
        )
        self.assertTrue(latest_reserved)
        self.assertTrue(
            await self.db.is_reply_reserved(self.connection_id, 456, 2)
        )
