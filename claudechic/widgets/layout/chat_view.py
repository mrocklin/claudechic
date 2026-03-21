"""ChatView: renders an Agent's message history to widgets and handles streaming."""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.widget import Widget

from claudechic.agent import (
    Agent,
    ImageAttachment,
    UserContent,
    AssistantContent,
    ToolUse,
    TextBlock,
)
from claudechic.config import CONFIG
from claudechic.enums import AgentStatus, ToolName
from claudechic.formatting import format_agent_prompt
from claudechic.widgets.content.message import (
    ChatMessage,
    ChatAttachment,
    ThinkingIndicator,
    SystemInfo,
)
from claudechic.widgets.primitives.scroll import AutoHideScroll
from claudechic.widgets.content.tools import ToolUseWidget, TaskWidget, AgentToolWidget
from claudechic.widgets.content.collapsed_turn import CollapsedTurn

if TYPE_CHECKING:
    from claude_agent_sdk import ToolUseBlock, ToolResultBlock

# Tools to collapse by default
COLLAPSE_BY_DEFAULT = {
    ToolName.WEB_SEARCH,
    ToolName.WEB_FETCH,
    ToolName.ASK_USER_QUESTION,
    ToolName.READ,
    ToolName.GLOB,
    ToolName.GREP,
    ToolName.ENTER_PLAN_MODE,
    ToolName.SKILL,
}

# MCP tool name prefixes to always collapse — configurable via
# collapse-tool-prefixes in ~/.claude/.claudechic.yaml
_DEFAULT_COLLAPSE_PREFIXES = [
    "mcp__plugin_context-mode",
    "mcp__searxng",
]
_COLLAPSE_PREFIXES = tuple(
    CONFIG.get("collapse-tool-prefixes") or _DEFAULT_COLLAPSE_PREFIXES
)


def _collapse_by_default(tool_name: str) -> bool:
    return tool_name in COLLAPSE_BY_DEFAULT or tool_name.startswith(_COLLAPSE_PREFIXES)

# How many recent tools to keep expanded (0 = collapse all)
RECENT_TOOLS_EXPANDED = CONFIG.get("recent-tools-expanded", 2)

# How many recent turns to render fully (older turns collapsed into single widget)
RECENT_TURNS_FULL = CONFIG.get("recent-turns-full", 3)


