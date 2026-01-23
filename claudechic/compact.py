"""Session compaction - reduce context by shrinking old tool uses.

This module modifies session JSONL files in-place. We shrink old, large
tool_use inputs and tool_result outputs while preserving the message structure
needed for Claude Code's renderer.

The SDK reads the JSONL file on resume, so modifying it directly affects
what Claude sees.
"""

import json
import shutil
from collections import defaultdict
from pathlib import Path

from claudechic.enums import ToolName
from claudechic.sessions import get_project_sessions_dir


# Files that should never have their Read results compacted (matched by basename).
READ_WHITELIST = [
    "CLAUDE.md",
    "README.md",
    "pyproject.toml",
    "package.json",
    "Cargo.toml",
]


def _is_whitelisted_read(file_path: str) -> bool:
    """Check if a file's basename matches the read whitelist."""
    return Path(file_path).name in READ_WHITELIST


# Compacted output strings for each tool type.
# These are minimal strings that won't crash Claude Code's renderer.
COMPACTED_RESULTS = {
    # Most tools just use plain text - a simple message works
    ToolName.BASH: "[output compacted]",
    ToolName.READ: "[file content compacted]",
    ToolName.WRITE: "[compacted]",
    ToolName.EDIT: "[compacted]",
    ToolName.GLOB: "[compacted]",
    ToolName.TASK: "[compacted]",
    ToolName.SKILL: "[compacted]",
    ToolName.TODO_WRITE: "[compacted]",
    ToolName.WEB_FETCH: "[compacted]",
    ToolName.WEB_SEARCH: "[compacted]",
    # Grep needs special handling - empty result that parses correctly
    ToolName.GREP: "No matches found",
}


