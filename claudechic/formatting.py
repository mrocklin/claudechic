"""Tool formatting and diff rendering utilities."""

import difflib
import json
import re
from pathlib import Path

from rich.text import Text

from claudechic.enums import ToolName


# Constants
MAX_CONTEXT_TOKENS = 200_000  # Claude's context window
MAX_HEADER_WIDTH = 70  # Max width for tool headers

# Inter-agent message patterns
# Matches ask_agent: [Question from agent 'X' - please respond...]
_AGENT_QUESTION_RE = re.compile(
    r"^\[Question from agent '([^']+)' - please respond back using tell_agent, or ask_agent if you need more context\]\n\n"
)
# Matches tell_agent: [Message from agent 'X']
_AGENT_MESSAGE_RE = re.compile(r"^\[Message from agent '([^']+)'\]\n\n")
# Matches spawn_agent/spawn_worktree: [Spawned by agent 'X']
_AGENT_SPAWNED_RE = re.compile(r"^\[Spawned by agent '([^']+)'\]\n\n")


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

    return ""


_MCP_LABELS: dict[str, str] = {
    # Context-mode sandbox tools
    "mcp__plugin_context-mode_context-mode__ctx_execute": "Run Code",
    "mcp__plugin_context-mode_context-mode__ctx_execute_file": "Analyze File",
    "mcp__plugin_context-mode_context-mode__ctx_search": "Search Context",
    "mcp__plugin_context-mode_context-mode__ctx_batch_execute": "Batch Run",
    "mcp__plugin_context-mode_context-mode__ctx_fetch_and_index": "Fetch & Index",
    "mcp__plugin_context-mode_context-mode__ctx_index": "Index Content",
    "mcp__plugin_context-mode_context-mode__ctx_stats": "Context Stats",
    "mcp__plugin_context-mode_context-mode__ctx_doctor": "Context Doctor",
    "mcp__plugin_context-mode_context-mode__ctx_upgrade": "Context Upgrade",
    # SearXNG
    "mcp__searxng__searxng_web_search": "Web Search",
    "mcp__searxng__web_url_read": "Read URL",
    # QMD vault
    "mcp__qmd__query": "Vault Search",
    "mcp__qmd__get": "Vault Get",
    "mcp__qmd__multi_get": "Vault Multi-Get",
    "mcp__qmd__status": "Vault Status",
}


def _format_mcp_header(name: str, input: dict) -> str:
    """Format an MCP tool name as a readable label with key input context."""
    label = _MCP_LABELS.get(name)
    if not label:
        # Generic: mcp__server__tool_name → Server: Tool Name
        parts = name.split("__", 2)
        if len(parts) == 3:
            server = parts[1].replace("-", " ").replace("_", " ").split()[0].title()
            tool = parts[2].replace("_", " ").title()
            label = f"{server}: {tool}"
        else:
            return name

    # Append the most useful input snippet based on what the tool actually uses
    # intent: qmd__query has a human-readable intent field
    if "intent" in input:
        return f"{label}: {str(input['intent'])[:50]}"
    # query: direct search terms (searxng)
    if "query" in input:
        return f"{label}: {str(input['query'])[:50]}"
    # searches: qmd search array — use first query string
    if "searches" in input:
        searches = input["searches"]
        if isinstance(searches, list) and searches:
            first = searches[0]
            q = first.get("query") or first.get("q") if isinstance(first, dict) else str(first)
            if q:
                return f"{label}: {str(q)[:50]}"
    # language: ctx_execute / ctx_execute_file — show language mode
    if "language" in input:
        lang = str(input["language"])
        # For file analysis also show the filename
        if "path" in input:
            fname = Path(input["path"]).name
            return f"{label}: {fname} ({lang})"
        return f"{label}: {lang}"
    # url: web fetch tools
    if "url" in input:
        return f"{label}: {str(input['url'])[:50]}"
    # path: vault get — show just the filename
    if "path" in input:
        fname = Path(input["path"]).name
        return f"{label}: {fname}"
    # commands: ctx_batch_execute — show count
    if "commands" in input:
        n = len(input["commands"]) if isinstance(input["commands"], list) else "?"
        return f"{label} ({n} cmds)"

    return label


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
    elif name == ToolName.ENTER_PLAN_MODE:
        return "EnterPlanMode"
    elif name == ToolName.EXIT_PLAN_MODE:
        return "ExitPlanMode"
    elif name.startswith("mcp__"):
        return _format_mcp_header(name, input)
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
