"""Contact profile helpers combining Store data and optional YAML files."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from store import Store

DATA_CONTACTS_DIR = Path("data/contacts")

AUTHOR_LABELS = {
    "contact": "Контакт",
    "owner": "Владелец",
    "ai_assistant": "AI",
    "other_bot": "Бот",
    "automatic": "Авто",
}


def normalize_person_name(name: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for name matching."""
    cleaned = re.sub(r"[^\w\s]", " ", name.lower(), flags=re.UNICODE)
    return " ".join(cleaned.split())


def _contact_dir(telegram_user_id: int) -> Path:
    return DATA_CONTACTS_DIR / str(telegram_user_id)


def profile_yaml_path(telegram_user_id: int) -> Path:
    return _contact_dir(telegram_user_id) / "profile.yaml"


def style_summary_path(telegram_user_id: int) -> Path:
    return _contact_dir(telegram_user_id) / "style_summary.md"


def load_contact_yaml(telegram_user_id: int) -> dict[str, Any]:
    path = profile_yaml_path(telegram_user_id)
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    return data if isinstance(data, dict) else {}


def save_contact_yaml(telegram_user_id: int, data: dict[str, Any]) -> Path:
    directory = _contact_dir(telegram_user_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = profile_yaml_path(telegram_user_id)
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(data, handle, allow_unicode=True, sort_keys=False)
    return path


def load_style_summary(telegram_user_id: int) -> str:
    path = style_summary_path(telegram_user_id)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def save_style_summary(telegram_user_id: int, content: str) -> Path:
    directory = _contact_dir(telegram_user_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = style_summary_path(telegram_user_id)
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path


async def get_contact_profile(
    store: Store,
    telegram_user_id: int,
    *,
    business_connection_id: str | None = None,
) -> dict[str, Any] | None:
    """Merge DB contact row with optional YAML profile and style summary."""
    contact = await store.get_contact_by_telegram_id(
        telegram_user_id,
        business_connection_id=business_connection_id,
    )
    yaml_profile = load_contact_yaml(telegram_user_id)
    style_summary = load_style_summary(telegram_user_id)

    if contact is None and not yaml_profile and not style_summary:
        return None

    profile: dict[str, Any] = {
        "telegram_user_id": telegram_user_id,
        "business_connection_id": business_connection_id,
    }
    if contact:
        profile.update(contact)
    if yaml_profile:
        profile["yaml_profile"] = yaml_profile
        for key, value in yaml_profile.items():
            if key not in profile or profile.get(key) in (None, ""):
                profile[key] = value
    if style_summary:
        profile["style_summary"] = style_summary
    return profile


async def get_recent_dialogue(
    store: Store,
    telegram_user_id: int,
    limit: int = 30,
    *,
    business_connection_id: str | None = None,
) -> list[str]:
    """Return labeled dialogue lines for the contact's most recent conversation."""
    contact = await store.get_contact_by_telegram_id(
        telegram_user_id,
        business_connection_id=business_connection_id,
    )
    if not contact:
        return []

    conversation = await store.get_conversation_for_contact(int(contact["id"]))
    if not conversation:
        return []

    messages = await store.get_recent_messages(int(conversation["id"]), limit)
    lines: list[str] = []
    for message in messages:
        author = AUTHOR_LABELS.get(message["author_type"], message["author_type"])
        text = (message.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{author}] {text}")
    return lines


async def get_memory_summaries(
    store: Store,
    telegram_user_id: int,
    days: int,
    *,
    business_connection_id: str | None = None,
) -> list[dict[str, Any]]:
    contact = await store.get_contact_by_telegram_id(
        telegram_user_id,
        business_connection_id=business_connection_id,
    )
    if not contact:
        return []

    conversation = await store.get_conversation_for_contact(int(contact["id"]))
    if not conversation:
        return []

    return await store.get_memory_summaries(int(conversation["id"]), days)


async def get_contact_communication_rules(
    store: Store,
    telegram_user_id: int,
    *,
    business_connection_id: str | None = None,
) -> dict[str, Any]:
    profile = await get_contact_profile(
        store,
        telegram_user_id,
        business_connection_id=business_connection_id,
    )
    if not profile:
        return {
            "communication_rules": "",
            "preferred_tone": "",
            "sensitive_topics": "",
            "ai_mode": None,
            "importance": None,
            "relationship": None,
        }

    yaml_rules = (profile.get("yaml_profile") or {}).get("communication", {})
    return {
        "communication_rules": profile.get("communication_rules")
        or yaml_rules.get("rules", ""),
        "preferred_tone": profile.get("preferred_tone")
        or yaml_rules.get("preferred_tone"),
        "sensitive_topics": profile.get("sensitive_topics")
        or yaml_rules.get("sensitive_topics", ""),
        "ai_mode": profile.get("ai_mode") or yaml_rules.get("ai_mode"),
        "importance": profile.get("importance"),
        "relationship": profile.get("relationship"),
        "manual_owner_notes": profile.get("manual_owner_notes", ""),
    }


async def seed_pending_andrey_usenko_profile(store: Store) -> dict[str, Any]:
    """
    Seed pending name binding for «Андрей Усенко» and default contact fields
    when a candidate telegram_user_id is later confirmed.
    """
    normalized = normalize_person_name("Андрей Усенко")
    existing = await store.get_pending_bindings_by_name(normalized)
    if existing:
        binding = existing[0]
    else:
        binding = await store.create_pending_name_binding(
            normalized_name=normalized,
        )

    return {
        "binding": binding,
        "normalized_name": normalized,
        "defaults": {
            "importance": "important",
            "relationship": "работа",
            "ai_mode": "urgent_only",
            "owner_alias": "Андрей Усенко",
        },
    }


async def apply_andrey_usenko_defaults(
    store: Store,
    telegram_user_id: int,
    *,
    business_connection_id: str,
) -> dict[str, Any]:
    """Apply seeded defaults once a candidate is confirmed."""
    contact = await store.get_or_create_contact(
        business_connection_id=business_connection_id,
        telegram_user_id=telegram_user_id,
    )
    seed = await seed_pending_andrey_usenko_profile(store)
    return await store.update_contact_fields(
        int(contact["id"]),
        **seed["defaults"],
    ) or contact
