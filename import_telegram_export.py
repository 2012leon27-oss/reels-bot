"""
Optional importer for Telegram Desktop chat exports (JSON or HTML).

Inserts historical messages as owner/contact roles without blocking the main bot.
Safe by default: dry-run unless --apply is passed.

CLI usage
---------

JSON export (Telegram Desktop → Export chat history → JSON)::

    python -m import_telegram_export \\
        --export path/to/result.json \\
        --database-url sqlite:///data/bot.db \\
        --owner-id 123456789 \\
        --connection-id YOUR_BUSINESS_CONNECTION_ID \\
        --chat-id PEER_CHAT_ID \\
        --dry-run

HTML export::

    python -m import_telegram_export \\
        --export path/to/messages.html \\
        --format html \\
        --database-url sqlite:///data/bot.db \\
        --owner-id 123456789 \\
        --connection-id CONN \\
        --chat-id CHAT \\
        --apply

Flags:
  --dry-run     Parse and print counts only (default).
  --apply       Insert rows (use on a backup DB first).
  --limit N     Cap messages imported per run.

This module is a stub: parsing is minimal and may not cover all export variants.
Verify output on a copy of the database before production use.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HTML_MESSAGE_RE = re.compile(
    r'<div class="message[^"]*"[^>]*>.*?</div>\s*</div>',
    re.DOTALL | re.IGNORECASE,
)


def _parse_json_export(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    messages = data.get("messages")
    if not isinstance(messages, list):
        raise ValueError("JSON export must contain a top-level 'messages' list")
    return [m for m in messages if isinstance(m, dict)]


def _role_from_json_message(msg: dict[str, Any], owner_id: int) -> str:
    from_id = msg.get("from_id")
    if isinstance(from_id, str) and from_id.startswith("user"):
        try:
            uid = int(from_id.replace("user", ""))
            if uid == owner_id:
                return "owner"
        except ValueError:
            pass
    if msg.get("type") == "service":
        return "automatic"
    return "contact"


def _text_from_json_message(msg: dict[str, Any]) -> str:
    text = msg.get("text")
    if isinstance(text, str):
        return text.strip()
    if isinstance(text, list):
        parts: list[str] = []
        for chunk in text:
            if isinstance(chunk, str):
                parts.append(chunk)
            elif isinstance(chunk, dict) and "text" in chunk:
                parts.append(str(chunk["text"]))
        return "".join(parts).strip()
    return ""


def _parse_html_export(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    blocks = _HTML_MESSAGE_RE.findall(raw)
    parsed: list[dict[str, Any]] = []
    for block in blocks:
        if "joined" in block.lower():
            continue
        text = re.sub(r"<[^>]+>", " ", block)
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            continue
        parsed.append({"text": text, "from": "unknown"})
    return parsed


def import_messages(
    *,
    export_path: Path,
    export_format: str,
    owner_id: int,
    connection_id: str,
    chat_id: int,
    database_url: str,
    dry_run: bool = True,
    limit: int | None = None,
) -> dict[str, int]:
    """Parse export and optionally insert into DB. Returns role counts."""
    if export_format == "json":
        raw_messages = _parse_json_export(export_path)
    elif export_format == "html":
        raw_messages = _parse_html_export(export_path)
    else:
        raise ValueError(f"Unsupported format: {export_format}")

    if limit is not None:
        raw_messages = raw_messages[:limit]

    counts = {"owner": 0, "contact": 0, "automatic": 0, "skipped": 0}

    # Normalize to {role, content}
    rows: list[tuple[str, str]] = []
    for msg in raw_messages:
        if export_format == "json":
            role = _role_from_json_message(msg, owner_id)
            content = _text_from_json_message(msg)
        else:
            role = "contact"
            content = str(msg.get("text", "")).strip()
        if not content:
            counts["skipped"] += 1
            continue
        if role == "automatic":
            counts["automatic"] += 1
            continue
        counts[role] = counts.get(role, 0) + 1
        rows.append((role, content))

    logger.info(
        "Parsed %s messages from %s (owner=%s contact=%s skipped=%s)",
        len(rows),
        export_path,
        counts["owner"],
        counts["contact"],
        counts["skipped"],
    )

    if dry_run:
        logger.info("Dry run — no database writes")
        return counts

    # Lazy import: optional path, must not break main bot startup.
    try:
        from database import Database  # type: ignore[attr-defined]
    except ImportError:
        logger.error("database module not available for --apply")
        return counts

    async def _insert() -> None:
        db = Database(database_url)
        await db.connect()
        try:
            for role, content in rows:
                db_role = "owner" if role == "owner" else "peer"
                await db.insert_message(
                    connection_id=connection_id,
                    chat_id=chat_id,
                    telegram_message_id=0,
                    role=db_role,
                    content=content,
                )
        finally:
            await db.close()

    import asyncio

    asyncio.run(_insert())
    logger.info("Inserted %s rows into %s", len(rows), database_url)
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Import Telegram Desktop export")
    parser.add_argument("--export", type=Path, required=True)
    parser.add_argument("--format", choices=("json", "html"), default="json")
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--owner-id", type=int, required=True)
    parser.add_argument("--connection-id", required=True)
    parser.add_argument("--chat-id", type=int, required=True)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write to database (default: dry-run)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicit dry-run (default unless --apply)",
    )
    args = parser.parse_args()
    dry_run = not args.apply
    counts = import_messages(
        export_path=args.export,
        export_format=args.format,
        owner_id=args.owner_id,
        connection_id=args.connection_id,
        chat_id=args.chat_id,
        database_url=args.database_url,
        dry_run=dry_run,
        limit=args.limit,
    )
    print(counts)


if __name__ == "__main__":
    main()
