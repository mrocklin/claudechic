"""Session management - loading and listing Claude Code sessions."""

import json
import os
import re
from datetime import datetime
from pathlib import Path

import aiofiles


def is_valid_uuid(s: str) -> bool:
    """Check if string is a valid UUID (not agent-* internal sessions)."""
    return bool(
        re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", s, re.I
        )
    )


def find_session_by_prefix(prefix: str, cwd: Path | None = None) -> str | None:
    """Find a session ID by prefix match.

    Args:
        prefix: The prefix to match (e.g., first 8 chars of UUID)
        cwd: Project directory. If None, uses current working directory.

    Returns:
        Full session ID if exactly one match, None otherwise.
    """
    # If it's already a full UUID, return as-is
    if is_valid_uuid(prefix):
        return prefix

    sessions_dir = get_project_sessions_dir(cwd)
    if not sessions_dir:
        return None

    prefix_lower = prefix.lower()
    matches = []
    for f in sessions_dir.glob("*.jsonl"):
        if f.stem.lower().startswith(prefix_lower) and is_valid_uuid(f.stem):
            matches.append(f.stem)

    return matches[0] if len(matches) == 1 else None


def count_sessions(cwd: Path | None = None) -> int:
    """Count session files in project directory."""
    sessions_dir = get_project_sessions_dir(cwd)
    if not sessions_dir:
        return 0
    return sum(1 for f in sessions_dir.glob("*.jsonl") if is_valid_uuid(f.stem))


def get_project_sessions_dir(cwd: Path | None = None) -> Path | None:
    """Get the sessions directory for a project.

    Claude stores sessions in ~/.claude/projects/-path-to-project
    with dashes instead of slashes (or backslashes on Windows).

    Args:
        cwd: Project directory. If None, uses current working directory.
    """
    cwd = (cwd or Path.cwd()).absolute()
    # Replace path separators with dashes (handles both / and \ on Windows)
    # Replace Windows drive colon with dash (C:\foo -> C--foo, matching Claude Code's format)
    project_key = str(cwd).replace(os.sep, "-").replace(":", "-").replace("_", "-").replace(".", "-")
    sessions_dir = Path.home() / ".claude/projects" / project_key
    return sessions_dir if sessions_dir.exists() else None


def _get_session_file(
    session_id: str, cwd: Path | None = None, agent_id: str | None = None
) -> Path | None:
    """Get path to session file if it exists."""
    sessions_dir = get_project_sessions_dir(cwd)
    if not sessions_dir:
        return None
    if agent_id:
        session_file = sessions_dir / f"agent-{agent_id}.jsonl"
    else:
        session_file = sessions_dir / f"{session_id}.jsonl"
    return session_file if session_file.exists() else None


