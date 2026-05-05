"""Agent: autonomous Claude agent with SDK connection and message history."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import mimetypes
import re
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from claude_agent_sdk import (
    AssistantMessage,
    CLIJSONDecodeError,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    # Aliased to disambiguate from the local TextBlock dataclass below.
    TextBlock as SdkTextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from claude_agent_sdk.types import (
    PermissionMode,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    StreamEvent,
    ToolPermissionContext,
)

from claudechic.enums import AgentStatus, PermissionChoice, ToolName
from claudechic.features.worktree.git import FinishState
from claudechic.file_index import FileIndex
from claudechic.permissions import PermissionRequest
from claudechic.sessions import get_plan_path_for_session
from claudechic.tasks import create_safe_task

if TYPE_CHECKING:
    from claudechic.protocols import AgentObserver, PermissionHandler

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message types for chat history
# ---------------------------------------------------------------------------


@dataclass
class ImageAttachment:
    """An image attached to a message."""

    path: str
    filename: str
    media_type: str
    base64_data: str


@dataclass
class UserContent:
    """A user message in chat history."""

    text: str
    images: list[ImageAttachment] = field(default_factory=list)


@dataclass
class ToolUse:
    """A tool use within an assistant turn."""

    id: str
    name: str
    input: dict[str, Any]
    parent_tool_use_id: str | None = None
    result: str | None = None
    is_error: bool = False


@dataclass
class TextBlock:
    """A text block within an assistant turn."""

    text: str


@dataclass
class AssistantContent:
    """An assistant message in chat history.

    Contains an ordered list of blocks (TextBlock or ToolUse) to preserve
    the original interleaving of text and tool uses.
    """

    blocks: list[TextBlock | ToolUse] = field(default_factory=list)


@dataclass
class ChatItem:
    """A single item in chat history."""

    role: Literal["user", "assistant"]
    content: UserContent | AssistantContent


# ---------------------------------------------------------------------------
# Settings lookup
# ---------------------------------------------------------------------------


# SDK-valid permission modes. We pass any of these through to the SDK verbatim
# so users who set e.g. "bypassPermissions" in settings.json get the behavior
# they asked for, even though the UI state machine doesn't represent those modes.
_SDK_PERMISSION_MODES = frozenset(
    {"default", "acceptEdits", "plan", "bypassPermissions", "dontAsk", "auto"}
)

# Modes the UI (footer label, shift+tab cycle) knows how to display. A narrower
# subset of the SDK modes; see `to_ui_permission_mode` for the mapping.
# `planSwarm` is intentionally excluded — it's an internal state toggled by
# a slash command, not something users should configure via settings.json.
_UI_PERMISSION_MODES = frozenset({"default", "acceptEdits", "plan", "auto"})


def get_default_permission_mode(cwd: Path) -> PermissionMode:
    """Resolve ``permissions.defaultMode`` from Claude settings.json layers.

    Layering matches Claude Code: user < project < local. Returns the topmost
    SDK-valid mode, or ``"default"`` if nothing is set. The value is suitable
    to pass directly to ``ClaudeAgentOptions(permission_mode=...)``.
    """
    layers = [
        Path.home() / ".claude" / "settings.json",
        cwd / ".claude" / "settings.json",
        cwd / ".claude" / "settings.local.json",
    ]
    resolved: PermissionMode = "default"
    for path in layers:
        try:
            data = json.loads(path.read_text())
        except (FileNotFoundError, PermissionError, json.JSONDecodeError):
            continue
        mode = data.get("permissions", {}).get("defaultMode")
        if isinstance(mode, str) and mode in _SDK_PERMISSION_MODES:
            resolved = cast(PermissionMode, mode)
    return resolved


def to_ui_permission_mode(mode: str) -> str:
    """Project an SDK mode onto the UI state machine.

    The UI only represents ``{default, acceptEdits, plan, auto}``; modes outside
    that set (``bypassPermissions``, ``dontAsk``) collapse to ``"default"`` so
    the footer and shift+tab cycle stay coherent. The SDK still receives the
    original mode — this only affects what ``Agent.permission_mode`` holds.
    """
    return mode if mode in _UI_PERMISSION_MODES else "default"


# ---------------------------------------------------------------------------
# Agent class
# ---------------------------------------------------------------------------


class Agent:
    """Autonomous Claude agent with its own SDK connection and state.

    The Agent owns:
    - SDK client and connection lifecycle
    - Message history (list of ChatItem)
    - Permission request queue
    - Per-agent state (images, todos, file index, etc.)

    Events are emitted via the observer protocol for UI integration.
    """

    # Tools to auto-approve when auto_approve_edits is True
    AUTO_EDIT_TOOLS = {ToolName.EDIT, ToolName.WRITE}

    # Tools blocked in plan mode (read-only enforcement)
    PLAN_MODE_BLOCKED_TOOLS = {
        ToolName.EDIT,
        ToolName.WRITE,
        ToolName.BASH,
        ToolName.NOTEBOOK_EDIT,
    }

    def __init__(
        self,
        name: str,
        cwd: Path,
        *,
        id: str | None = None,
        worktree: str | None = None,
        permission_mode: str = "default",
    ):
        # Identity
        self.id = id or str(uuid.uuid4())[:8]
        self.name = name
        self.cwd = cwd
        self.worktree = worktree

        # SDK
        self.client: ClaudeSDKClient | None = None
        self.session_id: str | None = None
        # Continuous reader: lives for the whole connection so post-turn
        # messages (e.g. from ScheduleWakeup/Monitor) aren't stranded.
        self._reader_task: asyncio.Task | None = None

        # Status
        self.status: AgentStatus = AgentStatus.IDLE
        self._thinking: bool = False  # Whether this agent is currently thinking
        self._interrupted: bool = False  # Suppress errors after intentional interrupt

        # Chat history
        self.messages: list[ChatItem] = []
        self._current_assistant: AssistantContent | None = None
        self._current_text_buffer: str = ""

        # Permission queue
        self.pending_prompts: deque[PermissionRequest] = deque()

        # Tool tracking (within current response)
        self.pending_tools: dict[str, ToolUse] = {}
        self.active_tasks: dict[str, str] = {}  # task_id -> accumulated text
        self.response_had_tools: bool = False
        self._needs_new_message: bool = True  # Start new ChatMessage on next text
        self._thinking_hidden: bool = (
            False  # Track if thinking indicator was hidden this response
        )

        # Per-agent state
        self.pending_images: list[ImageAttachment] = []
        self.file_index: FileIndex | None = None
        self.todos: list[dict] = []
        self.permission_mode: str = permission_mode  # default, acceptEdits, plan, auto
        self.session_allowed_tools: set[str] = set()  # Tools allowed for this session
        self._pending_followup: str | None = None  # Auto-send after current response
        self.model: str | None = None  # Model override (None = SDK default)
        self.effort: str | None = None  # Effort level (low/medium/high/xhigh/max)

        # Worktree finish state (for /worktree finish flow)
        self.finish_state: FinishState | None = None

        # Plan file path (cached after first lookup)
        self.plan_path: Path | None = None

        # UI state (managed by ChatApp, not widget references)
        self.pending_input: str = ""  # Saved input text when switching away

        # Observer for UI integration (set by AgentManager)
        self.observer: AgentObserver | None = None
        self.permission_handler: PermissionHandler | None = None

        # Background process tracking (PID of claude binary)
        self._claude_pid: int | None = None
        # Background task output files: command -> output_file path
        self._background_outputs: dict[str, str] = {}

        # Pending plan execution (set when "clear context + auto-approve" chosen)
        self.pending_plan_execution: dict | None = None  # {"plan": str, "mode": str}

        # Checkpoint tracking for /rewind command (UUIDs of user messages)
        self.checkpoint_uuids: list[str] = []

    @property
    def analytics_id(self) -> str:
        """ID for analytics events (session_id if connected, else internal id)."""
        return self.session_id or self.id

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def connect(
        self,
        options: ClaudeAgentOptions,
        resume: str | None = None,
    ) -> None:
        """Connect to SDK.

        Args:
            options: SDK options (should have can_use_tool set to self._handle_permission)
            resume: Optional session ID to resume
        """
        # Inject our permission handler
        options.can_use_tool = self._handle_permission

        self.client = ClaudeSDKClient(options)
        await self.client.connect()

        # Capture the claude process PID for background process tracking
        from claudechic.processes import get_claude_pid_from_client

        self._claude_pid = get_claude_pid_from_client(self.client)

        if resume:
            self.session_id = resume

        # Initialize file index
        self.file_index = FileIndex(root=self.cwd)
        await self.file_index.refresh()

        self._reader_task = asyncio.create_task(
            self._read_messages_forever(),
            name=f"agent-{self.id}-reader",
        )

    async def disconnect(self) -> None:
        """Disconnect and cleanup."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass

        if self.client:
            try:
                # disconnect() terminates gracefully and waits for session flush
                await self.client.disconnect()
            except Exception:
                pass
            self.client = None
        self._claude_pid = None

        # IMPORTANT: This cleanup is critical - do not remove!
        # See .ai-docs/anyio-cancel-scope-bug.md for full explanation.
        # Yields to event loop so task_done callbacks can clean up cancel scopes,
        # then forces cleanup of any remaining stale scopes.
        # Without this, cancelled anyio CancelScopes retain done tasks,
        # causing _deliver_cancellation to spin at ~56k calls/sec (25% CPU).
        await asyncio.sleep(0)
        self._cleanup_stale_cancel_scopes()

    def _cleanup_stale_cancel_scopes(self) -> None:
        """Remove done tasks from cancelled anyio CancelScopes.

        Works around a bug where cancelled scopes keep retrying _deliver_cancellation
        for tasks that are already done, causing 25% CPU spin.
        """
        import gc

        try:
            from anyio._backends._asyncio import CancelScope

            for obj in gc.get_objects():
                if isinstance(obj, CancelScope) and obj._cancel_called:
                    if hasattr(obj, "_tasks"):
                        done = [t for t in obj._tasks if t.done()]
                        for t in done:
                            obj._tasks.discard(t)
        except Exception:
            pass  # Best effort cleanup

    async def load_history(self, cwd: Path | None = None) -> None:
        """Load message history from session file into self.messages.

        This populates Agent.messages from the persisted session,
        making Agent.messages the single source of truth for history.
        Call ChatView._render_full() after this to update UI.

        Args:
            cwd: Working directory for session lookup (defaults to self.cwd)
        """
        from claudechic.sessions import load_session_messages

        if not self.session_id:
            return

        self.messages.clear()
        self.checkpoint_uuids.clear()  # Clear stale UUIDs; SDK will repopulate via replay
        raw_messages = await load_session_messages(self.session_id, cwd=cwd or self.cwd)

        current_assistant: AssistantContent | None = None

        for m in raw_messages:
            if m["type"] == "user":
                # Flush any pending assistant content
                if current_assistant is not None:
                    self.messages.append(
                        ChatItem(role="assistant", content=current_assistant)
                    )
                    current_assistant = None
                # Add user message
                self.messages.append(
                    ChatItem(role="user", content=UserContent(text=m["content"]))
                )
            elif m["type"] == "assistant":
                # Add text block to current assistant content (preserving order)
                if current_assistant is None:
                    current_assistant = AssistantContent()
                current_assistant.blocks.append(TextBlock(text=m["content"]))
            elif m["type"] == "tool_use":
                # Add tool use to current assistant content (preserving order)
                if current_assistant is None:
                    current_assistant = AssistantContent()
                current_assistant.blocks.append(
                    ToolUse(
                        id=m.get("id", ""),
                        name=m["name"],
                        input=m.get("input", {}),
                    )
                )

        # Flush final assistant content
        if current_assistant is not None:
            self.messages.append(ChatItem(role="assistant", content=current_assistant))

        log.info(f"Loaded {len(self.messages)} messages from session {self.session_id}")

    # -----------------------------------------------------------------------
    # Sending messages
    # -----------------------------------------------------------------------

    def attach_image(self, path: Path) -> ImageAttachment | None:
        """Attach an image to the next message.

        Returns ImageAttachment on success, None on failure.
        """
        try:
            data = base64.b64encode(path.read_bytes()).decode()
            media_type = mimetypes.guess_type(str(path))[0] or "image/png"
            img = ImageAttachment(str(path), path.name, media_type, data)
            self.pending_images.append(img)
            return img
        except Exception:
            return None

    def clear_images(self) -> None:
        """Clear pending images."""
        self.pending_images.clear()

    async def send(self, prompt: str, *, display_as: str | None = None) -> None:
        """Send a message; the long-running reader handles the response.

        Args:
            prompt: The prompt to send to Claude
            display_as: Optional shorter text to show in UI instead of full prompt
        """
        if not self.client:
            raise RuntimeError("Agent not connected")

        # Add user message to history (store display text if provided)
        display_text = display_as or prompt
        self.messages.append(
            ChatItem(
                role="user",
                content=UserContent(
                    text=display_text, images=list(self.pending_images)
                ),
            )
        )

        # Notify UI to display user message (pass full image info before clearing)
        if self.observer:
            self.observer.on_prompt_sent(self, display_text, list(self.pending_images))

        self._reset_response_state()
        self._set_status(AgentStatus.BUSY)
        self._interrupted = False  # Clear interrupt flag for new query

        # Prepend plan mode instructions if in plan mode
        if self.permission_mode == "plan":
            prompt = self._get_plan_mode_instructions() + prompt

        # Send the prompt. The reader task (started in connect()) will pick up
        # the response messages as they arrive.
        if self.pending_images:
            message = self._build_message_with_images(prompt)
            if self.client and self.client._transport:
                await self.client._transport.write(json.dumps(message) + "\n")
            self.pending_images.clear()
        else:
            await self.client.query(prompt)

    def _reset_response_state(self) -> None:
        """Clear per-turn state so the next response starts fresh.

        Called from send() at the start of a user-initiated turn, and from the
        reader when it detects a new turn beginning after IDLE (e.g. when a
        ScheduleWakeup fires and the agent resumes work without a user prompt).
        """
        self.response_had_tools = False
        self._current_assistant = None
        self._current_text_buffer = ""
        self._needs_new_message = True
        self._thinking_hidden = False

    async def interrupt(self) -> None:
        """Interrupt current response.

        Tells the SDK to stop the current turn. The reader keeps running and
        will process the SDK's wrap-up messages (ending in ResultMessage),
        which naturally returns the agent to IDLE.
        """
        self._interrupted = True

        if self.client:
            try:
                await self.client.interrupt()
            except Exception:
                pass

    async def _send_followup(self, message: str) -> None:
        """Send a follow-up message after brief delay (for 'do something else' flow)."""
        await asyncio.sleep(0.1)  # Let UI update
        await self.send(message)

    # -----------------------------------------------------------------------
    # Response processing
    # -----------------------------------------------------------------------

    def _get_plan_mode_instructions(self) -> str:
        """Get plan mode instructions with the plan file path.

        Note: These are prepended to every message in plan mode (~400 tokens).
        This is acceptable overhead for ensuring the agent follows the workflow.
        """
        plan_file_info = ""
        if self.plan_path:
            plan_file_info = f"\nPlan file: {self.plan_path}\nYou may ONLY write to this file. All other writes are blocked."
        else:
            plan_file_info = "\nPlan file: Will be created in ~/.claude/plans/ when you first write to it."

        return f"""<system-reminder>
PLAN MODE ACTIVE

Workflow Phases:
1. Initial Understanding - Launch up to 3 Explore agents in parallel to understand the codebase. Focus on comprehension.
2. Design - Launch Plan agent(s) to design implementation based on exploration. Up to 3 agents for complex tasks.
3. Review - Read critical files, ensure alignment with user intent, use AskUserQuestion for clarifications.
4. Final Plan - Write concise but complete plan to the plan file. Include file paths and verification steps.
5. Exit - Call ExitPlanMode when done. Your turn should only end with either AskUserQuestion or ExitPlanMode.

Key Rules:
- Use Explore subagent type in Phase 1
- Don't make large assumptions - ask questions
- Use AskUserQuestion for requirement clarification
- Use ExitPlanMode for plan approval (never ask "is this plan okay?" via text)
- Build plan incrementally by writing/editing the plan file
- Edit, Write, Bash, and NotebookEdit are NOT available (except writing to the plan file)
{plan_file_info}
</system-reminder>
"""

    async def _read_messages_forever(self) -> None:
        """Continuously consume the SDK message stream.

        Lives for the lifetime of the connection so that post-turn messages
        (e.g. from ``ScheduleWakeup``/``Monitor`` after a ``ResultMessage``)
        are observed and surfaced. ``receive_response()`` would terminate
        after the first ``ResultMessage`` and strand them.

        The outer ``while True`` exists for soft recovery: a malformed-JSON
        error breaks the current iterator, but a fresh ``receive_messages()``
        call resumes reading — needed so the recovery message scheduled below
        has a live consumer.
        """
        try:
            while True:
                try:
                    async for message in self.client.receive_messages():  # type: ignore[union-attr]
                        # New turn starting after IDLE (post-turn wakeup).
                        # Wakeup turns observed in practice begin with an
                        # AssistantMessage or StreamEvent; we skip SystemMessage
                        # so the connection-init message doesn't spuriously
                        # flip us into BUSY before any real work begins.
                        if self.status == AgentStatus.IDLE and not isinstance(
                            message, SystemMessage
                        ):
                            self._reset_response_state()
                            self._set_status(AgentStatus.BUSY)

                        try:
                            await self._handle_sdk_message(message)
                        except Exception as e:
                            log.exception("Error handling SDK message")
                            if self.observer:
                                self.observer.on_error(
                                    self, "Error handling message", e
                                )

                        if isinstance(message, ResultMessage):
                            self._flush_current_text()
                            self._set_status(AgentStatus.IDLE)
                            # The interrupt's wrap-up has landed — clear the
                            # flag so future errors aren't silently swallowed.
                            self._interrupted = False
                            # "Do something else" permission flow
                            if self._pending_followup:
                                followup = self._pending_followup
                                self._pending_followup = None
                                create_safe_task(
                                    self._send_followup(followup),
                                    name="send-followup",
                                )

                    # Iterator ended cleanly (connection closed) — stop reading.
                    break

                except asyncio.CancelledError:
                    raise
                except CLIJSONDecodeError as e:
                    # Soft recovery: send the error back to Claude and restart
                    # the iterator so the retry's response is consumed.
                    log.warning("CLIJSONDecodeError: %s", e)
                    if self.observer:
                        self.observer.on_error(self, str(e), e)
                    self._set_status(AgentStatus.IDLE)
                    create_safe_task(
                        self._send_followup(f"Error: {e}"),
                        name="json-decode-retry",
                    )
                    continue
                except Exception as e:
                    error_type = type(e).__name__
                    error_str = str(e).lower()
                    is_connection_error = (
                        "ConnectionError" in error_type
                        or "BrokenPipeError" in error_type
                        or ("connection" in error_str and "api" not in error_str)
                    )

                    if self._interrupted:
                        log.info("Suppressed error after interrupt: %s", e)
                    else:
                        log.exception("Reader failed")
                        if self.observer:
                            self.observer.on_error(self, "Response failed", e)

                    if is_connection_error and self.observer:
                        self.observer.on_connection_lost(self)

                    if self.observer:
                        self.observer.on_complete(self, None)
                    break
        finally:
            self._flush_current_text()
            self._set_status(AgentStatus.IDLE)

    async def _handle_sdk_message(self, message: Any) -> None:
        """Handle a single SDK message."""
        if isinstance(message, AssistantMessage):
            parent_id = message.parent_tool_use_id
            for block in message.content:
                if isinstance(block, ToolUseBlock):
                    self._handle_tool_use(block, parent_id)
                elif isinstance(block, ToolResultBlock):
                    self._handle_tool_result(block)
                elif isinstance(block, SdkTextBlock):
                    # Normally TextBlocks are streamed via StreamEvent deltas
                    # and arrive here pre-rendered — skip duplicates. But local
                    # slash commands (/context, /compact, …) bypass streaming
                    # and deliver their full output as a single TextBlock with
                    # an empty stream buffer. Route those as command output.
                    if (
                        not self._current_text_buffer
                        and self._current_assistant is None
                        and "## Context Usage" in block.text
                        and self.observer
                    ):
                        self.observer.on_command_output(self, block.text)

        elif isinstance(message, UserMessage):
            # Capture UUID for checkpoints (needed for /rewind file restoration)
            if message.uuid:
                self.checkpoint_uuids.append(message.uuid)

            # UserMessage can contain tool results or command output
            content = getattr(message, "content", "")
            if isinstance(content, str):
                # Handle local command output (e.g., /context)
                if "<local-command-stdout>" in content:
                    self._handle_command_output(content)
                # Detect SDK-loaded skills (e.g., /cleanup -> <command-name>/cleanup</command-name>)
                if "<command-name>/" in content:
                    match = re.search(
                        r"<command-name>(/[\w:-]+)</command-name>", content
                    )
                    if match and self.observer:
                        self.observer.on_skill_loaded(self, match.group(1))
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, ToolResultBlock):
                        self._handle_tool_result(block)

        elif isinstance(message, StreamEvent):
            self._handle_stream_event(message)

        elif isinstance(message, SystemMessage):
            # Capture session_id from init message (earlier than ResultMessage)
            if message.subtype == "init" and not self.session_id:
                if isinstance(message.data, dict) and "session_id" in message.data:
                    self.session_id = message.data["session_id"]
            if self.observer:
                self.observer.on_system_message(self, message)

        elif isinstance(message, ResultMessage):
            self._flush_current_text()
            self.session_id = message.session_id
            if self.observer:
                self.observer.on_complete(self, message)

    def _handle_text_chunk(
        self, text: str, new_message: bool, parent_tool_use_id: str | None
    ) -> None:
        """Handle incoming text chunk."""
        # If this belongs to a Task, accumulate there
        if parent_tool_use_id and parent_tool_use_id in self.active_tasks:
            self.active_tasks[parent_tool_use_id] += text
            return

        if new_message:
            self._flush_current_text()

        # Ensure we have an assistant content to accumulate into
        if self._current_assistant is None:
            self._current_assistant = AssistantContent()
            self.messages.append(
                ChatItem(role="assistant", content=self._current_assistant)
            )

        self._current_text_buffer += text
        # Update the current TextBlock in-place for live streaming display
        self._update_current_text_block()
        if self.observer:
            self.observer.on_message_updated(self)
            self.observer.on_text_chunk(self, text, new_message, parent_tool_use_id)

    def _handle_stream_event(self, event: StreamEvent) -> None:
        """Handle streaming event from SDK."""
        ev = event.event
        ev_type = ev.get("type")
        parent_id = event.parent_tool_use_id

        if ev_type == "content_block_delta":
            delta = ev.get("delta", {})
            if delta.get("type") == "text_delta":
                text = delta.get("text", "")
                if text:
                    # Start new message after tool use or at start of response
                    new_msg = self._needs_new_message
                    self._needs_new_message = False
                    self._handle_text_chunk(text, new_msg, parent_id)

    def _update_current_text_block(self) -> None:
        """Update the current TextBlock with accumulated text (for streaming)."""
        if not self._current_assistant or not self._current_text_buffer:
            return
        # Find or create the trailing TextBlock
        if self._current_assistant.blocks and isinstance(
            self._current_assistant.blocks[-1], TextBlock
        ):
            self._current_assistant.blocks[-1].text = self._current_text_buffer
        else:
            self._current_assistant.blocks.append(
                TextBlock(text=self._current_text_buffer)
            )

    def _flush_current_text(self) -> None:
        """Flush accumulated text to current assistant message and reset buffer."""
        if self._current_assistant and self._current_text_buffer:
            self._update_current_text_block()
            self._current_text_buffer = ""
            if self.observer:
                self.observer.on_message_updated(self)

    def _handle_command_output(self, content: str) -> None:
        """Handle command output from UserMessage (e.g., /context)."""
        import re

        # Extract content from <local-command-stdout>...</local-command-stdout>
        match = re.search(
            r"<local-command-stdout>(.*?)</local-command-stdout>", content, re.DOTALL
        )
        if match and self.observer:
            self.observer.on_command_output(self, match.group(1).strip())

    def _handle_tool_use(
        self, block: ToolUseBlock, parent_tool_use_id: str | None
    ) -> None:
        """Handle tool use start."""
        self._flush_current_text()
        self.response_had_tools = True
        self._needs_new_message = True  # Next text chunk starts a new ChatMessage

        # TodoWrite updates todos
        if block.name == ToolName.TODO_WRITE:
            self.todos = block.input.get("todos", [])
            if self.observer:
                self.observer.on_todos_updated(self)
            return

        tool = ToolUse(
            id=block.id,
            name=block.name,
            input=block.input,
            parent_tool_use_id=parent_tool_use_id,
        )

        # Track Task tools specially
        if block.name == ToolName.TASK:
            self.active_tasks[block.id] = ""

        self.pending_tools[block.id] = tool

        # Add to current assistant content
        if self._current_assistant is None:
            self._current_assistant = AssistantContent()
            self.messages.append(
                ChatItem(role="assistant", content=self._current_assistant)
            )
        self._current_assistant.blocks.append(tool)
        if self.observer:
            self.observer.on_message_updated(self)
            self.observer.on_tool_use(self, tool)

    def _handle_tool_result(self, block: ToolResultBlock) -> None:
        """Handle tool result."""
        from claudechic.processes import parse_background_task_output

        tool = self.pending_tools.pop(block.tool_use_id, None)
        if tool:
            tool.result = (
                block.content if isinstance(block.content, str) else str(block.content)
            )
            tool.is_error = block.is_error or False

            # Track background task output files
            if tool.name == ToolName.BASH and tool.result:
                output_file = parse_background_task_output(tool.result)
                if output_file:
                    command = tool.input.get("command", "")
                    self._background_outputs[command] = output_file

            # Update permission mode based on plan mode tools
            if tool.name == ToolName.EXIT_PLAN_MODE and not tool.is_error:
                self._set_permission_mode_local("default")
            elif tool.name == ToolName.ENTER_PLAN_MODE and not tool.is_error:
                self._set_permission_mode_local("plan")
                # Fetch plan path asynchronously (needed for ExitPlanMode later)
                create_safe_task(self.ensure_plan_path(), name="fetch-plan-path")

            if self.observer:
                self.observer.on_message_updated(self)
                self.observer.on_tool_result(self, tool)

        # Clean up active tasks
        self.active_tasks.pop(block.tool_use_id, None)

    # -----------------------------------------------------------------------
    # Permissions
    # -----------------------------------------------------------------------

    async def _handle_permission(
        self,
        tool_name: str,
        tool_input: dict[str, Any],
        context: ToolPermissionContext,  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> PermissionResult:
        """Handle permission request from SDK."""
        log.info(f"Permission requested for {tool_name}: {str(tool_input)[:100]}")

        # AskUserQuestion needs special handling
        if tool_name == ToolName.ASK_USER_QUESTION:
            return await self._handle_ask_user_question(tool_input)

        # Auto-allow EnterPlanMode; ExitPlanMode falls through to normal permission flow
        if tool_name == ToolName.ENTER_PLAN_MODE:
            return PermissionResultAllow()
        if tool_name.startswith("mcp__chic__"):
            return PermissionResultAllow()

        # Block mutating tools in plan mode (except writes to plan file)
        # Note: PreToolUse hook in app.py also blocks these; this is a fallback
        if self.permission_mode == "plan" and tool_name in self.PLAN_MODE_BLOCKED_TOOLS:
            # Allow Write/Edit to files in ~/.claude/plans/
            if tool_name in (ToolName.WRITE, ToolName.EDIT):
                file_path = tool_input.get("file_path", "")
                if file_path:
                    plans_dir = Path.home() / ".claude" / "plans"
                    resolved = Path(file_path).expanduser().resolve()
                    if str(resolved).startswith(str(plans_dir)):
                        self.plan_path = resolved  # Capture for ExitPlanMode display
                        log.info(f"Auto-approved {tool_name} to plan file (plan mode)")
                        return PermissionResultAllow()
            log.info(f"Denied {tool_name} (plan mode)")
            return PermissionResultDeny(
                message=f"{tool_name} is not available in plan mode. Write your plan to the plan file and use ExitPlanMode when ready.",
                interrupt=False,
            )

        # Auto-approve edits if in acceptEdits mode
        if self.permission_mode == "acceptEdits" and tool_name in self.AUTO_EDIT_TOOLS:
            log.info(f"Auto-approved {tool_name} (acceptEdits mode)")
            return PermissionResultAllow()

        # Auto-approve everything in auto mode. The CLI subprocess normally
        # auto-approves in auto mode and never invokes can_use_tool, so this
        # branch is usually unreached — d0feea5 fixed the session_id race
        # that previously left the CLI stuck in the old mode. We keep this
        # local handler as defense-in-depth: if any future race or CLI quirk
        # routes a tool through can_use_tool while permission_mode is "auto",
        # the user's intent (don't prompt) wins.
        if self.permission_mode == "auto":
            log.info(f"Auto-approved {tool_name} (auto mode)")
            return PermissionResultAllow()

        # Auto-approve if tool was allowed for session
        if tool_name in self.session_allowed_tools:
            log.info(f"Auto-approved {tool_name} (session allowed)")
            return PermissionResultAllow()

        # Auto-approve git commands during worktree finish
        if self.finish_state and tool_name == ToolName.BASH:
            command = tool_input.get("command", "")
            if command.startswith("git "):
                log.info(f"Auto-approved git during finish: {command[:50]}")
                return PermissionResultAllow()

        # Create permission request and queue it
        request = PermissionRequest(tool_name, tool_input)
        self.pending_prompts.append(request)
        if self.observer:
            self.observer.on_prompt_added(self, request)

        self._set_status(AgentStatus.NEEDS_INPUT)

        # Wait for UI to respond
        if self.permission_handler:
            result = await self.permission_handler(self, request)
        else:
            # No UI callback - wait for programmatic response
            result = await request.wait()

        # Remove from queue
        if request in self.pending_prompts:
            self.pending_prompts.remove(request)

        self._set_status(AgentStatus.BUSY)

        log.info(f"Permission result: {result.choice}")
        if result.choice == PermissionChoice.ALLOW_ALL:
            self._set_permission_mode_local("acceptEdits")
            return PermissionResultAllow()
        elif result.choice == PermissionChoice.ALLOW_SESSION:
            self.session_allowed_tools.add(tool_name)
            return PermissionResultAllow()
        elif result.choice == PermissionChoice.ALLOW:
            return PermissionResultAllow()
        elif result.choice == PermissionChoice.DENY and result.alternative_message:
            # User provided alternative instructions - don't interrupt so model continues
            return PermissionResultDeny(
                message=result.alternative_message, interrupt=False
            )
        else:
            return PermissionResultDeny(message="User denied permission")

    async def _handle_ask_user_question(
        self, tool_input: dict[str, Any]
    ) -> PermissionResult:
        """Handle AskUserQuestion tool - needs UI to collect answers."""
        questions = tool_input.get("questions", [])
        if not questions:
            return PermissionResultAllow(updated_input=tool_input)

        # Create a special request for question prompts
        request = PermissionRequest(ToolName.ASK_USER_QUESTION, tool_input)
        self.pending_prompts.append(request)
        if self.observer:
            self.observer.on_prompt_added(self, request)

        self._set_status(AgentStatus.NEEDS_INPUT)

        # The UI callback should handle question collection
        if self.permission_handler:
            result = await self.permission_handler(self, request)
        else:
            result = await request.wait()

        if request in self.pending_prompts:
            self.pending_prompts.remove(request)

        self._set_status(AgentStatus.BUSY)

        if result == PermissionChoice.DENY:
            return PermissionResultDeny(message="User cancelled questions")

        # Result should be the answers dict (stored in request._result by UI)
        answers = getattr(request, "_answers", {})
        return PermissionResultAllow(
            updated_input={"questions": questions, "answers": answers}
        )

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _set_status(self, status: AgentStatus) -> None:
        """Update status and emit event."""
        if self.status != status:
            self.status = status
            if self.observer:
                self.observer.on_status_changed(self)

    # Valid permission modes
    PERMISSION_MODES = {"default", "acceptEdits", "plan", "planSwarm", "auto"}

    def _set_permission_mode_local(self, mode: str) -> None:
        """Update permission mode locally without calling SDK.

        Used when SDK already knows (e.g., EnterPlanMode/ExitPlanMode tools).
        """
        assert mode in self.PERMISSION_MODES, f"Invalid permission mode: {mode}"
        if self.permission_mode != mode:
            self.permission_mode = mode
            if self.observer:
                self.observer.on_permission_mode_changed(self)

    async def ensure_plan_path(self) -> None:
        """Fetch and cache the plan path for this session (if not already set)."""
        if self.session_id and not self.plan_path:
            self.plan_path = await get_plan_path_for_session(
                self.session_id, cwd=self.cwd, must_exist=False
            )

    async def set_permission_mode(self, mode: str) -> None:
        """Update permission mode via SDK and emit event.

        Args:
            mode: One of 'default', 'acceptEdits', 'plan'
        """
        assert mode in self.PERMISSION_MODES, f"Invalid permission mode: {mode}"
        if self.permission_mode != mode:
            self.permission_mode = mode
            # Fetch plan path when entering plan mode
            if mode == "plan":
                await self.ensure_plan_path()
            # Push mode to SDK as soon as the subprocess is connected.
            # Do NOT gate on self.session_id: the control request works before
            # the `init` SystemMessage arrives, and gating on session_id caused
            # shift-tab into plan mode right after launch to silently no-op
            # the SDK call — the CLI would then reject ExitPlanMode at
            # validateInput with "you are not in plan mode".
            # "planSwarm" is claudechic-specific; the SDK doesn't know it.
            if self.client and mode != "planSwarm":
                await self.client.set_permission_mode(cast(PermissionMode, mode))
            if self.observer:
                self.observer.on_permission_mode_changed(self)

    def _build_message_with_images(self, prompt: str) -> dict[str, Any]:
        """Build SDK message with text and images."""
        content: list[dict[str, Any]] = []
        if prompt.strip():
            content.append({"type": "text", "text": prompt})
        for img in self.pending_images:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.media_type,
                        "data": img.base64_data,
                    },
                }
            )
        return {
            "type": "user",
            "message": {"role": "user", "content": content},
            "parent_tool_use_id": None,
        }

    def get_background_processes(self) -> list:
        """Get list of background processes for this agent.

        Returns:
            List of BackgroundProcess objects (with output_file if known)
        """
        if not self._claude_pid:
            return []
        from claudechic.processes import get_child_processes

        processes = get_child_processes(self._claude_pid)

        # Enrich with output files if we have them
        for proc in processes:
            if proc.command in self._background_outputs:
                # Create new instance with output_file set
                proc.output_file = self._background_outputs[proc.command]

        return processes
