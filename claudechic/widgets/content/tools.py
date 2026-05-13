"""Tool display widgets - ToolUseWidget and TaskWidget."""

import json
import logging
import re
from pathlib import Path

from rich.text import Text

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import Markdown, Static

from claudechic.widgets.primitives.button import Button
from claudechic.widgets.primitives.collapsible import QuietCollapsible

from claude_agent_sdk import ToolUseBlock, ToolResultBlock

from claudechic.enums import ToolName
from claudechic.formatting import (
    _TOOL_SEARCH_MAX_SIZE,
    extract_tool_search_names,
    format_tool_header,
    format_tool_input,
    format_result_summary,
    make_relative,
    strip_mcp_prefix,
)
from claudechic.widgets.content.diff import DiffWidget
from claudechic.widgets.content.message import ChatMessage
from claudechic.widgets.primitives.spinner import Spinner
from claudechic.widgets.base.tool_base import BaseToolWidget

log = logging.getLogger(__name__)

# Pattern to strip SDK-injected system reminders from tool results
SYSTEM_REMINDER_PATTERN = re.compile(
    r"\n*<system-reminder>.*?</system-reminder>\n*", re.DOTALL
)


def _extract_text_content(content: str | list) -> str:
    """Extract text from ToolResultBlock content (handles both str and MCP list format)."""
    # MCP format: [{"type": "text", "text": "..."}]
    if isinstance(content, list):
        texts = [
            item.get("text", "")
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        return "\n".join(texts)
    if isinstance(content, str):
        # Handle stringified MCP list format (SDK sometimes returns str repr)
        if content.startswith("[{") and "'text':" in content:
            if len(content) > _TOOL_SEARCH_MAX_SIZE:
                return content
            import ast

            try:
                parsed = ast.literal_eval(content)
                if isinstance(parsed, list):
                    texts = [
                        item.get("text", "")
                        for item in parsed
                        if isinstance(item, dict)
                    ]
                    return "\n".join(texts)
            except (ValueError, SyntaxError, MemoryError, RecursionError):
                pass
        return content
    return str(content)


class EditPlanRequested(Message):
    """Posted when user clicks Edit Plan button."""

    def __init__(self, plan_path: Path) -> None:
        super().__init__()
        self.plan_path = plan_path


class ToolUseWidget(BaseToolWidget):
    """A collapsible widget showing a tool use."""

    def __init__(
        self,
        block: ToolUseBlock,
        collapsed: bool = False,
        completed: bool = False,
        cwd: Path | None = None,
        plan_path: Path | None = None,
    ) -> None:
        super().__init__()
        self.block = block
        self.result: ToolResultBlock | bool | None = True if completed else None
        self._initial_collapsed = collapsed
        self._cwd = cwd
        self._plan_path = plan_path  # For ExitPlanMode
        self._header = format_tool_header(self.block.name, self.block.input, cwd)

    def set_plan_path(self, plan_path: Path | None) -> None:
        """Update plan path (for ExitPlanMode when path becomes available later)."""
        self._plan_path = plan_path

    def _make_diff_content(self) -> list[DiffWidget]:
        """Factory for lazy DiffWidget creation."""
        path = make_relative(self.block.input.get("file_path", ""), self._cwd)
        return [
            DiffWidget(
                self.block.input.get("old_string", ""),
                self.block.input.get("new_string", ""),
                path=path,
                replace_all=self.block.input.get("replace_all", False),
                id="diff-content",
            )
        ]

    def compose(self) -> ComposeResult:
        if not self.result:
            yield Spinner()
        # Skill with no args: just show header, no collapsible
        if self.block.name == ToolName.SKILL and not self.block.input.get("args"):
            yield Static(self._header, classes="skill-header", markup=False)
            return
        # ExitPlanMode: show plan as Markdown with special styling
        if self.block.name == ToolName.EXIT_PLAN_MODE:
            self.add_class("exit-plan-mode")
            plan_content = self._get_plan_content()
            with QuietCollapsible(
                title=self._header, collapsed=self._initial_collapsed
            ):
                if plan_content:
                    yield Markdown(plan_content, id="plan-content")
                else:
                    yield Static("(Plan content not available)", id="tool-output")
                if self._plan_path:
                    yield Button("📋 Edit Plan", classes="edit-plan-btn")
            return
        # ToolSearch: title summarizes query; body only shows loaded tools
        if self.block.name == ToolName.TOOL_SEARCH:
            with QuietCollapsible(
                title=self._header, collapsed=self._initial_collapsed
            ):
                yield Static("", id="tool-output", markup=False)
            return
        # Edit tool: use lazy content when collapsed (DiffWidget is expensive)
        if self.block.name == ToolName.EDIT:
            if self._initial_collapsed:
                # Lazy: defer DiffWidget creation until expanded
                yield QuietCollapsible(
                    title=self._header,
                    collapsed=True,
                    content_factory=self._make_diff_content,
                )
            else:
                # Immediate: user needs to see diff now
                with QuietCollapsible(title=self._header, collapsed=False):
                    yield from self._make_diff_content()
            return
        # Other tools: use normal pattern
        with QuietCollapsible(title=self._header, collapsed=self._initial_collapsed):
            tool_input = format_tool_input(self.block.name, self.block.input, self._cwd)
            # Bash uses "$ command" format with blank line separator
            if self.block.name == ToolName.BASH:
                yield Static(f"$ {tool_input}", id="tool-input", markup=False)
                yield Static("", id="tool-separator")
            else:
                yield Static(tool_input, id="tool-input", markup=False)
                yield Static("─" * 40, id="tool-separator")
            yield Static("", id="tool-output", markup=False)

    def stop_spinner(self) -> None:
        """Stop and remove the spinner."""
        if self.result is not None:
            return
        self.result = True
        try:
            self.query_one(Spinner).remove()
        except Exception:
            pass

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if "edit-plan-btn" in event.button.classes:
            event.stop()
            if self._plan_path:
                self.post_message(EditPlanRequested(self._plan_path))

    def _get_plan_content(self) -> str | None:
        """Get plan content for ExitPlanMode display."""
        # Prefer plan from tool input
        if plan := self.block.input.get("plan"):
            return plan

        # Use plan_path from agent (session-specific)
        if self._plan_path and self._plan_path.exists():
            try:
                return self._plan_path.read_text()
            except Exception:
                pass

        return None

    def _try_update_plan_content(self, collapsible: QuietCollapsible) -> None:
        """Try to update ExitPlanMode plan content if it wasn't available at compose time."""
        try:
            # Check if we have the placeholder - if plan-content exists, we're good
            collapsible.query_one("#plan-content", Markdown)
            return  # Already has Markdown content
        except Exception:
            pass  # No Markdown, check for tool-output placeholder

        # Try to get plan content now
        plan_content = self._get_plan_content()
        if plan_content:
            try:
                output_widget = collapsible.query_one("#tool-output", Static)
                output_widget.remove()
                collapsible.mount(Markdown(plan_content, id="plan-content"))
            except Exception:
                pass

    def set_result(self, result: ToolResultBlock) -> None:
        """Update with tool result."""
        self.result = result
        log.debug(
            f"Tool result for {self.block.name}: {len(str(result.content or ''))} chars"
        )
        # Remove spinner
        try:
            self.query_one(Spinner).remove()
        except Exception:
            pass
        try:
            collapsible = self.query_one(QuietCollapsible)
            if result.is_error:
                collapsible.add_class("error")
            # Update title with result summary
            if result.content:
                content = (
                    result.content
                    if isinstance(result.content, str)
                    else str(result.content)
                )
                summary = format_result_summary(
                    self.block.name, content, result.is_error or False
                )
                if summary:
                    collapsible.title = f"{self._header} {summary}"
            # Edit uses DiffWidget - skip output update
            if self.block.name == ToolName.EDIT:
                return
            # ExitPlanMode: try to update plan content if not available at compose time
            if self.block.name == ToolName.EXIT_PLAN_MODE:
                self._try_update_plan_content(collapsible)
                return
            # Update tool-output Static with plain text result
            output_widget = collapsible.query_one("#tool-output", Static)
            if result.content:
                content = (
                    result.content
                    if isinstance(result.content, str)
                    else str(result.content)
                )
                # Strip SDK-injected system reminders from display
                content = SYSTEM_REMINDER_PATTERN.sub("", content)
                truncated = len(content) > 2000
                preview = content[:2000]
                trunc_suffix = (
                    f"\n... (truncated, {len(content):,} chars total)"
                    if truncated
                    else ""
                )
                if result.is_error:
                    output_widget.update(f"Error:\n{preview}{trunc_suffix}")
                elif self.block.name == ToolName.READ:
                    # Strip line number gutter (format: "   1→ content")
                    preview = re.sub(r"^\s*\d+→\t?", "", preview, flags=re.MULTILINE)
                    if truncated:
                        shown = preview.count("\n") + (
                            1 if preview and not preview.endswith("\n") else 0
                        )
                        total = content.count("\n") + (
                            1 if content and not content.endswith("\n") else 0
                        )
                        preview += f"\n... ({shown} of {total} lines shown)"
                    output_widget.update(preview)
                elif self.block.name == ToolName.ENTER_PLAN_MODE:
                    output_widget.update("Entered plan mode")
                elif self.block.name == ToolName.TOOL_SEARCH:
                    names = extract_tool_search_names(result.content)
                    if names:
                        lines = [
                            Text.assemble(("+ ", "green"), strip_mcp_prefix(n))
                            for n in names
                        ]
                        output_widget.update(Text("\n").join(lines))
                    else:
                        output_widget.update(f"{preview}{trunc_suffix}")
                else:
                    output_widget.update(f"{preview}{trunc_suffix}")
        except Exception:
            pass  # Widget may not be fully mounted


class TaskWidget(BaseToolWidget):
    """A collapsible widget showing a Task with nested subagent content."""

    RECENT_EXPANDED = 2  # Keep last N tool uses expanded within task

    def __init__(
        self, block: ToolUseBlock, collapsed: bool = False, cwd: Path | None = None
    ) -> None:
        super().__init__()
        self.block = block
        self.result: ToolResultBlock | None = None
        self._initial_collapsed = collapsed
        self._cwd = cwd
        self._current_message: ChatMessage | None = None
        self._recent_tools: list[ToolUseWidget] = []
        self._pending_tools: dict[str, ToolUseWidget] = {}

    def compose(self) -> ComposeResult:
        desc = self.block.input.get("description", "Task")
        agent_type = self.block.input.get("subagent_type", "")
        title = f"Task: {desc}" + (f" ({agent_type})" if agent_type else "")
        with QuietCollapsible(title=title, collapsed=self._initial_collapsed):
            yield Static("", id="task-content")

    def stop_spinner(self) -> None:
        """No-op for TaskWidget (no spinner to stop)."""
        pass

    def add_text(self, text: str, new_message: bool = False) -> None:
        """Add text content from subagent."""
        try:
            content = self.query_one("#task-content", Static)
            if new_message or self._current_message is None:
                self._current_message = ChatMessage("")
                self._current_message.add_class("assistant-message")
                if new_message:
                    self._current_message.add_class("after-tool")
                content.mount(self._current_message)
            self._current_message.append_content(text)
        except Exception:
            pass  # Widget may not be mounted

    def add_tool_use(self, block: ToolUseBlock) -> None:
        """Add a tool use from subagent."""
        try:
            content = self.query_one("#task-content", Static)
            while len(self._recent_tools) >= self.RECENT_EXPANDED:
                old = self._recent_tools.pop(0)
                old.collapse()
            widget = ToolUseWidget(block, collapsed=True, cwd=self._cwd)
            self._pending_tools[block.id] = widget
            self._recent_tools.append(widget)
            content.mount(widget)
            self._current_message = None
        except Exception:
            pass  # Widget may not be mounted

    def add_tool_result(self, block: ToolResultBlock) -> None:
        """Add a tool result from subagent."""
        widget = self._pending_tools.get(block.tool_use_id)
        if widget:
            widget.set_result(block)
            del self._pending_tools[block.tool_use_id]

    def set_result(self, result: ToolResultBlock) -> None:
        """Set the Task's own result."""
        self.result = result
        # Stop spinners for any nested tools that didn't get results
        for widget in self._pending_tools.values():
            widget.stop_spinner()
        self._pending_tools.clear()
        try:
            collapsible = self.query_one(QuietCollapsible)
            if result.is_error:
                collapsible.add_class("error")
        except Exception:
            pass  # Widget may not be mounted


class ShellOutputWidget(Static):
    """Collapsible widget showing inline shell command output."""

    DEFAULT_CSS = """
    ShellOutputWidget {
        pointer: pointer;
    }
    """

    can_focus = False
    # Threshold for auto-collapsing
    COLLAPSE_THRESHOLD = 100  # lines

    def __init__(self, command: str, stdout: str, stderr: str, returncode: int) -> None:
        super().__init__()
        self.command = command
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode
        lines = (stdout + stderr).count("\n") + 1
        self._collapsed = lines > self.COLLAPSE_THRESHOLD

    def on_click(self) -> None:
        """Toggle collapsible when clicking anywhere on the widget."""
        try:
            collapsible = self.query_one(QuietCollapsible)
            collapsible.collapsed = not collapsible.collapsed
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        title = f" $ {self.command}"
        if len(title) > 60:
            title = title[:57] + "..."
        if self.returncode != 0:
            title += f" (exit {self.returncode})"
        with QuietCollapsible(title=title, collapsed=self._collapsed):
            # Combine stderr + stdout, parse ANSI color codes
            output = "\n".join(filter(None, [self.stderr, self.stdout])).rstrip()
            if output:
                yield Static(Text.from_ansi(output), id="shell-output")


class PendingShellWidget(Static):
    """Widget showing a running shell command with cancel button."""

    DEFAULT_CSS = """
    PendingShellWidget {
        border-left: thick $surface-darken-2;
        padding: 0 1;
        margin: 0 0 1 0;
        layout: horizontal;
        height: 1;
    }
    PendingShellWidget Spinner {
        width: 1;
    }
    PendingShellWidget .cmd-text {
        margin-left: 1;
        color: $text-muted;
        width: auto;
        max-width: 40;
    }
    PendingShellWidget .cancel-btn {
        margin-left: 1;
        background: $error-darken-1;
        color: $text;
        padding: 0 1;
        width: auto;
        text-align: center;
    }
    PendingShellWidget .cancel-btn:hover {
        background: $error;
    }
    """

    class Cancelled(Message):
        """Emitted when user clicks cancel."""

        def __init__(self, widget: "PendingShellWidget") -> None:
            super().__init__()
            self.widget = widget

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command

    def compose(self) -> ComposeResult:
        yield Spinner()
        yield Static(self.command, classes="cmd-text", markup=False)
        yield Button("Cancel", classes="cancel-btn")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        """Handle cancel button click."""
        event.stop()
        self.post_message(self.Cancelled(self))


class AgentListWidget(Static):
    """Formatted widget for displaying agent list from list_agents."""

    DEFAULT_CSS = """
    AgentListWidget {
        margin: 1 0 0 2;
    }
    """

    def __init__(self, content: str, cwd: Path | None = None) -> None:
        super().__init__()
        self._content = content
        self._cwd = cwd or Path.cwd()
        self._agents: list[tuple[str, str, str]] = []  # (indicator, name, path)
        self._parse_content()

    def _relative_path(self, path_str: str) -> str:
        """Make path relative if it's in cwd or parent directory."""
        try:
            path = Path(path_str.rstrip())
            if path == self._cwd:
                return "."
            if path.is_relative_to(self._cwd):
                return str(path.relative_to(self._cwd))
            if path.parent == self._cwd.parent:
                return f"../{path.name}"
            if path.is_relative_to(self._cwd.parent):
                return f"../{path.relative_to(self._cwd.parent)}"
        except (ValueError, OSError):
            pass
        return path_str

    def _parse_content(self) -> None:
        """Parse agent list content."""
        pattern = re.compile(r"^([* ])(\d+)\. (\S+) \[(\w+)\] - (.+)$")
        for line in self._content.split("\n"):
            if line.startswith("Agents:") or not line.strip():
                continue
            match = pattern.match(line)
            if match:
                active, _, name, _status, path = match.groups()
                indicator = "●" if active == "*" else "○"
                display_path = self._relative_path(path)
                self._agents.append((indicator, name, display_path))

    def compose(self) -> ComposeResult:
        """Render agent list with aligned columns."""
        if not self._agents:
            yield Static(self._content, classes="agent-fallback", markup=False)
            return

        # Pad names to align paths
        max_name = max(len(a[1]) for a in self._agents)
        for indicator, name, path in self._agents:
            padded = name.ljust(max_name)
            yield Static(f"{indicator} {padded}  {path}", markup=False)


class AgentToolWidget(BaseToolWidget):
    """Widget for displaying chic agent MCP tool calls (spawn_agent, ask_agent, etc.)."""

    class GoToAgent(Message):
        """Message posted when user clicks 'Go to agent' button."""

        def __init__(self, agent_name: str) -> None:
            super().__init__()
            self.agent_name = agent_name

    def __init__(
        self, block: ToolUseBlock, cwd: Path | None = None, completed: bool = False
    ) -> None:
        super().__init__()
        self.block = block
        self.result: ToolResultBlock | None = True if completed else None  # type: ignore[assignment]
        self._agent_name = block.input.get("name", "?")
        self._cwd = cwd

    def _make_title(self, verb: str, text: str = "") -> str:
        """Create collapsible title with optional truncated text preview."""
        if not text:
            return f"{verb} {self._agent_name}"
        preview = text[:60] + "..." if len(text) > 60 else text
        return f"{verb} {self._agent_name}: {preview}"

    def compose(self) -> ComposeResult:
        tool_short = self.block.name.replace("mcp__chic__", "")
        prompt = self.block.input.get("prompt", "") or self.block.input.get(
            "message", ""
        )

        if tool_short == "spawn_agent":
            with QuietCollapsible(
                title=self._make_title("Spawn", prompt), collapsed=True
            ):
                if prompt:
                    yield Markdown(prompt)
                yield Button(f"Go to {self._agent_name}", classes="go-btn")

        elif tool_short == "spawn_worktree":
            with QuietCollapsible(
                title=self._make_title("Worktree", prompt), collapsed=True
            ):
                if prompt:
                    yield Markdown(prompt)
                yield Button(f"Go to {self._agent_name}", classes="go-btn")

        elif tool_short == "ask_agent":
            if not self.result:
                yield Spinner()
            with QuietCollapsible(
                title=self._make_title("Ask", prompt), collapsed=True
            ):
                yield Markdown(prompt)
                yield Button(f"Go to {self._agent_name}", classes="go-btn")

        elif tool_short == "tell_agent":
            with QuietCollapsible(
                title=self._make_title("Tell", prompt), collapsed=True
            ):
                yield Markdown(prompt)
                yield Button(f"Go to {self._agent_name}", classes="go-btn")

        elif tool_short == "list_agents":
            with QuietCollapsible(title="List agents", collapsed=True):
                yield Static("")  # Placeholder, result will be mounted

        else:
            with QuietCollapsible(
                title=f"{tool_short}: {self._agent_name}", collapsed=True
            ):
                yield Static(json.dumps(self.block.input, indent=2), markup=False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if "go-btn" in event.button.classes:
            event.stop()
            self.post_message(self.GoToAgent(self._agent_name))

    def set_result(self, result: ToolResultBlock) -> None:
        """Update with tool result."""
        self.result = result
        try:
            self.query_one(Spinner).remove()
        except Exception:
            pass
        # For list_agents, render as formatted agent list
        tool_short = self.block.name.replace("mcp__chic__", "")
        if tool_short == "list_agents" and result.content:
            content = _extract_text_content(result.content)
            content = SYSTEM_REMINDER_PATTERN.sub("", content)
            try:
                self.mount(AgentListWidget(content, cwd=self._cwd))
            except Exception:
                pass
