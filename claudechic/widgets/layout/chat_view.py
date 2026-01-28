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
from claudechic.config import _load_config
from claudechic.enums import ToolName
from claudechic.formatting import format_agent_prompt
from claudechic.widgets.content.message import (
    ChatMessage,
    ChatAttachment,
    ThinkingIndicator,
    SystemInfo,
)
from claudechic.widgets.primitives.scroll import AutoHideScroll
from claudechic.widgets.content.tools import ToolUseWidget, TaskWidget, AgentToolWidget

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

# How many recent tools to keep expanded (0 = collapse all)
RECENT_TOOLS_EXPANDED = _load_config().get("recent-tools-expanded", 2)


class ChatView(AutoHideScroll):
    """A scrollable view that renders chat messages and handles streaming.

    Inherits from AutoHideScroll for thin scrollbar and smart tailing behavior.

    This widget owns:
    - Rendering agent.messages to Textual widgets
    - Streaming text updates (current_response tracking)
    - Tool widget lifecycle (pending_tool_widgets, active_task_widgets)
    - Thinking indicator lifecycle
    - Auto-collapse of old tool widgets
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

    # -----------------------------------------------------------------------
    # Agent switching (full re-render)
    # -----------------------------------------------------------------------

    def set_agent(self, agent: Agent | None) -> None:
        """Set the agent to render. Triggers full re-render from history."""
        self._agent = agent
        self._render_full()

    def _render_full(self) -> None:
        """Fully re-render the chat view from agent.messages.

        Uses mount_all() to batch all widget mounts into a single CSS recalculation,
        which is much faster than mounting widgets one at a time.
        """
        self.clear()
        if not self._agent:
            return

        # Count total tool uses to determine which to collapse
        # (collapse all except last RECENT_TOOLS_EXPANDED)
        total_tools = sum(
            sum(1 for b in item.content.blocks if isinstance(b, ToolUse))
            for item in self._agent.messages
            if item.role == "assistant" and isinstance(item.content, AssistantContent)
        )
        collapse_threshold = total_tools - RECENT_TOOLS_EXPANDED
        tool_index = 0

        # Build all widgets first, then mount in one batch
        widgets: list[Widget] = []
        for item in self._agent.messages:
            if item.role == "user" and isinstance(item.content, UserContent):
                # Format inter-agent messages (ask_agent/tell_agent)
                text, is_agent = format_agent_prompt(item.content.text)
                widgets.extend(
                    self._create_user_widgets(text, item.content.images, is_agent)
                )
            elif item.role == "assistant" and isinstance(
                item.content, AssistantContent
            ):
                new_widgets, tool_index = self._create_assistant_widgets(
                    item.content, tool_index, collapse_threshold
                )
                widgets.extend(new_widgets)

        # Single mount_all triggers one CSS recalculation instead of N
        self.mount_all(widgets)
        self.scroll_end(animate=False)

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
        widgets: list[Widget] = []
        for block in content.blocks:
            if isinstance(block, TextBlock):
                msg = ChatMessage(block.text)
                msg.add_class("assistant-message")
                widgets.append(msg)
            elif isinstance(block, ToolUse):
                collapse = tool_index < collapse_threshold
                widgets.append(
                    self._create_tool_widget(block, completed=True, collapsed=collapse)
                )
                tool_index += 1

        return widgets, tool_index

    def _create_tool_widget(
        self, tool: ToolUse, completed: bool = False, collapsed: bool = False
    ) -> Widget:
        """Create a tool widget (without mounting)."""
        from claude_agent_sdk import ToolUseBlock

        block = ToolUseBlock(id=tool.id, name=tool.name, input=tool.input)
        should_collapse = collapsed or tool.name in COLLAPSE_BY_DEFAULT
        cwd = self._agent.cwd if self._agent else None

        if tool.name == ToolName.TASK:
            return TaskWidget(block, collapsed=should_collapse, cwd=cwd)
        elif tool.name.startswith("mcp__chic__"):
            return AgentToolWidget(block, cwd=cwd, completed=completed)
        else:
            return ToolUseWidget(
                block, collapsed=should_collapse, completed=completed, cwd=cwd
            )

    # -----------------------------------------------------------------------
    # Streaming API - called by ChatApp during live response
    # -----------------------------------------------------------------------

    def append_user_message(self, text: str, images: list[ImageAttachment]) -> None:
        """Append a user message to the view."""
        formatted_text, is_agent = format_agent_prompt(text)
        self._mount_user_message(formatted_text, images, is_agent=is_agent)
        self.scroll_if_tailing()

    def start_response(self) -> None:
        """Show thinking indicator at start of response."""
        if self._thinking_indicator is None:
            self._thinking_indicator = ThinkingIndicator()
            self.mount(self._thinking_indicator)
            self.scroll_if_tailing()

    def end_response(self) -> None:
        """Clean up at end of response."""
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
        self._hide_thinking()

        # Route to Task widget if nested
        if parent_tool_id and parent_tool_id in self._active_task_widgets:
            task = self._active_task_widgets[parent_tool_id]
            task.add_tool_use(block)
            return

        # Auto-collapse old tools
        while len(self._recent_tools) >= RECENT_TOOLS_EXPANDED > 0:
            old = self._recent_tools.pop(0)
            old.collapse()

        # Create widget based on tool type
        collapsed = RECENT_TOOLS_EXPANDED == 0 or tool.name in COLLAPSE_BY_DEFAULT
        cwd = self._agent.cwd if self._agent else None
        if tool.name == ToolName.TASK:
            widget = TaskWidget(block, collapsed=collapsed, cwd=cwd)
            self._active_task_widgets[tool.id] = widget
        elif tool.name.startswith("mcp__chic__"):
            widget = AgentToolWidget(block, cwd=cwd)
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
        # Route to Task widget if nested
        if parent_tool_id and parent_tool_id in self._active_task_widgets:
            task = self._active_task_widgets[parent_tool_id]
            task.add_tool_result(block)
            return

        widget = self._pending_tool_widgets.get(tool_id)
        if widget:
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
