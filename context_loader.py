"""Loads owner and per-contact context without embedding private data in code."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


REQUIRED_CONTEXT_FILES = ("owner.md", "style.md", "rules.md")
UNCONFIGURED_MARKER = "[TODO:"


@dataclass(frozen=True, slots=True)
class ContextBundle:
    owner: str
    style: str
    rules: str
    contact_file: str
    is_configured: bool

    def as_prompt(self) -> str:
        return (
            "OWNER PROFILE:\n"
            f"{self.owner}\n\n"
            "WRITING STYLE:\n"
            f"{self.style}\n\n"
            "OWNER RULES:\n"
            f"{self.rules}\n\n"
            "OPTIONAL CONTACT FILE:\n"
            f"{self.contact_file or 'No separate file for this contact.'}"
        )


class ContextLoader:
    def __init__(self, root: Path | str = "contexts") -> None:
        self.root = Path(root)

    def load(self, chat_id: int) -> ContextBundle:
        contents: dict[str, str] = {}
        configured = True

        for filename in REQUIRED_CONTEXT_FILES:
            path = self.root / filename
            try:
                text = path.read_text(encoding="utf-8").strip()
            except FileNotFoundError:
                text = f"{UNCONFIGURED_MARKER} missing {filename}]"
            contents[filename] = text
            if not text or UNCONFIGURED_MARKER in text:
                configured = False

        contact_path = self.root / "contacts" / f"{chat_id}.md"
        try:
            contact_file = contact_path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            contact_file = ""

        return ContextBundle(
            owner=contents["owner.md"],
            style=contents["style.md"],
            rules=contents["rules.md"],
            contact_file=contact_file,
            is_configured=configured,
        )
