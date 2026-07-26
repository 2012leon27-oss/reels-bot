"""Save structured notes as local Markdown files."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

from config import settings

ROOT = Path(__file__).resolve().parent


def notes_dir_path() -> Path:
    path = Path(settings.notes_output_dir)
    if not path.is_absolute():
        path = ROOT / path
    path.mkdir(parents=True, exist_ok=True)
    return path


def _notes_dir() -> Path:
    return notes_dir_path()


def _safe_name(title: str) -> str:
    value = re.sub(r"[^\w\s-]", "", title, flags=re.UNICODE).strip().lower()
    value = re.sub(r"[-\s]+", "-", value).strip("-")
    return (value[:60] or "note")


def export_note(note: dict) -> dict[str, str]:
    """Write note markdown and refresh notes/index.md."""
    folder = _notes_dir()
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M")
    filename = f"{stamp}_{_safe_name(str(note.get("title", "note")))}.md"
    path = folder / filename
    path.write_text(note["structured_markdown"], encoding="utf-8")

    index_lines = [
        "# Заметки",
        "",
        f"Папка: `{folder}`",
        "",
    ]
    for item in sorted(folder.glob("*.md"), key=lambda p: p.name, reverse=True):
        if item.name == "index.md":
            continue
        index_lines.append(f"- [{item.stem}]({item.name})")
    index_lines.append("")
    (folder / "index.md").write_text("\n".join(index_lines), encoding="utf-8")

    return {
        "file_path": str(path),
        "file_name": filename,
        "folder_path": str(folder),
    }
