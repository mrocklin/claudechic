"""Tool formatting and diff rendering utilities."""

import difflib
import json
import re
from pathlib import Path

from rich.text import Text

from claudechic.enums import ToolName


# Constants
# Pre-connect fallback only. The authoritative per-model window comes from
# ClaudeSDKClient.get_context_usage()'s ``rawMaxTokens``, fetched live by
# ``ChatApp.refresh_context``.
DEFAULT_CONTEXT_WINDOW = 200_000
MAX_HEADER_WIDTH = 70  # Max width for tool headers

# Cap on stringified-list parsing via ast.literal_eval. Tool results from
# misbehaving MCP servers can be huge, and ast.literal_eval builds a parse
# tree proportional to input size — a multi-MB payload would freeze the UI.
# 64KB is generous for the actual use case (lists of tool/text descriptors).
LITERAL_EVAL_MAX_SIZE = 65_536


# ANSI CSI escapes — color codes (\x1b[31m), cursor moves (\x1b[2J),
# bracketed-paste markers (\x1b[?2004h). Deliberately narrow: OSC/DCS and
# C1-byte stripping are rare in CLI output and risk corrupting Unicode.
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def strip_ansi(text: str) -> str:
    """Strip ANSI CSI/SGR escape sequences from *text*.

    Used at the chat-display boundary so Markdown rendering doesn't show
    literal ``←[31m`` garbage when SDK stderr or tool output contains
    terminal color codes.
    """
    return _ANSI_CSI_RE.sub("", text)


# Inter-agent message patterns
# Matches ask_agent: [Question from agent 'X' - please respond...]
_AGENT_QUESTION_RE = re.compile(
    r"^\[Question from agent '([^']+)' - please respond back using tell_agent, or ask_agent if you need more context\]\n\n"
)
# Matches tell_agent: [Message from agent 'X']
_AGENT_MESSAGE_RE = re.compile(r"^\[Message from agent '([^']+)'\]\n\n")
# Matches spawn_agent/spawn_worktree: [Spawned by agent 'X']
_AGENT_SPAWNED_RE = re.compile(r"^\[Spawned by agent '([^']+)'\]\n\n")


def strip_mcp_prefix(name: str) -> str:
    """Strip 'mcp__<server>__' prefix for compact display."""
    return re.sub(r"^mcp__[^_]+__", "", name)


def extract_tool_search_names(content) -> list[str] | None:
    """Extract tool names from a ToolSearch result.

    Content is a list of {'type': 'tool_reference', 'tool_name': '...'} dicts,
    or a stringified repr of same. Returns None if the shape doesn't match.
    """
    items = content
    if isinstance(content, str):
        if len(content) > LITERAL_EVAL_MAX_SIZE:
            return None
        if not content.strip().startswith("[{"):
            return None
        try:
            import ast

            items = ast.literal_eval(content)
        except (ValueError, SyntaxError, MemoryError, RecursionError):
            return None
    if not isinstance(items, list):
        return None
    names = [
        item["tool_name"]
        for item in items
        if isinstance(item, dict)
        and item.get("type") == "tool_reference"
        and item.get("tool_name")
    ]
    return names or None


def format_agent_prompt(prompt: str) -> tuple[str, bool]:
    """Format inter-agent prompts for nicer display.

    Detects messages from other agents (via spawn/ask/tell_agent) and
    formats them with markdown styling.

    Returns:
        (formatted_text, is_agent_message)
    """
    match = _AGENT_QUESTION_RE.match(prompt)
    if match:
        agent_name = match.group(1)
        rest = prompt[match.end() :]
        return f"Question from **{agent_name}**:\n\n{rest}", True
    match = _AGENT_MESSAGE_RE.match(prompt)
    if match:
        agent_name = match.group(1)
        rest = prompt[match.end() :]
        return f"From **{agent_name}**:\n\n{rest}", True
    match = _AGENT_SPAWNED_RE.match(prompt)
    if match:
        agent_name = match.group(1)
        rest = prompt[match.end() :]
        return f"Spawned by **{agent_name}**:\n\n{rest}", True
    return prompt, False


def trim_model_name(name: str) -> str:
    """Strip trailing qualifiers from a model display name.

    Examples:
        "Opus 4.7 with 1M context" -> "Opus 4.7"
        "Sonnet 4.5 (beta)" -> "Sonnet 4.5"
        "Haiku" -> "Haiku"
    """
    if not name:
        return name
    # Drop anything from " with " onwards (case-insensitive)
    trimmed = re.split(r"\s+with\s+", name, maxsplit=1, flags=re.IGNORECASE)[0]
    # Drop trailing parenthesized qualifiers like "(beta)" or "(experimental)"
    trimmed = re.sub(r"\s*\([^)]*\)\s*$", "", trimmed)
    return trimmed.strip()


def make_relative(path: str, cwd: Path | None) -> str:
    """Make path relative to cwd if possible, otherwise return as-is."""
    if not cwd or not path:
        return path
    try:
        p = Path(path)
        if p.is_absolute() and p.is_relative_to(cwd):
            return str(p.relative_to(cwd))
    except (ValueError, OSError):
        pass
    return path