def compact_session(
    session_id: str,
    cwd: Path | None = None,
    keep_last_n: int = 5,  # Keep last N tool results regardless of size
    min_result_size: int = 1000,  # Only shrink results larger than this (bytes)
    min_input_size: int = 2000,  # Only shrink inputs larger than this (bytes)
    aggressive: bool = False,  # If True, use lower thresholds (500/1000)
    dry_run: bool = False,
) -> dict:
    """Compact a session by shrinking old, large tool_use/tool_result pairs.

    Strategy: Only shrink things that are BOTH old AND large.
    - Small tool results (<1KB) kept regardless of age
    - Small tool inputs (<2KB) kept regardless of age
    - Recent items (last N per tool type) kept regardless of size
    - Read results preserved unless followed by Write/Edit to same file
    - Whitelisted files (CLAUDE.md, etc.) never compacted
    """
    # Aggressive mode uses lower thresholds
    if aggressive:
        min_result_size = min(min_result_size, 500)
        min_input_size = min(min_input_size, 1000)

    sessions_dir = get_project_sessions_dir(cwd)
    if not sessions_dir:
        return {"error": "No sessions directory found"}

    session_file = sessions_dir / f"{session_id}.jsonl"
    if not session_file.exists():
        return {"error": f"Session file not found: {session_file}"}

    # Load all messages
    messages = []
    with open(session_file, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                messages.append(json.loads(line))

    # First pass: collect tool_use info and track order
    tool_uses: dict = {}  # tool_id -> {name, input, input_size, msg_idx}
    tool_order: list = []  # tool_ids in order

    # Track file operations for Read compaction heuristics
    file_reads: dict[str, list[str]] = defaultdict(list)  # file_path -> [tool_ids]
    file_writes: dict[str, list[str]] = defaultdict(list)

    for msg_idx, m in enumerate(messages):
        if m.get("type") == "assistant":
            for block in m.get("message", {}).get("content", []):
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block["id"]
                    inp = block.get("input", {})
                    input_size = len(json.dumps(inp))
                    tool_name = block.get("name")
                    tool_uses[tool_id] = {
                        "name": tool_name,
                        "input": inp,
                        "input_size": input_size,
                        "msg_idx": msg_idx,
                    }
                    tool_order.append(tool_id)

                    # Track file operations
                    file_path = inp.get("file_path")
                    if file_path:
                        if tool_name == ToolName.READ:
                            file_reads[file_path].append(tool_id)
                        elif tool_name in (ToolName.WRITE, ToolName.EDIT):
                            file_writes[file_path].append(tool_id)

    # Second pass: collect tool_result info
    # tool_id -> result_size
    tool_results: dict = {}
    for m in messages:
        if m.get("type") == "user":
            content = m.get("message", {}).get("content", [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_id = block.get("tool_use_id")
                        result_content = block.get("content", "")
                        tool_results[tool_id] = len(str(result_content))

    # Decide what to truncate based on size AND recency
    # Keep last N of each tool type
    tool_counts: dict = defaultdict(int)
    recent_tools: set = set()

    for tool_id in reversed(tool_order):
        info = tool_uses.get(tool_id, {})
        name = info.get("name", "unknown")
        if tool_counts[name] < keep_last_n:
            recent_tools.add(tool_id)
            tool_counts[name] += 1

    # Identify tool_uses with large inputs to compact
    compact_input_ids = set()
    for tool_id, info in tool_uses.items():
        if tool_id in recent_tools:
            continue  # Keep recent
        if info["input_size"] < min_input_size:
            continue  # Keep small
        compact_input_ids.add(tool_id)

    # Identify tool_results with large outputs to compact
    compact_result_ids = set()
    for tool_id, result_size in tool_results.items():
        if tool_id in recent_tools:
            continue  # Keep recent
        if result_size < min_result_size:
            continue  # Keep small
        compact_result_ids.add(tool_id)

    # Special handling for Read: preserve reads unless followed by a write to same file
    # This keeps "context gathering" reads while compacting "read before edit" patterns
    for file_path, read_ids in file_reads.items():
        write_ids = file_writes.get(file_path, [])

        for read_id in read_ids:
            if read_id not in compact_result_ids:
                continue  # Already preserved (recent or small)

            # Check whitelist
            if _is_whitelisted_read(file_path):
                compact_result_ids.discard(read_id)
                continue

            # Check if there's any write to this file after this read
            read_msg_idx = tool_uses[read_id]["msg_idx"]
            has_later_write = any(
                tool_uses[wid]["msg_idx"] > read_msg_idx for wid in write_ids
            )

            # Preserve if no write follows (this read provides unique context)
            if not has_later_write:
                compact_result_ids.discard(read_id)

    # Create compacted messages
    compacted_messages = []

    for m in messages:
        msg_type = m.get("type")

        # Handle assistant messages - shrink tool_use inputs
        if msg_type == "assistant":
            content = m.get("message", {}).get("content", [])
            if not isinstance(content, list):
                compacted_messages.append(m)
                continue

            new_content = []
            modified = False
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id")
                    if tool_id in compact_input_ids:
                        # Shrink the input but keep the tool_use block
                        info = tool_uses[tool_id]
                        new_block = {
                            **block,
                            "input": {
                                "_compacted": True,
                                "_original_size": info["input_size"],
                            },
                        }
                        new_content.append(new_block)
                        modified = True
                    else:
                        new_content.append(block)
                else:
                    new_content.append(block)

            if modified:
                new_msg = {**m, "message": {**m["message"], "content": new_content}}
                compacted_messages.append(new_msg)
            else:
                compacted_messages.append(m)

        # Handle user messages - shrink tool_result outputs
        elif msg_type == "user":
            content = m.get("message", {}).get("content", [])
            if not isinstance(content, list):
                compacted_messages.append(m)
                continue

            new_content = []
            modified = False
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_id = block.get("tool_use_id")
                    if tool_id in compact_result_ids:
                        # Get the tool name to use the right compacted output
                        tool_name = tool_uses.get(tool_id, {}).get("name", "unknown")
                        compacted_output = COMPACTED_RESULTS.get(
                            tool_name, "[compacted]"
                        )
                        new_block = {
                            "type": "tool_result",
                            "tool_use_id": tool_id,
                            "content": compacted_output,
                        }
                        new_content.append(new_block)
                        modified = True
                    else:
                        new_content.append(block)
                else:
                    new_content.append(block)

            if modified:
                new_msg = {**m, "message": {**m["message"], "content": new_content}}
                # Also update toolUseResult if present
                if "toolUseResult" in new_msg and len(new_content) == 1:
                    tool_id = new_content[0].get("tool_use_id")
                    if tool_id in compact_result_ids:
                        tool_name = tool_uses.get(tool_id, {}).get("name", "unknown")
                        new_msg["toolUseResult"] = COMPACTED_RESULTS.get(
                            tool_name, "[compacted]"
                        )
                compacted_messages.append(new_msg)
            else:
                compacted_messages.append(m)

        else:
            compacted_messages.append(m)

    # Calculate before/after token breakdown by category
    def calc_tokens(msgs: list) -> dict[str, int]:
        """Calculate token breakdown for a message list."""
        breakdown: dict[str, float] = defaultdict(float)
        for m in msgs:
            t = m.get("type")
            if t == "assistant":
                for block in m.get("message", {}).get("content", []):
                    if isinstance(block, dict):
                        if block.get("type") == "text":
                            breakdown["assistant_text"] += (
                                len(block.get("text", "")) / 4
                            )
                        elif block.get("type") == "tool_use":
                            breakdown["tool_inputs"] += (
                                len(json.dumps(block.get("input", {}))) / 4
                            )
            elif t == "user":
                content = m.get("message", {}).get("content", [])
                if isinstance(content, str):
                    breakdown["user_text"] += len(content) / 4
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict):
                            if block.get("type") == "tool_result":
                                breakdown["tool_results"] += (
                                    len(str(block.get("content", ""))) / 4
                                )
                            elif block.get("type") == "text":
                                breakdown["user_text"] += len(block.get("text", "")) / 4
        return {k: int(v) for k, v in breakdown.items()}

    before_breakdown = calc_tokens(messages)
    after_breakdown = calc_tokens(compacted_messages)

    before_total = sum(before_breakdown.values())
    after_total = sum(after_breakdown.values())
    tokens_saved = before_total - after_total

    stats = {
        "compacted_inputs": len(compact_input_ids),
        "compacted_results": len(compact_result_ids),
        "tokens_saved": tokens_saved,
        "before_total": before_total,
        "after_total": after_total,
        "before_breakdown": before_breakdown,
        "after_breakdown": after_breakdown,
        "file": str(session_file),
    }

    if dry_run:
        stats["dry_run"] = True
        return stats

    # Write compacted file
    backup_file = session_file.with_suffix(".jsonl.bak")
    shutil.copy(session_file, backup_file)

    with open(session_file, "w", encoding="utf-8") as f:
        for m in compacted_messages:
            f.write(json.dumps(m) + "\n")

    stats["backup"] = str(backup_file)
    return stats


def format_compact_summary(stats: dict, dry_run: bool = False) -> str:
    """Format compaction stats as a markdown summary for display."""
    before = stats.get("before_total", 0)
    after = stats.get("after_total", 0)

    before_bd = stats.get("before_breakdown", {})
    after_bd = stats.get("after_breakdown", {})

    def pct(val: int, total: int) -> str:
        if total == 0:
            return f"{val:,} (0%)"
        return f"{val:,} ({val * 100 // total}%)"

    # Build markdown table
    header = "## Compaction Preview (dry run)" if dry_run else "## Session Compacted"
    lines = [
        header,
        "",
        "| Category | Before | After |",
        "|----------|-------:|------:|",
    ]

    categories = ["tool_results", "tool_inputs", "assistant_text", "user_text"]
    for cat in categories:
        b = before_bd.get(cat, 0)
        a = after_bd.get(cat, 0)
        if b > 0 or a > 0:
            cat_display = cat.replace("_", " ").title()
            lines.append(f"| {cat_display} | {pct(b, before)} | {pct(a, after)} |")

    lines.append(f"| **Total** | **{before:,}** | **{after:,}** |")

    return "\n".join(lines)
