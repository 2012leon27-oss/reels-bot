import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from config import ConfigurationError, Settings
from context_loader import ContextLoader
from decision import InvalidDecision, ReplyDecision
from policy import escalation_reason


VALID_ENV = {
    "BOT_TOKEN": "test-token",
    "OWNER_ID": "123456",
    "DATABASE_URL": "postgresql://localhost/test",
    "LLM_API_KEY": "test-key",
}


class SettingsTests(unittest.TestCase):
    def test_valid_minimal_configuration_is_safe_by_default(self) -> None:
        with patch.dict(os.environ, VALID_ENV, clear=True):
            settings = Settings.from_env()
        self.assertFalse(settings.auto_reply_enabled)
        self.assertTrue(settings.escalate_unknown_contacts)
        self.assertEqual(settings.owner_id, 123456)

    def test_missing_secret_fails_fast(self) -> None:
        with patch.dict(os.environ, {"OWNER_ID": "1"}, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()

    def test_invalid_confidence_threshold_fails(self) -> None:
        env = {**VALID_ENV, "CONFIDENCE_THRESHOLD": "1.5"}
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(ConfigurationError):
                Settings.from_env()


class DecisionTests(unittest.TestCase):
    def test_parses_valid_reply(self) -> None:
        decision = ReplyDecision.from_json(
            '{"action":"reply","confidence":0.94,"category":"greeting",'
            '"reply":"привет)","reason":"routine"}'
        )
        self.assertEqual(decision.reply, "привет)")
        self.assertFalse(decision.must_escalate(0.82, None))

    def test_low_confidence_escalates(self) -> None:
        decision = ReplyDecision.from_json(
            '{"action":"reply","confidence":0.4,"category":"other",'
            '"reply":"не знаю","reason":"uncertain"}'
        )
        self.assertTrue(decision.must_escalate(0.82, None))

    def test_invalid_or_oversized_reply_is_rejected(self) -> None:
        with self.assertRaises(InvalidDecision):
            ReplyDecision.from_json('{"action":"reply","confidence":"high"}')
        raw = (
            '{"action":"reply","confidence":0.9,"category":"x","reply":"'
            + ("a" * 4097)
            + '","reason":"x"}'
        )
        with self.assertRaises(InvalidDecision):
            ReplyDecision.from_json(raw)


class PolicyTests(unittest.TestCase):
    def default_reason(self, text: str = "привет, как дела?") -> str | None:
        return escalation_reason(
            text=text,
            is_text_message=True,
            runtime_paused=False,
            auto_reply_enabled=True,
            context_configured=True,
            contact_trusted=True,
            escalate_unknown_contacts=True,
        )

    def test_routine_message_can_reach_model(self) -> None:
        self.assertIsNone(self.default_reason())

    def test_money_and_meeting_are_always_escalated(self) -> None:
        self.assertIn("деньги", self.default_reason("сколько стоит и куда оплатить?"))
        self.assertIn("встреча", self.default_reason("давай назначим встречу"))

    def test_unknown_contact_and_pause_escalate(self) -> None:
        reason = escalation_reason(
            text="привет",
            is_text_message=True,
            runtime_paused=False,
            auto_reply_enabled=True,
            context_configured=True,
            contact_trusted=False,
            escalate_unknown_contacts=True,
        )
        self.assertIn("не разрешён", reason or "")

        reason = escalation_reason(
            text="привет",
            is_text_message=True,
            runtime_paused=True,
            auto_reply_enabled=True,
            context_configured=True,
            contact_trusted=True,
            escalate_unknown_contacts=True,
        )
        self.assertIn("приостановлены", reason or "")


class ContextTests(unittest.TestCase):
    def test_template_marker_keeps_autoreplies_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "owner.md").write_text("[TODO: owner]", encoding="utf-8")
            (root / "style.md").write_text("short", encoding="utf-8")
            (root / "rules.md").write_text("safe", encoding="utf-8")
            self.assertFalse(ContextLoader(root).load(42).is_configured)

    def test_contact_file_is_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ("owner.md", "style.md", "rules.md"):
                (root / name).write_text("configured", encoding="utf-8")
            (root / "contacts").mkdir()
            (root / "contacts" / "42.md").write_text("friend", encoding="utf-8")
            context = ContextLoader(root).load(42)
            self.assertTrue(context.is_configured)
            self.assertEqual(context.contact_file, "friend")


if __name__ == "__main__":
    unittest.main()