def _extract_session_info(filepath: Path) -> tuple[str, int, float, str]:
    """Extract title, message count, timestamp, and last message from a session file.

    Claude Code uses summary field if available, otherwise first user message.
    Counts non-meta user entries.

    Returns (title, msg_count, last_timestamp_unix, last_msg).
    last_msg is the last meaningful user message (>10 chars), useful as a
    "where you left off" hint in the session picker.
    """
    summary = ""
    first_msg = ""
    last_msg = ""
    msg_count = 0
    last_timestamp: float = 0

    try:
        with open(filepath, encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    d = json.loads(line)
                    msg_type = d.get("type")

                    # Check for summary (preferred title source)
                    if msg_type == "summary":
                        summary = d.get("summary", "")

                    # Count and extract first/last message
                    elif msg_type == "user" and not d.get("isMeta"):
                        msg_count += 1
                        content = d.get("message", {}).get("content", "")
                        text = ""
                        if isinstance(content, str) and content.strip():
                            if not content.startswith("<command-"):
                                text = content.replace("\n", " ")[:100]
                        elif isinstance(content, list) and content:
                            block = content[0]
                            if block.get("type") == "text":
                                txt = block.get("text", "")
                                if txt.strip() and not txt.startswith("<command-"):
                                    text = txt.replace("\n", " ")[:100]
                        if text:
                            if not first_msg:
                                first_msg = text
                            # Track last meaningful message (skip short acks like "ok", "yes")
                            if len(text) > 10:
                                last_msg = text

                    # Track timestamp from any entry
                    if ts := d.get("timestamp"):
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            last_timestamp = max(last_timestamp, dt.timestamp())
                        except ValueError:
                            pass
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
    except (IOError, OSError):
        pass

    # Prefer summary over first message
    title = summary or first_msg
    return title, msg_count, last_timestamp, last_msg


async def get_recent_sessions(
    limit: int = 20, search: str = "", cwd: Path | None = None
) -> list[tuple[str, str, float, int, str]]:
    """Get recent sessions from session files (matching Claude Code behavior).

    Args:
        limit: Maximum number of sessions to return
        search: Optional text to filter sessions by title
        cwd: Project directory. If None, uses current working directory.

    Returns:
        List of (session_id, title, timestamp, msg_count) tuples,
        sorted by content timestamp descending.
    """
    sessions_dir = get_project_sessions_dir(cwd)
    if not sessions_dir:
        return []

    # Get files sorted by mtime for initial ordering
    candidates = []
    for f in sessions_dir.glob("*.jsonl"):
        if not is_valid_uuid(f.stem):
            continue
        try:
            stat = f.stat()
            if stat.st_size > 0:
                candidates.append((f, stat.st_mtime))
        except OSError:
            continue

    candidates.sort(key=lambda x: x[1], reverse=True)

    search_lower = search.lower()
    sessions = []

    # We need to scan more files than limit because file mtime may not match
    # content timestamp. Scan up to 5x limit to catch recent sessions.
    scan_limit = limit * 5

    for i, (f, mtime) in enumerate(candidates):
        if i >= scan_limit:
            break

        title, msg_count, last_ts, last_msg = _extract_session_info(f)

        if msg_count == 0:
            continue

        title = title or f.stem[:8]
        if search and search_lower not in title.lower() and search_lower not in last_msg.lower():
            continue

        # Prefer timestamp from file content over file mtime
        effective_time = last_ts or mtime
        sessions.append((f.stem, title, effective_time, msg_count, last_msg))

    # Sort by content timestamp (more accurate than file mtime)
    sessions.sort(key=lambda x: x[2], reverse=True)

    return sessions[:limit]


async def load_session_messages(session_id: str, cwd: Path | None = None) -> list[dict]:
    """Load all messages from a session file.

    Returns list of message dicts with 'type' key:
    - user: {'type': 'user', 'content': str}
    - assistant: {'type': 'assistant', 'content': str}
    - tool_use: {'type': 'tool_use', 'name': str, 'input': dict, 'id': str}
    """
    session_file = _get_session_file(session_id, cwd)
    if not session_file:
        return []

    skip_tags = ("<command-name>/", "<local-command-stdout>", "<local-command-caveat>")
    messages = []
    try:
        async with aiofiles.open(session_file, encoding="utf-8") as f:
            async for line in f:
                d = json.loads(line)
                if d.get("type") == "user":
                    content = d.get("message", {}).get("content", "")
                    if isinstance(content, str) and content.strip():
                        if content.strip().startswith("/"):
                            continue
                        if any(tag in content for tag in skip_tags):
                            continue
                        messages.append({"type": "user", "content": content})
                elif d.get("type") == "assistant":
                    msg = d.get("message", {})
                    content_blocks = msg.get("content", [])
                    for block in content_blocks:
                        if isinstance(block, dict):
                            if block.get("type") == "text":
                                text = block.get("text", "")
                                if text.strip():
                                    messages.append(
                                        {"type": "assistant", "content": text}
                                    )
                            elif block.get("type") == "tool_use":
                                messages.append(
                                    {
                                        "type": "tool_use",
                                        "name": block.get("name", "?"),
                                        "input": block.get("input", {}),
                                        "id": block.get("id", ""),
                                    }
                                )
    except (json.JSONDecodeError, IOError):
        pass

    return messages


async def get_plan_path_for_session(
    session_id: str, cwd: Path | None = None, must_exist: bool = True
) -> Path | None:
    """Get the plan file path (~/.claude/plans/{slug}.md) for a session.

    Args:
        session_id: The session ID
        cwd: Working directory for finding session file
        must_exist: If True, only return path if file exists. If False, return
                   the expected path even if file doesn't exist yet.
    """
    session_file = _get_session_file(session_id, cwd)
    if not session_file:
        return None

    # Find slug in session file (read first 32KB, slug appears early)
    slug = None
    try:
        async with aiofiles.open(session_file, mode="rb") as f:
            chunk = await f.read(32768)

        for line in chunk.split(b"\n"):
            if b'"slug"' not in line:
                continue
            try:
                data = json.loads(line)
                if "slug" in data:
                    slug = data["slug"]
                    break
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Skip lines that fail to parse (partial line at chunk boundary)
                continue
    except (IOError, OSError):
        return None

    if not slug:
        return None

    plan_path = Path.home() / ".claude" / "plans" / f"{slug}.md"
    if must_exist:
        return plan_path if plan_path.exists() else None
    return plan_path


async def get_context_from_session(
    session_id: str, cwd: Path | None = None, agent_id: str | None = None
) -> int | None:
    """Get total input context tokens from session file's last usage block.

    Sums: input_tokens + cache_creation_input_tokens + cache_read_input_tokens
    """
    session_file = _get_session_file(session_id, cwd, agent_id)
    if not session_file:
        return None

    # Read from end of file to find last usage entry efficiently
    try:
        file_size = os.path.getsize(session_file)
        if file_size == 0:
            return None

        # Read last chunk (usually enough to find last usage)
        chunk_size = min(32768, file_size)  # 32KB chunk
        async with aiofiles.open(session_file, mode="rb") as f:
            await f.seek(file_size - chunk_size)
            chunk = await f.read()

        # Split into lines, process in reverse
        lines = chunk.split(b"\n")
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if "message" in data and isinstance(data["message"], dict):
                    usage = data["message"].get("usage")
                    if usage:
                        return (
                            usage.get("input_tokens", 0)
                            + usage.get("cache_creation_input_tokens", 0)
                            + usage.get("cache_read_input_tokens", 0)
                        )
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Skip lines that fail to parse - expected for partial lines
                # when reading from middle of file (chunk may split UTF-8 chars)
                continue
    except (IOError, OSError):
        return None

    return None