def truncate_path(path: str, max_len: int) -> str:
    """Truncate path from the front, preserving the end which is more informative.

    Truncates just before a path separator when possible.
    """
    if len(path) <= max_len:
        return path
    # Leave room for "..."
    available = max_len - 3
    if available <= 0:
        return "..." + path[-max_len:] if max_len > 0 else ""
    suffix = path[-available:]
    # Try to truncate at a path separator for cleaner output
    sep_idx = suffix.find("/")
    if sep_idx > 0 and sep_idx < len(suffix) - 1:
        suffix = suffix[sep_idx:]
    return "..." + suffix


def count_diff_changes(old: str, new: str) -> tuple[int, int]:
    """Count additions and deletions in a diff.

    Returns (additions, deletions) as line counts.
    """
    old_lines = old.splitlines() if old else []
    new_lines = new.splitlines() if new else []
    sm = difflib.SequenceMatcher(None, old_lines, new_lines)

    additions = 0
    deletions = 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "delete":
            deletions += i2 - i1
        elif tag == "insert":
            additions += j2 - j1
        elif tag == "replace":
            deletions += i2 - i1
            additions += j2 - j1
    return additions, deletions


def format_result_summary(name: str, content: str, is_error: bool = False) -> str:
    """Extract a short summary from tool result content.

    Returns a parenthesized summary like "(143 lines)" or "(exit 1)".
    """
    if is_error:
        return "(error)"

    if name == ToolName.READ:
        # Count lines in result (content has N newlines for N+1 lines, unless empty)
        lines = content.count("\n") + 1 if content.strip() else 0
        return f"({lines} lines)"

    elif name == ToolName.BASH:
        stripped = content.strip()
        if not stripped:
            return "(no output)"
        lines = stripped.split("\n")
        # Check last line for exit code pattern
        if "exit code" in lines[-1].lower():
            return f"({lines[-1].strip()})"
        return f"({len(lines)} lines)"

    elif name == ToolName.GREP:
        # Count matches (files or lines)
        lines = [line for line in content.strip().split("\n") if line.strip()]
        if not lines or (len(lines) == 1 and "no matches" in lines[0].lower()):
            return "(no matches)"
        return f"({len(lines)} matches)"

    elif name == ToolName.GLOB:
        # Count files
        lines = [line for line in content.strip().split("\n") if line.strip()]
        if not lines:
            return "(no files)"
        return f"({len(lines)} files)"

    elif name == ToolName.WRITE:
        return "(done)"

    elif name == ToolName.TOOL_SEARCH:
        names = extract_tool_search_names(content)
        count = len(names) if names else content.count("<function>")
        return f"({count} tools)" if count else ""

    return ""


def format_tool_header(name: str, input: dict, cwd: Path | None = None) -> str:
    """Format a one-line header for a tool use."""
    if name == ToolName.EDIT:
        old = input.get("old_string", "")
        new = input.get("new_string", "")
        additions, deletions = count_diff_changes(old, new)
        # Leave room for path + change counts
        stats = f" (+{additions}, -{deletions})"
        path = make_relative(input.get("file_path", "?"), cwd)
        path = truncate_path(path, MAX_HEADER_WIDTH - 6 - len(stats))
        return f"Edit: {path}{stats}"
    elif name == ToolName.WRITE:
        path = make_relative(input.get("file_path", "?"), cwd)
        path = truncate_path(path, MAX_HEADER_WIDTH - 7)
        return f"Write: {path}"
    elif name == ToolName.READ:
        path = make_relative(input.get("file_path", "?"), cwd)
        path = truncate_path(path, MAX_HEADER_WIDTH - 6)
        return f"Read: {path}"
    elif name == ToolName.BASH:
        cmd = input.get("command", "?")
        desc = input.get("description", "")
        if desc:
            return f"Bash: {desc}"
        return f"Bash: {cmd[:50]}{'...' if len(cmd) > 50 else ''}"
    elif name == ToolName.GLOB:
        return f"Glob: {input.get('pattern', '?')}"
    elif name == ToolName.GREP:
        return f"Grep: {input.get('pattern', '?')}"
    elif name == ToolName.WEB_SEARCH:
        return f"WebSearch: {input.get('query', '?')}"
    elif name == ToolName.WEB_FETCH:
        return f"WebFetch: {input.get('url', '?')[:50]}"
    elif name == ToolName.TASK:
        desc = input.get("description", "")
        agent = input.get("subagent_type", "")
        if desc:
            return f"Task: {desc}" + (f" ({agent})" if agent else "")
        return "Task" + (f" ({agent})" if agent else "")
    elif name == ToolName.TODO_WRITE:
        todos = input.get("todos", [])
        return f"TodoWrite: {len(todos)} items"
    elif name == ToolName.ASK_USER_QUESTION:
        questions = input.get("questions", [])
        if questions and questions[0].get("question"):
            q = questions[0]["question"][:40]
            return f"AskUserQuestion: {q}..."
        return "AskUserQuestion"
    elif name == ToolName.SKILL:
        return f"Skill: {input.get('skill', '?')}"
    elif name == ToolName.TOOL_SEARCH:
        query = input.get("query", "?")
        if query.startswith("select:"):
            names = (n.strip() for n in query[len("select:") :].split(","))
            query = ", ".join(strip_mcp_prefix(n) for n in names if n)
        max_results = input.get("max_results")
        suffix = f" (max {max_results})" if max_results and max_results != 5 else ""
        return f"ToolSearch: {query}{suffix}"
    elif name == ToolName.ENTER_PLAN_MODE:
        return "EnterPlanMode"
    elif name == ToolName.EXIT_PLAN_MODE:
        return "ExitPlanMode"
    else:
        return f"{name}"


