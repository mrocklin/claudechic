"""History loading and saving for Claude CLI history file."""

from __future__ import annotations

import json
import time
from pathlib import Path

HISTORY_FILE = Path.home() / ".claude" / "history.jsonl"


def append_to_history(display: str, project: Path, session_id: str) -> None:
    """Append a command to the global history file."""
    entry = {
        "display": display,
        "pastedContents": {},
        "timestamp": int(time.time() * 1000),
        "project": str(project),
        "sessionId": session_id,
    }
    try:
        with open(HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass  # Silently fail if can't write


def load_global_history(limit: int = 1000) -> list[str]:
    """Load command history from ~/.claude/history.jsonl.

    Returns deduplicated list of commands, most recent first.
    """
    if not HISTORY_FILE.exists():
        return []

    entries: list[tuple[int, str]] = []  # (timestamp, display)
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    display = entry.get("display", "").strip()
                    timestamp = entry.get("timestamp", 0)
                    if display:
                        entries.append((timestamp, display))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []

    # Sort by timestamp descending (most recent first)
    entries.sort(key=lambda x: x[0], reverse=True)

    # Deduplicate while preserving order
    seen: set[str] = set()
    result: list[str] = []
    for _, display in entries:
        if display not in seen:
            seen.add(display)
            result.append(display)
            if len(result) >= limit:
                break

    return result
