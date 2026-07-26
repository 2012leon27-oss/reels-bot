import tempfile
import unittest
from pathlib import Path

import database


class DatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.original_url = database.settings.database_url
        self.original_path = database.settings.sqlite_path
        object.__setattr__(database.settings, "database_url", "")
        object.__setattr__(
            database.settings,
            "sqlite_path",
            str(Path(self.tempdir.name) / "notes.db"),
        )
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        object.__setattr__(database.settings, "database_url", self.original_url)
        object.__setattr__(database.settings, "sqlite_path", self.original_path)
        self.tempdir.cleanup()

    async def test_note_is_private_until_sharing_is_enabled(self):
        note = await database.create_note(
            owner_id="test:owner",
            source_text="Полный исходник",
            structured={
                "title": "Тест",
                "summary": "Суть",
                "structured_text": "## План\nСделать тест.",
                "decisions": [],
                "actions": [],
                "ideas": [],
                "open_questions": [],
                "tags": [],
            },
            structured_markdown="# Тест",
            mode="auto",
        )

        self.assertFalse(note["share_enabled"])
        self.assertIsNone(await database.get_public_note(note["share_id"]))

        shared = await database.set_note_sharing(
            note["id"],
            "test:owner",
            enabled=True,
            include_source=False,
        )

        self.assertTrue(shared["share_enabled"])
        self.assertFalse(shared["share_source"])
        self.assertIsNotNone(await database.get_public_note(note["share_id"]))

        await database.set_note_sharing(
            note["id"],
            "test:owner",
            enabled=False,
            include_source=False,
        )
        self.assertIsNone(await database.get_public_note(note["share_id"]))


if __name__ == "__main__":
    unittest.main()