class ChatView(AutoHideScroll):
    """A scrollable view that renders chat messages and handles streaming.

    Inherits from AutoHideScroll for thin scrollbar and smart tailing behavior.

    This widget owns:
    - Rendering agent.messages to Textual widgets
    - Streaming text updates (current_response tracking)
    - Tool widget lifecycle (pending_tool_widgets, active_task_widgets)
    - Thinking indicator lifecycle
    - Auto-collapse of old tool widgets

    Performance optimization:
    - When hidden (background agent), UI updates are deferred
    - On becoming visible, the view re-renders from agent.messages
    - This avoids CSS recalculation for hidden widgets
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._agent: Agent | None = None

        # Widget tracking
        self._current_response: ChatMessage | None = None
        self._pending_tool_widgets: dict[
            str, ToolUseWidget | TaskWidget | AgentToolWidget
        ] = {}
        self._active_task_widgets: dict[str, TaskWidget] = {}
        self._recent_tools: list[ToolUseWidget | TaskWidget | AgentToolWidget] = []
        self._thinking_indicator: ThinkingIndicator | None = None

        # Deferred update tracking for hidden views
        self._needs_rerender: bool = False

    # -----------------------------------------------------------------------
    # Visibility handling (performance optimization for background agents)
    # -----------------------------------------------------------------------

    @property
    def is_hidden(self) -> bool:
        """Check if this ChatView is currently hidden.

        Note: The 'hidden' class is managed by ChatApp.on_agent_switched(),
        which adds/removes it when switching between agents.
        """
        return self.has_class("hidden")

    def flush_deferred_updates(self) -> None:
        """Re-render from agent.messages if updates were deferred while hidden.

        Call this after remove_class("hidden") to render any updates that
        accumulated while this view was in the background.
        """
        if self._needs_rerender:
            self._render_full()
            self._needs_rerender = False
        # Always restore thinking indicator if agent is busy (even if no updates
        # were deferred - e.g., switched away while waiting for a tool result)
        self._restore_busy_state()

    # -----------------------------------------------------------------------
    # Agent switching (full re-render)
    # -----------------------------------------------------------------------

    def set_agent(self, agent: Agent | None) -> None:
        """Set the agent to render. Triggers full re-render from history.

        If the view is hidden (background agent), rendering is deferred until
        the view becomes visible via flush_deferred_updates().
        """
        self._agent = agent
        if self.is_hidden:
            self._needs_rerender = True
        else:
            self._render_full()

    def _render_full(self) -> None:
        """Fully re-render the chat view from agent.messages.

        Old turns (beyond RECENT_TURNS_FULL) are collapsed into lightweight
        CollapsedTurn widgets that lazy-load full content on expand.

        Uses mount_all() to batch all widget mounts into a single CSS recalculation.
        """
        self.clear()
        if not self._agent:
            return

        # Group messages into turns (user + assistant pairs)
        turns: list[tuple[UserContent | None, AssistantContent | None]] = []
        current_user: UserContent | None = None

        for item in self._agent.messages:
            if item.role == "user" and isinstance(item.content, UserContent):
                # Start new turn
                if current_user is not None:
                    # Previous user had no assistant response
                    turns.append((current_user, None))
                current_user = item.content
            elif item.role == "assistant" and isinstance(
                item.content, AssistantContent
            ):
                # Complete current turn
                turns.append((current_user, item.content))
                current_user = None

        # Handle trailing user message with no response yet
        if current_user is not None:
            turns.append((current_user, None))

        # Determine which turns to collapse (all but last RECENT_TURNS_FULL)
        collapse_before = max(0, len(turns) - RECENT_TURNS_FULL)

        # Count tools in recent turns for tool collapse threshold
        recent_tools = sum(
            sum(1 for b in asst.blocks if isinstance(b, ToolUse))
            for _, asst in turns[collapse_before:]
            if asst is not None
        )
        collapse_threshold = recent_tools - RECENT_TOOLS_EXPANDED
        tool_index = 0

        # Build widgets
        widgets: list[Widget] = []

        for turn_idx, (user_content, assistant_content) in enumerate(turns):
            if turn_idx < collapse_before and user_content and assistant_content:
                # Old turn: collapse into single widget with lazy expansion
                widgets.append(
                    self._create_collapsed_turn(user_content, assistant_content)
                )
            else:
                # Recent turn: render fully
                if user_content:
                    text, is_agent = format_agent_prompt(user_content.text)
                    widgets.extend(
                        self._create_user_widgets(text, user_content.images, is_agent)
                    )
                if assistant_content:
                    new_widgets, tool_index = self._create_assistant_widgets(
                        assistant_content, tool_index, collapse_threshold
                    )
                    widgets.extend(new_widgets)

        # Single mount_all triggers one CSS recalculation instead of N
        self.mount_all(widgets)
        self.scroll_end(animate=False)

    def _create_collapsed_turn(
        self, user_content: UserContent, assistant_content: AssistantContent
    ) -> CollapsedTurn:
        """Create a collapsed turn widget with lazy expansion."""

        def make_turn_widgets() -> list[Widget]:
            """Factory to create full widgets when turn is expanded."""
            widgets: list[Widget] = []
            # User message
            text, is_agent = format_agent_prompt(user_content.text)
            widgets.extend(
                self._create_user_widgets(text, user_content.images, is_agent)
            )
            # Assistant response (collapse all tools, ignore returned tool_index)
            new_widgets, _ = self._create_assistant_widgets(
                assistant_content, tool_index=0, collapse_threshold=999
            )
            widgets.extend(new_widgets)
            return widgets

        return CollapsedTurn(
            user_content, assistant_content, widget_factory=make_turn_widgets
        )

    def _create_user_widgets(
        self, text: str, images: list[ImageAttachment], is_agent: bool = False
    ) -> list[Widget]:
        """Create widgets for a user message (without mounting)."""
        widgets: list[Widget] = []
        msg = ChatMessage(text, is_agent=is_agent)
        msg.add_class("agent-message" if is_agent else "user-message")
        widgets.append(msg)

        for i, img in enumerate(images):
            if img.filename.lower().startswith("screenshot"):
                display_name = f"Screenshot #{i + 1}"
            else:
                display_name = img.filename
            widgets.append(ChatAttachment(img.path, display_name))

        return widgets

    def _create_assistant_widgets(
        self, content: AssistantContent, tool_index: int, collapse_threshold: int
    ) -> tuple[list[Widget], int]:
        """Create widgets for an assistant message (without mounting).

        Iterates over blocks in order to preserve text/tool interleaving.
        Returns (widgets, updated_tool_index).
        """
        from claude_agent_sdk import ToolUseBlock

        widgets: list[Widget] = []
        pending_tools = self._agent.pending_tools if self._agent else {}
        for block in content.blocks:
            if isinstance(block, TextBlock):
                msg = ChatMessage(block.text)
                msg.add_class("assistant-message")
                widgets.append(msg)
            elif isinstance(block, ToolUse):
                # Route nested tools to their parent TaskWidget
                parent_id = block.parent_tool_use_id
                if parent_id and parent_id in self._active_task_widgets:
                    sdk_block = ToolUseBlock(
                        id=block.id, name=block.name, input=block.input
                    )
                    self._active_task_widgets[parent_id].add_tool_use(sdk_block)
                    tool_index += 1
                    continue

                collapse = tool_index < collapse_threshold
                # Check if tool is still pending (no result yet)
                completed = block.id not in pending_tools
                widget = self._create_tool_widget(
                    block, completed=completed, collapsed=collapse
                )
                # Track pending tools for result updates
                if not completed:
                    self._pending_tool_widgets[block.id] = widget
                    # Track active Task widgets for nested tool routing
                    if isinstance(widget, TaskWidget):
                        self._active_task_widgets[block.id] = widget
                widgets.append(widget)
                tool_index += 1

        return widgets, tool_index

    def _create_tool_widget(
        self, tool: ToolUse, completed: bool = False, collapsed: bool = False
    ) -> ToolUseWidget | TaskWidget | AgentToolWidget:
        """Create a tool widget (without mounting)."""
        from claude_agent_sdk import ToolUseBlock

        block = ToolUseBlock(id=tool.id, name=tool.name, input=tool.input)
        should_collapse = collapsed or _collapse_by_default(tool.name)
        cwd = self._agent.cwd if self._agent else None

        if tool.name == ToolName.TASK:
            return TaskWidget(block, collapsed=should_collapse, cwd=cwd)
        elif tool.name.startswith("mcp__chic__"):
            return AgentToolWidget(block, cwd=cwd, completed=completed)
        elif tool.name == ToolName.EXIT_PLAN_MODE:
            plan_path = self._agent.plan_path if self._agent else None
            return ToolUseWidget(
                block,
                collapsed=should_collapse,
                completed=completed,
                cwd=cwd,
                plan_path=plan_path,
            )
        else:
            return ToolUseWidget(
                block, collapsed=should_collapse, completed=completed, cwd=cwd
            )

    # -----------------------------------------------------------------------
    # Streaming API - called by ChatApp during live response
    # -----------------------------------------------------------------------

    def append_user_message(self, text: str, images: list[ImageAttachment]) -> None:
        """Append a user message to the view."""
        # Defer if hidden - will re-render from agent.messages when shown
        if self.is_hidden:
            self._needs_rerender = True
            return
        formatted_text, is_agent = format_agent_prompt(text)
        self._mount_user_message(formatted_text, images, is_agent=is_agent)
        self.scroll_if_tailing()

    def start_response(self) -> None:
        """Show thinking indicator at start of response."""
        # Defer if hidden - no need to show spinner for background agents
        if self.is_hidden:
            self._needs_rerender = True
            return
        if self._thinking_indicator is None:
            self._thinking_indicator = ThinkingIndicator()
            self.mount(self._thinking_indicator)
            self.scroll_if_tailing()

    def end_response(self) -> None:
        """Clean up at end of response."""
        if self.is_hidden:
            self._needs_rerender = True
            return
        self._hide_thinking()
        self._current_response = None

    def append_text(
        self, text: str, new_message: bool, parent_tool_id: str | None
    ) -> None:
        """Append streaming text to the view.

        Args:
            text: The text chunk to append
            new_message: Whether this starts a new ChatMessage
            parent_tool_id: If set, text belongs to a Task widget
        """
        # Defer if hidden - will re-render from agent.messages when shown
        if self.is_hidden:
            self._needs_rerender = True
            return

        self._hide_thinking()

        # Route to Task widget if nested
        if parent_tool_id and parent_tool_id in self._active_task_widgets:
            task = self._active_task_widgets[parent_tool_id]
            task.add_text(text, new_message=new_message)
            return

        # Create new message widget if needed
        if new_message or not self._current_response:
            self._current_response = ChatMessage("")
            self._current_response.add_class("assistant-message")
            self.mount(self._current_response)

        self._current_response.append_content(text)
        self.scroll_if_tailing()

    def append_tool_use(
        self, tool: ToolUse, block: "ToolUseBlock", parent_tool_id: str | None
    ) -> None:
        """Append a tool use widget to the view.

        Args:
            tool: The ToolUse data object
            block: The SDK ToolUseBlock for widget construction
            parent_tool_id: If set, tool belongs to a Task widget
        """
        # Defer if hidden - will re-render from agent.messages when shown
        if self.is_hidden:
            self._needs_rerender = True
            return

        self._hide_thinking()

        # Route to Task widget if nested
        if parent_tool_id and parent_tool_id in self._active_task_widgets:
            task = self._active_task_widgets[parent_tool_id]
            task.add_tool_use(block)
            return

        # Batch collapse + mount to avoid multiple CSS recalculations
        with self.app.batch_update():
            # Auto-collapse old tools
            while len(self._recent_tools) >= RECENT_TOOLS_EXPANDED > 0:
                old = self._recent_tools.pop(0)
                old.collapse()

            # Create widget based on tool type
            collapsed = RECENT_TOOLS_EXPANDED == 0 or _collapse_by_default(tool.name)
            cwd = self._agent.cwd if self._agent else None
            if tool.name == ToolName.TASK:
                widget = TaskWidget(block, collapsed=collapsed, cwd=cwd)
                self._active_task_widgets[tool.id] = widget
            elif tool.name.startswith("mcp__chic__"):
                widget = AgentToolWidget(block, cwd=cwd)
            elif tool.name == ToolName.EXIT_PLAN_MODE:
                # Pass agent's plan_path for correct plan file lookup
                plan_path = self._agent.plan_path if self._agent else None
                widget = ToolUseWidget(
                    block, collapsed=collapsed, cwd=cwd, plan_path=plan_path
                )
            else:
                widget = ToolUseWidget(block, collapsed=collapsed, cwd=cwd)

            self._pending_tool_widgets[tool.id] = widget
            self._recent_tools.append(widget)
            self.mount(widget)
        self.scroll_if_tailing()

    def update_tool_result(
        self, tool_id: str, block: "ToolResultBlock", parent_tool_id: str | None
    ) -> None:
        """Update a tool widget with its result.

        Args:
            tool_id: The tool use ID
            block: The SDK ToolResultBlock
            parent_tool_id: If set, result belongs to a Task widget
        """
        # Defer if hidden - will re-render from agent.messages when shown
        if self.is_hidden:
            self._needs_rerender = True
            return

        # Route to Task widget if nested
        if parent_tool_id and parent_tool_id in self._active_task_widgets:
            task = self._active_task_widgets[parent_tool_id]
            task.add_tool_result(block)
            return

        widget = self._pending_tool_widgets.get(tool_id)
        if widget:
            # For ExitPlanMode, update plan_path in case it wasn't available at creation
            if (
                isinstance(widget, ToolUseWidget)
                and widget.block.name == ToolName.EXIT_PLAN_MODE
            ):
                plan_path = self._agent.plan_path if self._agent else None
                if plan_path and not widget._plan_path:
                    widget.set_plan_path(plan_path)
            widget.set_result(block)
            del self._pending_tool_widgets[tool_id]
            # Clean up task tracking if this was a task
            self._active_task_widgets.pop(tool_id, None)

    def append_system_info(self, message: str, severity: str) -> None:
        """Append a system info message (not stored in history)."""
        widget = SystemInfo(message, severity)
        self.mount(widget)
        widget.scroll_visible()

    def clear(self) -> None:
        """Clear all content from the view."""
        # Await removal in background to ensure task cleanup
        await_remove = self.remove_children()
        self.app.run_worker(await_remove, exclusive=False)
        self._current_response = None
        self._pending_tool_widgets.clear()
        self._active_task_widgets.clear()
        self._recent_tools.clear()
        self._thinking_indicator = None

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _mount_user_message(
        self, text: str, images: list[ImageAttachment], is_agent: bool = False
    ) -> None:
        """Mount a user message widget with optional image attachments."""
        for widget in self._create_user_widgets(text, images, is_agent):
            self.mount(widget)

    def _hide_thinking(self) -> None:
        """Remove thinking indicator if present."""
        if self._thinking_indicator is not None:
            self._thinking_indicator.remove()
            self._thinking_indicator = None

    def _restore_busy_state(self) -> None:
        """Restore thinking indicator if agent is busy with no pending tool spinners."""
        if self._agent and self._agent.status == AgentStatus.BUSY:
            # Only show ThinkingIndicator if there are no pending tools
            # (pending tools have their own inline spinners)
            if not self._pending_tool_widgets and self._thinking_indicator is None:
                self._thinking_indicator = ThinkingIndicator()
                self.mount(self._thinking_indicator)
                self.scroll_end(animate=False)