def get_lang_from_path(path: str) -> str:
    """Guess language from file extension for syntax highlighting."""
    ext = Path(path).suffix.lower()
    return {
        ".py": "python",
        ".js": "javascript",
        ".ts": "typescript",
        ".jsx": "jsx",
        ".tsx": "tsx",
        ".rs": "rust",
        ".go": "go",
        ".rb": "ruby",
        ".java": "java",
        ".c": "c",
        ".cpp": "cpp",
        ".h": "c",
        ".hpp": "cpp",
        ".css": "css",
        ".html": "html",
        ".json": "json",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".md": "markdown",
        ".sh": "bash",
        ".bash": "bash",
    }.get(ext, "")


def _tokenize(s: str) -> list[str]:
    """Split string into words and punctuation for word-level diff."""
    return re.findall(r"\w+|[^\w\s]|\s+", s)


def _render_word_diff(old_line: str, new_line: str, result: Text) -> None:
    """Render a single line pair with word-level highlighting."""
    old_tokens = _tokenize(old_line)
    new_tokens = _tokenize(new_line)
    sm = difflib.SequenceMatcher(None, old_tokens, new_tokens)

    # Build old line - use color only, no background
    result.append("- ", style="red")
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        chunk = "".join(old_tokens[i1:i2])
        if tag == "equal":
            result.append(chunk, style="red dim")
        elif tag in ("delete", "replace"):
            result.append(chunk, style="red bold")
    result.append("\n")

    # Build new line - use color only, no background
    result.append("+ ", style="green")
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        chunk = "".join(new_tokens[j1:j2])
        if tag == "equal":
            result.append(chunk, style="green dim")
        elif tag in ("insert", "replace"):
            result.append(chunk, style="green bold")
    result.append("\n")


def format_diff_text(old: str, new: str, max_len: int = 300) -> Text:
    """Format a diff with subtle red/green backgrounds."""
    result = Text()
    old_preview = old[:max_len] + ("..." if len(old) > max_len else "")
    new_preview = new[:max_len] + ("..." if len(new) > max_len else "")
    old_lines = old_preview.split("\n") if old else []
    new_lines = new_preview.split("\n") if new else []

    sm = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for line in old_lines[i1:i2]:
                result.append(f"  {line}\n", style="dim")
        elif tag == "delete":
            for line in old_lines[i1:i2]:
                result.append(f"- {line}\n", style="red")
        elif tag == "insert":
            for line in new_lines[j1:j2]:
                result.append(f"+ {line}\n", style="green")
        elif tag == "replace":
            # For replaced lines, highlight word-level changes
            for old_line, new_line in zip(old_lines[i1:i2], new_lines[j1:j2]):
                _render_word_diff(old_line, new_line, result)
            # Handle unequal line counts
            for line in old_lines[i1 + len(new_lines[j1:j2]) : i2]:
                result.append(f"- {line}\n", style="red")
            for line in new_lines[j1 + len(old_lines[i1:i2]) : j2]:
                result.append(f"+ {line}\n", style="green")
    return result


def format_tool_input(name: str, input: dict, cwd: Path | None = None) -> str:
    """Format plain-text input for a tool use (no markdown)."""
    if name == ToolName.WRITE:
        content = input.get("content", "")
        preview = content[:400] + ("..." if len(content) > 400 else "")
        return preview
    elif name == ToolName.READ:
        path = make_relative(input.get("file_path", "?"), cwd)
        offset = input.get("offset")
        limit = input.get("limit")
        if isinstance(offset, int) or isinstance(limit, int):
            start = offset if isinstance(offset, int) else 0
            end = start + limit if isinstance(limit, int) else "end"
            return f"{path} (lines {start}-{end})"
        return path
    elif name == ToolName.BASH:
        return input.get("command", "?")
    elif name == ToolName.GLOB:
        pattern = input.get("pattern", "?")
        path = input.get("path")
        if path and path != ".":
            return f"{pattern} in {path}"
        return pattern
    elif name == ToolName.GREP:
        pattern = input.get("pattern", "?")
        path = input.get("path")
        if path and path != ".":
            return f"{pattern} in {path}"
        return pattern
    elif name == ToolName.ENTER_PLAN_MODE:
        return "Entering plan mode"
    elif name == ToolName.EXIT_PLAN_MODE:
        return "Exiting plan mode"
    elif name == ToolName.SKILL:
        args = input.get("args", "")
        return args if args else ""
    else:
        return json.dumps(input, indent=2)
