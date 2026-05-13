"""Claude Code Textual UI - Main application."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import logging
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from claude_agent_sdk.types import HookEvent
    from claudechic.screens.chat import ChatScreen
    from textual.timer import Timer

from textual.app import App
from textual.screen import Screen

from claudechic.theme import CHIC_THEME, CHIC_LIGHT_THEME, load_custom_themes
from textual.binding import Binding
from textual.containers import Vertical, Horizontal
from textual.events import MouseUp
from textual import work

from claude_agent_sdk import (
    CLIConnectionError,
    ClaudeSDKClient,
    ClaudeAgentOptions,
    SystemMessage,
    ToolUseBlock,
    ResultMessage,
)
from claude_agent_sdk.types import HookMatcher
from claudechic.messages import (
    ResponseComplete,
    SystemNotification,
    ToolUseMessage,
    ToolResultMessage,
    CommandOutputMessage,
)
from claudechic.sessions import (
    find_session_by_prefix,
    get_plan_path_for_session,
    get_recent_sessions,
)
from claudechic.features.diff import EditFileRequested
from claudechic.features.worktree import list_worktrees
from claudechic.commands import BARE_WORDS, handle_command
from claudechic.features.worktree.commands import on_response_complete_finish
from claudechic.permissions import PermissionRequest, PermissionResponse
from claudechic.agent import (
    Agent,
    ImageAttachment,
    ToolUse,
    get_default_permission_mode,
)
from claudechic.agent_manager import AgentManager
from claudechic.analytics import capture
from claudechic.config import CONFIG, NEW_INSTALL, save as save_config
from claudechic.enums import AgentStatus, PermissionChoice, ToolName
from claudechic.mcp import set_app, create_chic_server
from claudechic.file_index import FileIndex
from claudechic.formatting import trim_model_name
from claudechic.history import append_to_history
from claudechic.widgets import (
    ContextBar,
    ChatMessage,
    ChatInput,
    ConnectingIndicator,
    ImageAttachments,
    ErrorMessage,
    AgentToolWidget,
    TodoWidget,
    TodoPanel,
    ProcessPanel,
    ReviewPanel,
    SelectionPrompt,
    QuestionPrompt,
    TextAreaAutoComplete,
    HistorySearch,
    AgentSection,
    AgentItem,
    WorktreeItem,
    ChatView,
    PlanItem,
    PlanSection,
    FileItem,
    FilesSection,
    HamburgerButton,
    EditPlanRequested,
    PendingShellWidget,
)
from claudechic.widgets.layout.footer import (
    PermissionModeLabel,
    ModelLabel,
    EffortLabel,
    StatusFooter,
)
from claudechic.widgets.prompts import EffortPrompt, ModelPrompt
from claudechic.errors import setup_logging  # noqa: F401 - used at startup
from claudechic.errors import set_notify_callback as set_log_notify_callback
from claudechic.profiling import profile
from claudechic.tasks import create_safe_task
from claudechic.sampling import start_sampler, get_sampler

log = logging.getLogger(__name__)

# Pattern to strip SDK's <tool_use_error> tags from error messages
TOOL_USE_ERROR_PATTERN = re.compile(r"</?tool_use_error>")

# Friendly labels for the permission modes the UI can land in. Used by the
# Shift+Tab cycle and the plan-exit fallback notify so messages stay
# consistent (e.g. ``acceptEdits`` shows as "Auto-edit", not "Acceptedits").
_PERMISSION_MODE_LABELS = {
    "default": "Default",
    "acceptEdits": "Auto-edit",
    "plan": "Plan",
    "auto": "Auto",
}


def _plan_mode_pre_tool_use_decision(hook_input: dict) -> dict:
    """PreToolUse hook decision for plan mode.

    Pure function — extracted from the closure in ``_plan_mode_hooks`` so
    it can be unit-tested directly. Returns the SDK hook output:

    - ``{}`` to allow the tool to proceed through normal permission flow.
    - A ``hookSpecificOutput`` deny payload (per
      ``claude_agent_sdk.types.PreToolUseHookSpecificOutput``) to block.

    Bash is deliberately not blocked: the CLI's plan mode prompt instructs
    the model to avoid "non-readonly tools," but read-only Bash (ls, find,
    git status, etc.) is legitimate during exploration — blocking it all
    trips up Explore subagents that need Bash for read-only ops.
    """
    blocked_tools = {"Edit", "Write", "NotebookEdit"}
    plans_dir = Path.home() / ".claude" / "plans"

    permission_mode = hook_input.get("permission_mode", "default")
    tool_name = hook_input.get("tool_name", "")
    tool_input = hook_input.get("tool_input", {})

    if permission_mode != "plan" or tool_name not in blocked_tools:
        return {}

    # Allow Write/Edit to files under ~/.claude/plans/
    if tool_name in ("Write", "Edit"):
        file_path = tool_input.get("file_path", "")
        if file_path:
            resolved = Path(file_path).expanduser().resolve()
            if resolved.is_relative_to(plans_dir):
                return {}

    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"{tool_name} is not available in plan mode. "
                "Write your plan to the plan file and use "
                "ExitPlanMode when ready."
            ),
        },
    }


def _categorize_cli_error(e: CLIConnectionError) -> str:
    """Categorize CLI connection error without exposing user paths."""
    msg = str(e)
    if "Working directory does not exist" in msg:
        return "cwd_not_found"
    if "not ready for writing" in msg:
        return "not_ready"
    if "terminated process" in msg:
        return "process_terminated"
    if "Not connected" in msg:
        return "not_connected"
    if "Failed to start" in msg:
        return "start_failed"
    if "not found" in msg.lower():
        return "cli_not_found"
    return "unknown"


class ChatApp(App):
    """Main chat application.

    Implements AgentManagerObserver and AgentObserver protocols for
    UI integration with AgentManager.
    """

    TITLE = "Claude Chic"
    CSS_PATH = Path(__file__).parent / "styles.tcss"

    BINDINGS = [
        Binding("ctrl+y", "copy_selection", "Copy", priority=True, show=False),
        Binding("ctrl+c", "quit", "Quit", priority=True, show=False),
        Binding("ctrl+s", "screenshot", "Screenshot", show=False),
        # Agent switching: ctrl+1 through ctrl+9
        *[
            Binding(
                f"ctrl+{i}",
                f"switch_agent({i})",
                f"Agent {i}",
                priority=True,
                show=False,
            )
            for i in range(1, 10)
        ],
    ]

    # Auto-approve Edit/Write tools (but still prompt for Bash, etc.)
    AUTO_EDIT_TOOLS = {ToolName.EDIT, ToolName.WRITE}

    # Width thresholds for layout (sidebar=28, min chat=80)
    SIDEBAR_MIN_WIDTH = 110  # Below this, hide sidebar
    CENTERED_SIDEBAR_WIDTH = 140  # Above this, center chat while showing sidebar

    def __init__(
        self,
        resume_session_id: str | None = None,
        initial_prompt: str | None = None,
        remote_port: int = 0,
        skip_permissions: bool = False,
        theme_override: str | None = None,
    ) -> None:
        super().__init__()
        self.scroll_sensitivity_y = 1.0  # Smoother scrolling (default is 2.0)
        # AgentManager is the single source of truth for agents
        self.agent_mgr: AgentManager | None = None

        self._resume_on_start = resume_session_id
        self._initial_prompt = initial_prompt
        self._remote_port = remote_port
        self._skip_permissions = skip_permissions
        self._theme_override = theme_override
        # Event queues for testing
        self.interactions: asyncio.Queue[PermissionRequest] = asyncio.Queue()
        self.completions: asyncio.Queue[ResponseComplete] = asyncio.Queue()
        # File index for fuzzy file search
        self.file_index: FileIndex | None = None
        # Cached widget references (initialized lazily)
        self._agent_section: AgentSection | None = None
        self._plan_section: PlanSection | None = None
        self._files_section: FilesSection | None = None
        self._todo_panel: TodoPanel | None = None
        self._review_panel: ReviewPanel | None = None
        self._process_panel: ProcessPanel | None = None
        self._context_bar: ContextBar | None = None
        self._right_sidebar: Vertical | None = None
        self._input_container: Vertical | None = None
        self._chat_input: ChatInput | None = None
        self._status_footer: StatusFooter | None = None
        # Track running shell command for Ctrl+C cancellation
        self._shell_process: asyncio.subprocess.Process | None = None
        # Pending shell cancel handlers (widget_id -> callback)
        self._pending_shell_cancels: dict[int, Any] = {}
        # Agent-to-UI mappings (Agent has no UI references)
        self._chat_views: dict[str, ChatView] = {}  # agent_id -> ChatView
        self._agent_metadata: dict[
            str, dict
        ] = {}  # agent_id -> {created_at, same_directory}
        self._active_prompts: dict[
            str, Any
        ] = {}  # agent_id -> SelectionPrompt/QuestionPrompt
        # Sidebar overlay state (for narrow screens)
        self._sidebar_overlay_open = False
        self._hamburger_btn: HamburgerButton | None = None
        # Available models from SDK (populated in _update_slash_commands)
        self._available_models: list[dict] = []
        # Track pending slash commands passed to Claude (for typo detection)
        # agent_id -> command name (e.g., "/cleanup")
        self._pending_slash_commands: dict[str, str] = {}

    def _fatal_error(self) -> None:
        """Override to use plain Python tracebacks instead of rich's fancy ones."""
        import traceback

        self.bell()
        # Store plain text traceback for display after exit
        self._exit_renderables.append(traceback.format_exc())
        self._close_messages_no_wait()

    # Properties to access active agent's state
    @property
    def _agent(self) -> Agent | None:
        """Get the active agent."""
        return self.agent_mgr.active if self.agent_mgr else None

    @property
    def agents(self) -> dict[str, Agent]:
        """Get all agents dict (from AgentManager)."""
        return self.agent_mgr.agents if self.agent_mgr else {}

    @property
    def active_agent_id(self) -> str | None:
        """Get active agent ID (from AgentManager)."""
        return self.agent_mgr.active_id if self.agent_mgr else None

    @property
    def client(self) -> ClaudeSDKClient | None:
        return self._agent.client if self._agent else None

    @client.setter
    def client(self, value: ClaudeSDKClient | None) -> None:
        if self._agent:
            self._agent.client = value

    @property
    def session_id(self) -> str | None:
        return self._agent.session_id if self._agent else None

    @session_id.setter
    def session_id(self, value: str | None) -> None:
        if self._agent:
            self._agent.session_id = value

    @property
    def sdk_cwd(self) -> Path:
        return self._agent.cwd if self._agent else Path.cwd()

    @sdk_cwd.setter
    def sdk_cwd(self, value: Path) -> None:
        if self._agent:
            self._agent.cwd = value

    @property
    def _chat_view(self) -> ChatView | None:
        """Get the active agent's chat view."""
        if self._agent:
            return self._chat_views.get(self._agent.id)
        return None

    def _get_chat_view(self, agent_id: str | None) -> ChatView | None:
        """Get chat view for an agent by ID."""
        if agent_id:
            return self._chat_views.get(agent_id)
        return self._chat_view

    def _get_agent(self, agent_id: str | None) -> Agent | None:
        """Get agent by ID, or active agent if None."""
        if self.agent_mgr is None:
            return None
        return self.agent_mgr.get(agent_id)

    def _track_edited_file(self, tool: ToolUse, file_path: Path) -> None:
        """Track an edited file in the sidebar."""
        from claudechic.formatting import make_relative

        # Calculate additions/deletions from tool input
        additions = 0
        deletions = 0
        if tool.name == "Edit":
            old_str = tool.input.get("old_string", "")
            new_str = tool.input.get("new_string", "")
            deletions = old_str.count("\n") + (1 if old_str else 0)
            additions = new_str.count("\n") + (1 if new_str else 0)
        elif tool.name == "Write":
            content = tool.input.get("content", "")
            additions = content.count("\n") + (1 if content else 0)

        # Make path relative to agent's working directory
        agent = self.agent_mgr.active if self.agent_mgr else None
        if not agent:
            return
        cwd = Path(agent.cwd)
        rel_path = Path(make_relative(str(file_path), cwd))

        try:
            self.files_section.add_file(rel_path, additions, deletions)
        except Exception:
            pass  # Widget may not exist yet

    # Cached widget accessors (lazy init on first access)
    @property
    def agent_section(self) -> AgentSection:
        if self._agent_section is None:
            self._agent_section = self.query_one("#agent-section", AgentSection)
        return self._agent_section

    @property
    def plan_section(self) -> PlanSection:
        if self._plan_section is None:
            self._plan_section = self.query_one("#plan-section", PlanSection)
        return self._plan_section

    @property
    def files_section(self) -> FilesSection:
        if self._files_section is None:
            self._files_section = self.query_one("#files-section", FilesSection)
        return self._files_section

    async def _async_refresh_files(self, agent: Agent) -> None:
        """Refresh files section from git for the given agent's directory."""
        from claudechic.features.diff import get_file_stats

        try:
            section = self.files_section
            # Clear existing file items (not the title)
            await section.async_clear()

            # Fetch fresh from git
            stats = await get_file_stats(str(agent.cwd))
            if stats:
                files = {
                    Path(s.path): (s.additions, s.deletions, s.untracked) for s in stats
                }
                section.mount_all_files(files)
            else:
                section.add_class("hidden")
        except Exception:
            # Hide on error (not a git repo, etc)
            try:
                self.files_section.add_class("hidden")
            except Exception:
                pass

    @property
    def todo_panel(self) -> TodoPanel:
        if self._todo_panel is None:
            self._todo_panel = self.query_one("#todo-panel", TodoPanel)
        return self._todo_panel

    @property
    def review_panel(self) -> ReviewPanel:
        if self._review_panel is None:
            self._review_panel = self.query_one("#review-panel", ReviewPanel)
        return self._review_panel

    @property
    def process_panel(self) -> ProcessPanel:
        if self._process_panel is None:
            self._process_panel = self.query_one("#process-panel", ProcessPanel)
        return self._process_panel

    @property
    def context_bar(self) -> ContextBar:
        if self._context_bar is None:
            self._context_bar = self.query_one("#context-bar", ContextBar)
        return self._context_bar

    @property
    def right_sidebar(self) -> Vertical:
        if self._right_sidebar is None:
            self._right_sidebar = self.query_one("#right-sidebar", Vertical)
        return self._right_sidebar

    @property
    def input_container(self) -> Vertical:
        if self._input_container is None:
            self._input_container = self.query_one("#input-container", Vertical)
        return self._input_container

    @property
    def chat_input(self) -> ChatInput:
        if self._chat_input is None:
            self._chat_input = self.query_one("#input", ChatInput)
        return self._chat_input

    @property
    def status_footer(self) -> StatusFooter:
        if self._status_footer is None:
            self._status_footer = self.query_one(StatusFooter)
        return self._status_footer

    @property
    def hamburger_btn(self) -> HamburgerButton:
        if self._hamburger_btn is None:
            self._hamburger_btn = self.query_one("#hamburger-btn", HamburgerButton)
        return self._hamburger_btn

    def _set_agent_status(
        self, status: AgentStatus, agent_id: str | None = None
    ) -> None:
        """Update an agent's status and sidebar display."""
        agent = self._get_agent(agent_id)
        if not agent:
            return
        agent.status = status
        try:
            self.agent_section.update_status(agent.id, status)
        except Exception:
            pass  # Sidebar not mounted yet

    def show_error(self, message: str, exception: Exception | None = None) -> None:
        """Display an error message in the chat view and log to file.

        Args:
            message: Brief description of what failed
            exception: Optional exception for logging (full traceback logged to file)
        """
        chat_view = self._chat_view
        if chat_view:
            error_widget = ErrorMessage(message, exception)
            chat_view.mount(error_widget)
            self.call_after_refresh(chat_view.scroll_if_tailing)
        # Also show toast for visibility
        self.notify(message, severity="error")

    async def _replace_client(self, options: ClaudeAgentOptions) -> None:
        """Safely replace current client with a new one."""
        # Cancel any permission prompts waiting for user input
        for prompt in list(self.query(SelectionPrompt)) + list(
            self.query(QuestionPrompt)
        ):
            prompt.cancel()
        old = self.client
        self.client = None
        if old:
            try:
                await old.interrupt()
            except Exception:
                pass
            # Skip disconnect() - it causes race conditions with SDK cleanup.
            # interrupt() is sufficient to stop the subprocess.
        new_client = ClaudeSDKClient(options)
        await new_client.connect()
        self.client = new_client

    def _attach_image(self, path: Path) -> None:
        """Read and queue image for next message on active agent."""
        agent = self._agent
        if not agent:
            self.notify("No active agent", severity="error")
            return
        img = agent.attach_image(path)
        if img:
            self.query_one("#image-attachments", ImageAttachments).add_image(path.name)
        else:
            self.notify(f"Failed to attach {path.name}", severity="error")

    def on_image_attachments_removed(self, event: ImageAttachments.Removed) -> None:
        """Handle removal of an image attachment from active agent."""
        agent = self._agent
        if agent:
            agent.pending_images = [
                img for img in agent.pending_images if img.filename != event.filename
            ]

    @asynccontextmanager
    async def _show_prompt(self, prompt, agent: Agent | None = None):
        """Show a prompt widget, hiding input container. Restores on exit.

        If agent is provided, the prompt is associated with that agent and only
        shown when that agent is active. If agent is None, uses the currently
        active agent.
        """
        if agent is None:
            agent = self._agent
        if agent:
            self._active_prompts[agent.id] = prompt

        # Mount prompt as sibling of input-container (after it in chat-column)
        is_active = agent is None or agent.id == self.active_agent_id
        self.query_one("#chat-column").mount(prompt)
        if is_active:
            self.input_container.add_class("hidden")
        else:
            prompt.add_class("hidden")
        try:
            yield prompt
        finally:
            if agent:
                self._active_prompts.pop(agent.id, None)
            try:
                prompt.remove()
            except Exception:
                pass  # Prompt may already be removed
            # Restore input if this agent is now active (user may have switched)
            if agent is None or agent.id == self.active_agent_id:
                self.input_container.remove_class("hidden")

    def action_cycle_permission_mode(self) -> None:
        """Cycle permission mode: default -> acceptEdits -> plan -> auto -> default.

        Skips modes the SDK has rejected for this agent (cached in
        ``agent.unsupported_modes``) — e.g. ``"auto"`` is Opus-only, so on
        Sonnet/Haiku the CLI rejects it and we never offer it again on this
        connection. If the SDK rejects on the first attempt of an
        unknown-unsupported mode, we advance to the next candidate so a
        single Shift+Tab always moves the user forward.
        """
        if not self._agent:
            return
        agent = self._agent  # Capture for closure
        modes = ["default", "acceptEdits", "plan", "auto"]
        # If we're sitting in a non-UI mode (bypassPermissions, dontAsk),
        # treat "before default" as the cursor so we still advance somewhere sane.
        try:
            cursor = modes.index(agent.permission_mode)
        except ValueError:
            cursor = -1

        async def set_mode():
            for offset in range(1, len(modes) + 1):
                candidate = modes[(cursor + offset) % len(modes)]
                if candidate == agent.permission_mode:
                    continue
                if candidate in agent.unsupported_modes:
                    continue
                try:
                    await agent.set_permission_mode(candidate)
                except ValueError as e:
                    # SDK rejected — set_permission_mode has cached it in
                    # unsupported_modes. Try the next one in the cycle so
                    # the user's Shift+Tab moves forward instead of bouncing.
                    self.notify(
                        f"{_PERMISSION_MODE_LABELS[candidate]} mode unavailable: {e}",
                        severity="warning",
                    )
                    continue
                self.notify(f"Mode: {_PERMISSION_MODE_LABELS[candidate]}")
                return
            self.notify("No alternate permission mode available", severity="warning")

        self.run_worker(set_mode(), exclusive=False)

    async def _set_mode_with_fallback(
        self,
        agent: Agent,
        target: str,
        fallback: str = "acceptEdits",
    ) -> str:
        """Set ``target`` permission mode; on SDK rejection, fall back to ``fallback``.

        Used at plan-exit / plan-execution seams where the UX intent is
        "don't make me approve every edit." If the chosen mode (typically
        ``"auto"``) isn't supported on the active model, we drop to
        ``acceptEdits`` rather than crashing or silently denying. Returns
        the mode actually in effect after the call.

        If ``target`` is already in ``agent.unsupported_modes`` (we've seen
        the SDK reject it earlier this connection — e.g. user Shift+Tab'd
        into auto on Sonnet, then exits plan mode picking "auto"), skip
        the round-trip and go straight to ``fallback`` with the same notice.
        """
        target_label = _PERMISSION_MODE_LABELS.get(target, target)
        fallback_label = _PERMISSION_MODE_LABELS[fallback]

        async def apply_fallback() -> str:
            if fallback == agent.permission_mode:
                return fallback
            try:
                await agent.set_permission_mode(fallback)
                return fallback
            except ValueError:
                return agent.permission_mode

        if target in agent.unsupported_modes:
            self.notify(
                f"{target_label} mode unavailable on this model; "
                f"using {fallback_label}.",
                severity="warning",
            )
            return await apply_fallback()

        try:
            await agent.set_permission_mode(target)
            return target
        except ValueError as e:
            self.notify(
                f"{target_label} mode unavailable ({e}); using {fallback_label}.",
                severity="warning",
            )
            return await apply_fallback()

    def _update_footer_permission_mode(self) -> None:
        """Update footer to reflect current agent's permission mode."""
        try:
            self.status_footer.permission_mode = (
                self._agent.permission_mode if self._agent else "default"
            )
        except Exception:
            pass  # Footer may not be mounted yet

    # Built-in slash commands (imported from single source of truth)
    @property
    def LOCAL_COMMANDS(self) -> list[str]:
        from claudechic.commands import get_autocomplete_commands

        return get_autocomplete_commands()

    def get_default_screen(self) -> Screen:
        """Return the chat screen as the default."""
        from claudechic.screens.chat import ChatScreen

        return ChatScreen(slash_commands=self.LOCAL_COMMANDS)

    def _handle_sdk_stderr(self, message: str) -> None:
        """Handle SDK stderr output by showing in chat."""
        message = message.strip()
        if not message:
            return
        self._show_system_info(message, "warning", None)

    def _plan_mode_hooks(self) -> "dict[HookEvent, list[HookMatcher]]":
        """Register the plan-mode PreToolUse hook with the SDK."""

        async def block_mutating_tools(
            hook_input: dict,
            match: str | None,  # noqa: ARG001
            ctx: object,  # noqa: ARG001
        ) -> dict:
            return _plan_mode_pre_tool_use_decision(hook_input)

        return {
            "PreToolUse": [HookMatcher(matcher=None, hooks=[block_mutating_tools])],  # type: ignore[arg-type]
        }

    def _make_options(
        self,
        cwd: Path | None = None,
        resume: str | None = None,
        agent_name: str | None = None,
        model: str | None = None,
        effort: str | None = None,
    ) -> ClaudeAgentOptions:
        """Create SDK options with common settings.

        Note: can_use_tool is set by Agent.connect() to its own handler,
        which routes to permission_ui_callback set by AgentManager.
        """
        # Override ANTHROPIC_API_KEY to prefer subscription auth,
        # unless ANTHROPIC_BASE_URL is set (SSO proxy needs the key)
        env: dict[str, str] = {}
        if not os.environ.get("ANTHROPIC_BASE_URL"):
            env["ANTHROPIC_API_KEY"] = ""
        # Clear VIRTUAL_ENV so agents in worktrees use their own venv
        if os.environ.get("VIRTUAL_ENV"):
            env["VIRTUAL_ENV"] = ""
        # Override the SDK's default entrypoint of "sdk-py". Claude Code's
        # /resume picker filters out sessions whose entrypoint is in
        # {"sdk-cli", "sdk-ts", "sdk-py"}, so claudechic-created sessions
        # otherwise wouldn't appear when the user runs `claude` directly.
        env["CLAUDE_CODE_ENTRYPOINT"] = "claudechic"

        return ClaudeAgentOptions(
            permission_mode="bypassPermissions"
            if self._skip_permissions
            else get_default_permission_mode(cwd or Path.cwd()),
            env=env,
            setting_sources=["user", "project", "local"],
            cwd=cwd,
            resume=resume,
            model=model,
            effort=effort,  # type: ignore[arg-type]  # SDK Literal lags CLI (missing "xhigh")
            mcp_servers={"chic": create_chic_server(caller_name=agent_name)},
            include_partial_messages=True,
            stderr=self._handle_sdk_stderr,
            hooks=self._plan_mode_hooks(),
            enable_file_checkpointing=True,
            extra_args={"replay-user-messages": None},
        )

    async def on_mount(self) -> None:
        # Track app start (and install if new user)
        self._app_start_time = time.time()
        if NEW_INSTALL:
            self.run_worker(capture("app_installed"))
        self.run_worker(capture("app_started", resumed=bool(self._resume_on_start)))

        # Set up notification callback for log messages (warnings and errors)
        set_log_notify_callback(
            lambda msg, severity: self.notify(msg, severity=severity, timeout=5)
        )

        # Warn on washed-out colors over SSH (COLORTERM rarely propagates,
        # so Textual falls back to 256-color). Skip locally to avoid nagging
        # users whose terminal genuinely can't do truecolor.
        is_ssh = any(
            os.environ.get(v) for v in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
        )
        color_system = self.console.color_system
        log.debug(
            "color_system=%s TERM=%s COLORTERM=%s SSH=%s",
            color_system,
            os.environ.get("TERM"),
            os.environ.get("COLORTERM"),
            is_ssh,
        )
        if is_ssh and color_system != "truecolor":
            log.warning(
                "Colors look washed out? Terminal is %s, not truecolor. "
                "Try `export COLORTERM=truecolor` on the remote host.",
                color_system or "unknown",
            )

        # Start CPU sampling profiler + event loop lag monitor
        start_sampler()
        self._monitor_lag_task = create_safe_task(
            self._monitor_event_loop_lag(), name="event-loop-lag"
        )

        # Preload shell executable cache in background
        from claudechic.shell_complete import preload_executables

        preload_executables()

        # Start background process polling
        self.set_interval(2.0, self._poll_background_processes)

        # Register app for MCP tools
        set_app(self)

        # Start remote control server if requested
        if self._remote_port:
            from claudechic.remote import start_server

            await start_server(self, self._remote_port)

        # Register themes (chic default + light variant + user-defined from config)
        self.register_theme(CHIC_THEME)
        self.register_theme(CHIC_LIGHT_THEME)
        for theme in load_custom_themes():
            self.register_theme(theme)
        self.theme = self._theme_override or CONFIG.get("theme") or "chic"

        # Warn if running in YOLO mode
        if self._skip_permissions:
            self.notify("⚠️ Permission checks disabled", severity="warning", timeout=5)

        # Initialize AgentManager (but don't create agent yet - wait for screen ready)
        self.agent_mgr = AgentManager(self._make_options)
        self._wire_agent_manager_callbacks()

        # Initialize file index for fuzzy file search (doesn't need widgets)
        self._cwd = Path.cwd()
        self.file_index = FileIndex(root=self._cwd)
        self._refresh_file_index()

    async def _monitor_event_loop_lag(self) -> None:
        """Continuously measure event loop lag for episode diagnostics."""
        sampler = get_sampler()
        if not sampler:
            return
        metrics = sampler.async_metrics
        while True:
            t0 = time.perf_counter()
            await asyncio.sleep(0.05)
            lag = time.perf_counter() - t0 - 0.05
            if lag > 0.001:
                metrics.record_lag(lag)

    def on_chat_screen_ready(self, event: ChatScreen.Ready) -> None:
        """Handle chat screen ready - now safe to create agent and access widgets."""
        if self.agent_mgr is None:
            return

        # Create initial agent (now that widgets are mounted)
        self.agent_mgr.create_unconnected(name=self._cwd.name, cwd=self._cwd)

        # Populate ghost worktrees (feature branches only)
        self._populate_worktrees()

        # Focus input immediately - UI is ready
        self.chat_input.focus()

        # Initialize vi mode if enabled in config
        if CONFIG.get("vi-mode"):
            self._update_vi_mode(True)

        # Refresh roborev reviews for initial agent
        agent = self._agent
        if agent:
            self._refresh_reviews(agent)

        # Connect SDK in background - UI renders while this happens
        self._connect_initial_client()

    def watch_theme(self, theme: str) -> None:
        """Save theme preference when changed (skip if overridden by CLI flag)."""
        if self._theme_override:
            return
        if theme != CONFIG.get("theme"):
            CONFIG["theme"] = theme
            save_config()

    @work(exclusive=True, group="connect")
    async def _connect_initial_client(self) -> None:
        """Connect SDK for the initial agent."""
        if self.agent_mgr is None or self.agent_mgr.active is None:
            return

        agent = self.agent_mgr.active

        # Show connecting status
        self.status_footer.model = "connecting..."

        # Resolve resume ID (handle __most_recent__ sentinel or prefix from CLI)
        resume = self._resume_on_start
        if resume == "__most_recent__":
            sessions = await get_recent_sessions(limit=1)
            resume = sessions[0][0] if sessions else None
        elif resume:
            # Support prefix matching (e.g., first 8 chars)
            resolved = find_session_by_prefix(resume, cwd=agent.cwd)
            if resolved:
                resume = resolved
            else:
                self.notify(f"No unique session matching '{resume}'", severity="error")
                resume = None

        # Connect the agent to SDK
        options = self._make_options(
            cwd=agent.cwd,
            resume=resume,
            agent_name=agent.name,
            model=agent.model,
            effort=agent.effort,
        )
        try:
            await agent.connect(options, resume=resume)
        except CLIConnectionError as e:
            await capture(
                "error_occurred",
                error_type="CLIConnectionError",
                error_subtype=_categorize_cli_error(e),
                context="initial_connect",
            )
            self.exit(
                message=f"Connection failed: {e}\n\nPlease run `claude /login` to authenticate."
            )
            return
        except Exception as e:
            # Catch-all (e.g. SDK "Control request timeout: initialize") so
            # the user sees a clean message rather than a crashing traceback.
            await capture(
                "error_occurred",
                error_type=type(e).__name__,
                error_subtype=str(e)[:200],
                context="initial_connect",
            )
            self.exit(message=f"Connection failed: {e}")
            return

        # Load history if resuming
        if resume:
            await self._load_and_display_history(resume)
            self.notify(f"Resuming {resume[:8]}...")

        # Fetch SDK commands and update autocomplete
        await self._update_slash_commands()

        # Send initial prompt if provided
        if self._initial_prompt:
            self._send_initial_prompt()

    async def _update_slash_commands(self) -> None:
        """Fetch available commands from SDK and update autocomplete."""
        try:
            if not self.client:
                return
            info = await self.client.get_server_info()
            if not info:
                return
            sdk_commands = ["/" + cmd["name"] for cmd in info.get("commands", [])]
            all_commands = self.LOCAL_COMMANDS + sdk_commands
            autocomplete = self.query_one(TextAreaAutoComplete)
            autocomplete.slash_commands = all_commands
            # Update footer with model info and store available models
            if "models" in info:
                models = info["models"]
                if isinstance(models, list) and models:
                    self._available_models = models
                    # Update footer with current agent's model
                    agent = self._agent
                    self._update_footer_model(agent.model if agent else None)
                    self.status_footer.effort = (agent.effort if agent else None) or ""
        except Exception as e:
            log.warning(f"Failed to fetch SDK commands: {e}")
        self._refresh_dynamic_completions()
        self.refresh_context()

    def _refresh_dynamic_completions(self) -> None:
        """Refresh autocomplete with current agents and worktrees."""
        autocomplete = self.query_one_optional(TextAreaAutoComplete)
        if not autocomplete:
            return

        # Start from current commands (may include SDK commands)
        base = (
            list(autocomplete.slash_commands)
            if autocomplete.slash_commands
            else list(self.LOCAL_COMMANDS)
        )

        # Remove old dynamic completions (keep static worktree commands from COMMANDS)
        from claudechic.commands import COMMANDS

        static_worktree = set(
            next(
                (variants for name, _, variants in COMMANDS if name == "/worktree"), []
            )
        )
        base = [
            c
            for c in base
            if not c.startswith("/agent ")
            and (not c.startswith("/worktree ") or c in static_worktree)
        ]

        # Add current agents
        if self.agent_mgr:
            for agent in self.agent_mgr.agents.values():
                base.append(f"/agent {agent.name}")
                base.append(f"/agent close {agent.name}")

        # Add current worktrees
        try:
            from claudechic.features.worktree import list_worktrees

            for wt in list_worktrees():
                if not wt.is_main:
                    base.append(f"/worktree {wt.branch}")
        except Exception:
            pass  # Not a git repo or git not available

        autocomplete.slash_commands = base

    @work(exclusive=True, group="file_index")
    async def _refresh_file_index(self) -> None:
        """Refresh the file index in the background."""
        if self.file_index:
            await self.file_index.refresh()

    def _poll_background_processes(self) -> None:
        """Poll for background processes and update the panel and footer."""
        agent = self._agent
        if not agent:
            return
        processes = agent.get_background_processes()
        self.process_panel.update_processes(processes)
        self.status_footer.update_processes(processes)
        self._position_right_sidebar()

    _review_poll_timer: Timer | None = None
    _review_poll_agent_id: str | None = None  # agent that owns the poll timer

    @work(exclusive=True, group="refresh_reviews", exit_on_error=False)
    async def _refresh_reviews(self, agent: Agent) -> None:
        """Fetch roborev reviews for the agent's branch and update the sidebar panel."""
        import asyncio

        from claudechic.features.roborev import get_current_branch, list_reviews

        cwd = agent.cwd
        branch = await asyncio.to_thread(get_current_branch, cwd)
        reviews = await asyncio.to_thread(list_reviews, cwd, branch)

        # Guard: agent may have switched during the await calls above
        if agent is not self._agent:
            return

        self.review_panel.update_reviews(reviews)
        self._position_right_sidebar()

        # Poll while any reviews are still running
        from claudechic.widgets.layout.reviews import has_running_reviews

        self._stop_review_polling(agent.id)
        if has_running_reviews(reviews):
            self._review_poll_agent_id = agent.id
            self._review_poll_timer = self.set_timer(
                5, lambda: self._refresh_reviews(agent)
            )

    def _stop_review_polling(self, agent_id: str | None = None) -> None:
        """Cancel any pending review poll timer.

        If agent_id is given, only stops polling if it belongs to that agent.
        If agent_id is None, stops unconditionally (e.g. on agent switch).
        """
        if self._review_poll_timer is None:
            return
        if agent_id is not None and self._review_poll_agent_id != agent_id:
            return
        self._review_poll_timer.stop()
        self._review_poll_timer = None
        self._review_poll_agent_id = None

    async def _load_and_display_history(
        self, session_id: str, cwd: Path | None = None
    ) -> None:
        """Load session history into agent and render in chat view.

        This uses Agent.messages as the single source of truth.
        """
        agent = self._agent
        if not agent:
            return

        # Set session_id and load history
        agent.session_id = session_id
        await agent.load_history(cwd=cwd)

        # Re-render ChatView from Agent.messages
        chat_view = self._chat_views.get(agent.id)
        if chat_view:
            chat_view._render_full()
            self.call_after_refresh(chat_view.scroll_if_tailing)

    @work(group="refresh_context", exclusive=True)
    async def refresh_context(self) -> None:
        """Update context bar from live SDK context usage.

        Uses ``ClaudeSDKClient.get_context_usage()``, the same data source
        Claude Code's ``/context`` command uses, so the bar always reflects
        the active model's window (including the 1M beta for Opus 4.7).
        """
        agent = self._agent
        if not agent or not agent.client:
            self.context_bar.tokens = 0
            return
        try:
            usage = await agent.client.get_context_usage()
        except Exception as e:
            log.debug(f"get_context_usage failed: {e}")
            return
        raw_max = usage.get("rawMaxTokens")
        if isinstance(raw_max, int) and raw_max > 0:
            self.context_bar.max_tokens = raw_max
        self.context_bar.tokens = usage.get("totalTokens", 0)

    def _send_initial_prompt(self) -> None:
        """Send the initial prompt from CLI args."""
        prompt = self._initial_prompt
        self._initial_prompt = None  # Clear so it doesn't re-send
        if prompt:
            self._handle_prompt(prompt)

    def on_chat_input_submitted(self, event: ChatInput.Submitted) -> None:
        has_text = bool(event.text.strip())
        has_images = bool(self._agent and self._agent.pending_images)
        if not has_text and not has_images:
            return
        self.chat_input.clear()
        self._handle_prompt(event.text)

    def on_chat_input_vi_mode_changed(self, event: ChatInput.ViModeChanged) -> None:
        """Update footer when vi mode changes."""
        enabled = CONFIG.get("vi-mode", False)
        self.status_footer.update_vi_mode(event.mode if enabled else None, enabled)

    def _handle_prompt(self, prompt: str) -> None:
        """Process a prompt - handles local commands or sends to Claude."""
        chat_view = self._chat_view
        if not chat_view:
            return

        # Append to global history
        agent = self._agent
        if agent:
            append_to_history(prompt, agent.cwd, agent.session_id or agent.id)

        # Try slash commands, bang commands, and special keywords first
        stripped = prompt.strip()
        if (
            stripped.startswith("/")
            or stripped.startswith("!")
            or stripped in BARE_WORDS
        ) and handle_command(self, prompt):
            return

        # Track message sent
        self.run_worker(
            capture("message_sent", agent_id=agent.analytics_id if agent else "unknown")
        )

        # User message will be mounted by _on_agent_prompt_sent callback
        self._send_to_active_agent(prompt)

    def _send_to_active_agent(
        self, prompt: str, *, display_as: str | None = None
    ) -> None:
        """Send prompt to active agent using Agent.send().

        Args:
            prompt: Full prompt to send to Claude
            display_as: Optional shorter text to show in UI
        """
        if self.agent_mgr is None or self.agent_mgr.active is None:
            log.warning("_send_to_active_agent: no agent manager or active agent")
            self.notify("Agent not ready", severity="error")
            return

        self._send_to_agent(self.agent_mgr.active, prompt, display_as=display_as)

    def _send_to_agent(
        self, agent: "Agent", prompt: str, *, display_as: str | None = None
    ) -> None:
        """Send prompt to a specific agent.

        Args:
            agent: The agent to send to
            prompt: Full prompt to send to Claude
            display_as: Optional shorter text to show in UI
        """
        # Clear visual indicator (images already on agent.pending_images)
        if attachments := self.query_one_optional(
            "#image-attachments", ImageAttachments
        ):
            attachments.clear()

        # Start async send (returns immediately, callbacks handle UI)
        create_safe_task(
            agent.send(prompt, display_as=display_as),
            name=f"send-{agent.id}",
        )

    def _show_thinking(self, agent_id: str | None = None) -> None:
        """Show the thinking indicator for a specific agent."""
        chat_view = self._get_chat_view(agent_id)
        if chat_view:
            chat_view.start_response()

    def _hide_thinking(self, agent_id: str | None = None) -> None:
        """Hide thinking indicator for a specific agent."""
        try:
            chat_view = self._get_chat_view(agent_id)
            if chat_view:
                chat_view._hide_thinking()
        except Exception:
            pass  # OK to fail during shutdown

    @profile
    def on_tool_use_message(self, event: ToolUseMessage) -> None:
        agent = self._get_agent(event.agent_id)
        chat_view = self._get_chat_view(event.agent_id)
        if not agent or not chat_view:
            return

        # Create ToolUse data object for ChatView
        tool = ToolUse(
            id=event.block.id, name=event.block.name, input=event.block.input
        )
        chat_view.append_tool_use(tool, event.block, event.parent_tool_use_id)

    @profile
    def on_tool_result_message(self, event: ToolResultMessage) -> None:
        chat_view = self._get_chat_view(event.agent_id)
        if not chat_view:
            return

        chat_view.update_tool_result(
            event.block.tool_use_id, event.block, event.parent_tool_use_id
        )
        self._show_thinking(event.agent_id)

    def on_system_notification(self, event: SystemNotification) -> None:
        """Handle system notification from SDK.

        Known subtypes:
        - api_error: API errors with retry info
        - compact_boundary: Conversation compaction markers
        - local_command: Slash command records
        - stop_hook_summary: Hook execution results
        - turn_duration: Timing info (no display needed)
        """
        subtype = event.subtype
        data = event.data
        level = data.get("level", "info")

        # Display important notifications in chat (not stored in history)
        if subtype == "api_error":
            error = data.get("error", {})
            error_msg = error.get("error", {}).get("message", "API error")
            retry = data.get("retryAttempt", 0)
            max_retries = data.get("maxRetries", 0)
            if retry > 0:
                self._show_system_info(
                    f"API error (retry {retry}/{max_retries}): {error_msg}",
                    "warning",
                    event.agent_id,
                )
            else:
                self._show_system_info(
                    f"API error: {error_msg}", "error", event.agent_id
                )

        elif subtype == "compact_boundary":
            content = data.get("content", "Conversation compacted")
            self._show_system_info(content, "info", event.agent_id)

        elif level == "error":
            # Generic error handling for any error-level message
            msg = data.get("content", data.get("error", f"System error: {subtype}"))
            self._show_system_info(str(msg)[:200], "error", event.agent_id)

        elif subtype not in ("stop_hook_summary", "turn_duration", "local_command"):
            # Unknown subtype with content - might be important (like terms notification)
            content = data.get("content") or data.get("message")
            if content:
                log.info("Unknown system message [%s]: %s", subtype, content)
                self._show_system_info(str(content), "info", event.agent_id)

        # Log all notifications for debugging
        log.debug(
            "System notification: subtype=%s level=%s data=%s",
            subtype,
            level,
            list(data.keys()),
        )

    def _show_system_info(
        self, message: str, severity: str, agent_id: str | None
    ) -> None:
        """Show system info message in chat view (not stored in history)."""
        from claudechic.filters import should_filter_message

        if should_filter_message(message):
            log.debug("Filtered system message: %s", message[:100])
            return

        chat_view = self._get_chat_view(agent_id)
        if not chat_view:
            # Fallback to notify if no chat view
            notify_map = {"warning": "warning", "error": "error"}
            self.notify(message[:100], severity=notify_map.get(severity, "information"))  # type: ignore[arg-type]
            return

        chat_view.append_system_info(message, severity)

    def on_resize(self, event) -> None:
        """Reposition right sidebar on resize and handle compact height."""
        self.call_after_refresh(self._position_right_sidebar)
        self.call_after_refresh(self._apply_compact_height)

    def _position_right_sidebar(self) -> None:
        """Show/hide right sidebar and adjust centering based on terminal width."""
        # Show sidebar when wide enough and we have multiple agents, worktrees, or todos
        agent_count = len(self.agent_mgr) if self.agent_mgr else 0
        has_content = bool(
            agent_count > 1
            or self.agent_section._worktrees
            or self.todo_panel.todos
            or (self._review_panel and self._review_panel.review_count)
        )
        width = self.size.width
        main = self.query_one("#main", Horizontal)

        # Check if any agent needs attention (for hamburger color)
        needs_attention = any(
            a.status == AgentStatus.NEEDS_INPUT for a in self.agents.values()
        )

        # Compute desired state
        show_sidebar_inline = width >= self.SIDEBAR_MIN_WIDTH and has_content
        show_overlay = (
            has_content and not show_sidebar_inline and self._sidebar_overlay_open
        )
        show_hamburger = (
            has_content and not show_sidebar_inline and not self._sidebar_overlay_open
        )
        sidebar_shift = show_sidebar_inline and width < self.CENTERED_SIDEBAR_WIDTH
        hide_sidebar = not (show_sidebar_inline or show_overlay)

        # Apply classes - set_class is a no-op if state already matches
        self.right_sidebar.set_class(hide_sidebar, "hidden")
        self.right_sidebar.set_class(show_overlay, "overlay")
        self.hamburger_btn.set_class(show_hamburger, "visible")
        self.hamburger_btn.set_class(
            show_hamburger and needs_attention, "needs-attention"
        )
        main.set_class(sidebar_shift, "sidebar-shift")

        # Reset overlay state when not applicable
        if not has_content or show_sidebar_inline:
            self._sidebar_overlay_open = False

        # Layout sidebar contents when visible
        if show_sidebar_inline or show_overlay:
            self._layout_sidebar_contents()

    COMPACT_HEIGHT = 20  # Enable compact mode below this height

    def _apply_compact_height(self) -> None:
        """Apply compact styles based on terminal height."""
        height = self.size.height
        compact = height < self.COMPACT_HEIGHT
        try:
            for widget in [
                self.query_one("StatusFooter"),
                self.query_one("#input-container"),
                *self.query(".chat-view"),
            ]:
                # set_class is a no-op if state already matches
                widget.set_class(compact, "compact-height")
        except Exception:
            pass  # Widgets not yet mounted

    def _layout_sidebar_contents(self) -> None:
        """Coordinate sidebar section visibility and compaction based on available space.

        Priority order (highest first): active agent > todos > other agents > processes > plan
        """
        # Get available height (terminal height minus footer)
        height = self.size.height - 1  # StatusFooter is 1 line

        # Count items in each section
        agent_count = self.agent_section.item_count
        todo_count = len(self.todo_panel.todos)
        review_count = self._review_panel.review_count if self._review_panel else 0
        process_count = self.process_panel.process_count
        has_plan = self.plan_section.has_plan

        # Height costs (lines) - based on actual CSS
        # AgentSection title: padding 1 1 1 1 = 3 lines (top + text + bottom)
        AGENT_SECTION_TITLE = 3
        AGENT_EXPANDED = 3  # height: 3 with padding
        AGENT_COMPACT = 1  # height: 1 without padding
        # TodoPanel: border-top(1) + padding(2) + title with padding(2) = 5 lines overhead
        TODO_OVERHEAD = 5
        TODO_ITEM = 1
        # ReviewPanel: same structure as TodoPanel
        REVIEW_OVERHEAD = 5
        REVIEW_ITEM = 1
        # ProcessPanel: same structure as TodoPanel
        PROCESS_OVERHEAD = 5
        PROCESS_ITEM = 1
        # PlanSection: border-top(1) + title(3) + item(3) = 7 lines
        PLAN_TOTAL = 7

        # Start with fixed costs: agent section title always present
        used = AGENT_SECTION_TITLE

        # Todos: high priority, always show if present
        if todo_count:
            used += TODO_OVERHEAD + todo_count * TODO_ITEM
        self.todo_panel.set_visible(bool(todo_count))

        remaining = height - used

        # Agents: try expanded first, fall back to compact
        agents_expanded = agent_count * AGENT_EXPANDED
        agents_compact = agent_count * AGENT_COMPACT

        if agents_expanded <= remaining:
            self.agent_section.set_compact(False)
            remaining -= agents_expanded
        else:
            self.agent_section.set_compact(True)
            remaining -= agents_compact

        # Reviews: show if room
        if review_count and remaining >= REVIEW_OVERHEAD + review_count * REVIEW_ITEM:
            self.review_panel.set_visible(True)
            remaining -= REVIEW_OVERHEAD + review_count * REVIEW_ITEM
        else:
            self.review_panel.set_visible(False)

        # Processes: show if room
        if (
            process_count
            and remaining >= PROCESS_OVERHEAD + process_count * PROCESS_ITEM
        ):
            self.process_panel.set_visible(True)
            remaining -= PROCESS_OVERHEAD + process_count * PROCESS_ITEM
        else:
            self.process_panel.set_visible(False)

        # Plan: lowest priority, show if room
        if has_plan and remaining >= PLAN_TOTAL:
            self.plan_section.set_visible(True)
        else:
            self.plan_section.set_visible(False)

    def on_response_complete(self, event: ResponseComplete) -> None:
        agent = self._get_agent(event.agent_id)
        chat_view = self._get_chat_view(event.agent_id)
        self._set_agent_status(AgentStatus.IDLE, event.agent_id)
        if event.result and agent:
            agent.session_id = event.result.session_id
            self.refresh_context()
        if chat_view:
            # End response via ChatView (hides thinking, flushes content)
            chat_view.end_response()
            # Flush any pending debounced content and mark summary
            current = chat_view._current_response
            if current:
                current.flush()
                if agent and agent.response_had_tools:
                    current.add_class("summary")
        self.chat_input.focus()
        self.completions.put_nowait(event)

        # Continue worktree finish if this agent has a pending finish
        # This check is agent-scoped so switching agents won't trigger cleanup
        if agent and agent.finish_state:
            on_response_complete_finish(self, agent)

        # Refresh roborev reviews after response
        if agent:
            self._refresh_reviews(agent)

        # Check for plan file and update sidebar
        if agent and agent.session_id:
            self.run_worker(self._check_for_plan(agent))

    def on_command_output_message(self, event: CommandOutputMessage) -> None:
        """Handle command output (e.g., /context) by displaying in chat."""
        self._hide_thinking(event.agent_id)
        chat_view = self._get_chat_view(event.agent_id)
        if not chat_view:
            return

        # Use custom widget for context reports
        if "## Context Usage" in event.content:
            from claudechic.widgets.reports.context import ContextReport

            widget = ContextReport(event.content)
        else:
            # Fallback to system message for other command output
            widget = ChatMessage(event.content)
            widget.add_class("system-message")

        chat_view.mount(widget)
        chat_view.scroll_if_tailing()

    @work(group="resume", exclusive=True, exit_on_error=False)
    async def resume_session(self, session_id: str) -> None:
        """Resume a session by reconnecting the active agent."""
        log.info(f"resume_session started: {session_id}")
        agent = self._agent
        if not agent:
            self.show_error("No active agent to resume")
            self.post_message(ResponseComplete(None))
            return
        try:
            await self._reconnect_agent(agent, session_id)
            agent.session_id = session_id
            self.post_message(ResponseComplete(None))
            self.refresh_context()
            # Check for plan file
            self.run_worker(self._check_for_plan(agent))
            log.info(f"Resume complete for {session_id}")
        except Exception as e:
            self.show_error("Session resume failed", e)
            self.post_message(ResponseComplete(None))

    def action_clear(self) -> None:
        chat_view = self._chat_view
        if chat_view:
            chat_view.clear()

    def action_copy_selection(self) -> None:
        selected = self.screen.get_selected_text()
        if selected:
            self.copy_to_clipboard(selected)
            self.notify("Copied to clipboard")

    def action_new_agent(self) -> None:
        """Create a new agent (prompts for name/path)."""
        self.notify("Use /agent <name> to create a new agent")

    def action_switch_agent(self, position: int) -> None:
        """Switch to agent by position (1-indexed)."""
        agent_ids = list(self.agents.keys())
        if 0 < position <= len(agent_ids) and self.agent_mgr:
            self.agent_mgr.switch(agent_ids[position - 1])

    def action_history_search(self) -> None:
        """Open reverse history search, or cycle if already open."""
        hs = self.query_one("#history-search", HistorySearch)
        if hs.display:
            hs.action_next_match()
        else:
            hs.show()

    def on_history_search_selected(self, event: HistorySearch.Selected) -> None:
        """Handle history selection - populate input."""
        # Suppress autocomplete BEFORE setting text to prevent timer start
        self.query_one(TextAreaAutoComplete).suppress()
        self.chat_input.text = event.text
        self.chat_input.move_cursor(self.chat_input.document.end)
        self.chat_input.focus()

    def on_history_search_cancelled(self, event: HistorySearch.Cancelled) -> None:
        """Handle history search cancellation."""
        self.chat_input.focus()

    def on_pending_shell_widget_cancelled(
        self, event: "PendingShellWidget.Cancelled"
    ) -> None:
        """Handle shell command cancellation from widget."""
        handler = self._pending_shell_cancels.get(id(event.widget))
        if handler:
            handler(event)

    def on_mouse_up(self, event: MouseUp) -> None:
        # Close sidebar overlay if clicking outside of it
        if self._sidebar_overlay_open:
            # Check if click is outside the sidebar
            try:
                sidebar = self.right_sidebar
                # Get click position relative to screen
                x, _ = event.screen_x, event.screen_y
                # Check if within sidebar bounds
                sb_x = sidebar.region.x
                sb_width = sidebar.region.width
                if x < sb_x or x >= sb_x + sb_width:
                    self._close_sidebar_overlay()
            except Exception:
                pass
        self.set_timer(0.05, self._check_and_copy_selection)

    def copy_to_clipboard(self, text: str) -> None:
        """Copy to both CLIPBOARD (OSC 52) and PRIMARY (xclip/xsel) on Linux."""
        super().copy_to_clipboard(text)
        if not sys.platform.startswith("linux"):
            return
        import shutil
        import subprocess

        for cmd in (
            ["xclip", "-selection", "primary"],
            ["xsel", "--primary", "--input"],
        ):
            if shutil.which(cmd[0]):
                try:
                    proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    proc.stdin.write(text.encode())  # type: ignore[union-attr]
                    proc.stdin.close()  # type: ignore[union-attr]
                except Exception:
                    pass
                return

    def _check_and_copy_selection(self) -> None:
        selected = self.screen.get_selected_text()
        if selected and len(selected.strip()) > 0:
            self.copy_to_clipboard(selected)
            self.notify("Copied", timeout=1)

    def action_quit(self) -> None:  # type: ignore[override]
        # If history search is visible, cancel it
        if hs := self.query_one_optional("#history-search", HistorySearch):
            if hs.styles.display != "none":
                hs.action_cancel()
                return

        # If shell command is running, kill it
        if self._shell_process is not None:
            try:
                # os.killpg is Unix-only; fall back to terminate() on Windows
                if sys.platform != "win32":
                    os.killpg(self._shell_process.pid, 15)  # SIGTERM to process group
                else:
                    self._shell_process.terminate()
            except (ProcessLookupError, OSError):
                self._shell_process.terminate()
            return

        # If input has text, clear it first
        if chat_input := self.query_one_optional("ChatInput", ChatInput):
            if chat_input.text:
                chat_input.text = ""
                return

        now = time.time()
        if hasattr(self, "_last_quit_time") and now - self._last_quit_time < 1.0:
            self.run_worker(self._cleanup_and_exit())
        else:
            self._last_quit_time = now
            self.notify("Press Ctrl+C again to quit")

    async def _cleanup_and_exit(self, reason: str = "quit") -> None:
        """Disconnect all agents and exit.

        Args:
            reason: Why the app is closing (quit, crash, error)
        """
        # Close all agents in parallel (fires agent_closed events with message_count)
        if self.agent_mgr:
            await self.agent_mgr.close_all()

        # Track app close with session duration
        duration = time.time() - getattr(self, "_app_start_time", time.time())
        await capture("app_closed", duration_seconds=int(duration), end_reason=reason)

        # Suppress SDK stderr noise during exit (stream closed errors on all platforms)
        # Note: Windows transport __del__ exceptions need additional stderr redirect
        # in __main__.py's finally block (after Textual exits). See issue #31.
        sys.stderr = open(os.devnull, "w")
        self.exit()

    def run_shell_command(
        self, cmd: str, shell: str, cwd: str | None, env: dict[str, str]
    ) -> None:
        """Run a shell command async with PTY for color support."""
        from claudechic.shell_runner import run_in_pty_cancellable
        from claudechic.widgets import PendingShellWidget, ShellOutputWidget

        chat_view = self._chat_view
        if not chat_view:
            return

        # Create pending widget with cancel button
        pending_widget = PendingShellWidget(cmd)
        chat_view.mount(pending_widget)
        chat_view.scroll_if_tailing()

        # Store cancel flag that can be set by the widget
        cancel_event = asyncio.Event()

        def on_cancel(message: PendingShellWidget.Cancelled) -> None:
            if message.widget is pending_widget:
                cancel_event.set()

        self._pending_shell_cancels[id(pending_widget)] = on_cancel

        async def _run() -> None:
            try:
                # Show tip after 1 second if command is still running
                async def show_tip_after_delay() -> None:
                    await asyncio.sleep(1.0)
                    self.notify("Tip: Use -i flag for interactive commands", timeout=5)

                tip_task = create_safe_task(show_tip_after_delay(), name="tip-delay")

                output, returncode, was_cancelled = await run_in_pty_cancellable(
                    cmd, shell, cwd, env, cancel_event
                )
                tip_task.cancel()

                # Clean up cancel handler
                self._pending_shell_cancels.pop(id(pending_widget), None)

                # Remove pending widget
                pending_widget.remove()

                if was_cancelled:
                    self.notify("Command cancelled")
                else:
                    widget = ShellOutputWidget(
                        command=cmd,
                        stdout=output,
                        stderr="",
                        returncode=returncode,
                    )
                    chat_view.mount(widget)
                    chat_view.scroll_if_tailing()

            except asyncio.CancelledError:
                self._pending_shell_cancels.pop(id(pending_widget), None)
                pending_widget.remove()
                self.notify("Command cancelled")

        self.run_worker(_run(), exclusive=False)

    def _show_session_picker(self) -> None:
        from claudechic.screens import SessionScreen

        def on_dismiss(session_id: str | None) -> None:
            if session_id:
                log.info(f"Resuming session: {session_id}")
                self.run_worker(self._load_and_display_history(session_id))
                self.notify(f"Resuming {session_id[:8]}...")
                self.resume_session(session_id)
            self.chat_input.focus()

        # Use current agent's cwd so sessions are filtered by the agent's project
        cwd = self._agent.cwd if self._agent else None
        self.push_screen(SessionScreen(cwd=cwd), on_dismiss)

    def _show_rewind_picker(self) -> None:
        """Show the rewind checkpoint picker screen."""
        from claudechic.screens import RewindScreen

        agent = self._agent
        if not agent:
            self.notify("No active agent", severity="error")
            return

        def on_dismiss(result: tuple[int, str] | None) -> None:
            if result:
                checkpoint_idx, rewind_type = result
                self.run_worker(self._do_rewind(checkpoint_idx, rewind_type))
            self.chat_input.focus()

        self.push_screen(RewindScreen(agent), on_dismiss)

    def _rewind_to_checkpoint_direct(self, index: int) -> None:
        """Directly rewind to a checkpoint index (defaults to 'both')."""
        self.run_worker(self._do_rewind(index, "both"))

    async def _do_rewind(self, checkpoint_idx: int, rewind_type: str) -> None:
        """Execute the rewind operation.

        Args:
            checkpoint_idx: The checkpoint index to rewind to
            rewind_type: One of "conversation", "code", or "both"
        """
        from claudechic.checkpoints import get_checkpoints
        from claudechic.enums import AgentStatus

        agent = self._agent
        if not agent or not agent.client:
            self.notify("No active agent", severity="error")
            return

        # Prevent rewind while agent is processing (mutating messages could cause race conditions)
        if agent.status != AgentStatus.IDLE:
            self.notify("Cannot rewind while agent is busy", severity="warning")
            return

        checkpoints = get_checkpoints(agent)
        if not checkpoints:
            self.notify("No checkpoints available", severity="warning")
            return
        if checkpoint_idx < 0 or checkpoint_idx >= len(checkpoints):
            self.notify(
                f"Invalid checkpoint {checkpoint_idx}. Valid: 0-{len(checkpoints) - 1}",
                severity="error",
            )
            return

        checkpoint = checkpoints[checkpoint_idx]
        rewound_files = False
        rewound_conversation = False

        if rewind_type in ("code", "both"):
            # Revert files using SDK
            if checkpoint.uuid:
                try:
                    await agent.client.rewind_files(checkpoint.uuid)
                    rewound_files = True
                except Exception as e:
                    log.exception("Failed to rewind files")
                    self.notify(f"Failed to rewind files: {e}", severity="error")
                    if rewind_type == "code":
                        return
            else:
                self.notify(
                    "No UUID for checkpoint - file rewind not available",
                    severity="warning",
                )
                if rewind_type == "code":
                    return

        if rewind_type in ("conversation", "both"):
            # Truncate messages to checkpoint
            # Find the index in agent.messages for this checkpoint's user message,
            # then include all assistant messages until the next user message
            user_msg_count = 0
            checkpoint_user_idx = 0
            for i, item in enumerate(agent.messages):
                if item.role == "user":
                    if user_msg_count == checkpoint_idx:
                        checkpoint_user_idx = i
                        break
                    user_msg_count += 1

            # Now find where to truncate: after all assistant messages following this user
            truncate_at = checkpoint_user_idx + 1
            for i in range(checkpoint_user_idx + 1, len(agent.messages)):
                if agent.messages[i].role == "user":
                    # Found next user message - stop before it
                    break
                truncate_at = i + 1  # Include this assistant message

            agent.messages = agent.messages[:truncate_at]
            agent.checkpoint_uuids = agent.checkpoint_uuids[: checkpoint_idx + 1]

            # Re-render chat view
            chat_view = self._chat_view
            if chat_view:
                chat_view._render_full()

            rewound_conversation = True

        # Single combined notification
        if rewound_files and rewound_conversation:
            self.notify(
                f"Rewound to checkpoint #{checkpoint_idx} (files + conversation)"
            )
        elif rewound_files:
            self.notify(f"Rewound files to checkpoint #{checkpoint_idx}")
        elif rewound_conversation:
            self.notify(f"Rewound conversation to checkpoint #{checkpoint_idx}")

    @work(group="reconnect", exclusive=True, exit_on_error=False)
    async def _reconnect_sdk(self, new_cwd: Path) -> None:
        """Reconnect SDK with a new working directory."""
        agent = self._agent
        if not agent:
            return
        try:
            # Check for existing session BEFORE creating client
            sessions = await get_recent_sessions(limit=1, cwd=new_cwd)
            resume_id = sessions[0][0] if sessions else None

            await self._replace_client(
                self._make_options(
                    cwd=new_cwd,
                    resume=resume_id,
                    agent_name=agent.name,
                    model=agent.model,
                    effort=agent.effort,
                )
            )

            # Clear ChatView state
            chat_view = self._chat_views.get(agent.id)
            if chat_view:
                chat_view.clear()
            agent.cwd = new_cwd

            if resume_id:
                await self._load_and_display_history(resume_id, cwd=new_cwd)
                agent.session_id = resume_id
                self.notify(f"Resumed session in {new_cwd.name}")
            else:
                agent.session_id = None
                self.notify(f"SDK reconnected in {new_cwd.name}")
        except Exception as e:
            self.show_error("SDK reconnect failed", e)

    async def _check_for_plan(self, agent: "Agent") -> None:
        """Check if a plan file exists for this agent's session and cache it."""
        if not agent.session_id:
            return
        plan_path = await get_plan_path_for_session(agent.session_id, cwd=agent.cwd)
        agent.plan_path = plan_path
        # Update sidebar if this is still the active agent
        if self._agent and self._agent.id == agent.id:
            self.plan_section.set_plan(plan_path)

    def action_escape(self) -> None:
        """Handle Escape: cancel picker, dismiss prompts, close overlay, or interrupt agent."""
        # Sidebar overlay takes priority (most likely what user wants to dismiss)
        if self._sidebar_overlay_open:
            self._close_sidebar_overlay()
            return

        # Cancel active agent's prompt only
        if self.active_agent_id:
            active_prompt = self._active_prompts.get(self.active_agent_id)
        else:
            active_prompt = None
        if active_prompt:
            active_prompt.cancel()
            return

        # Interrupt running agent - send interrupt to SDK
        if self.client and self._agent and self._agent.status == "busy":
            self.run_worker(self.client.interrupt(), exclusive=False)
            self._hide_thinking()
            self.notify("Interrupted")
            self.chat_input.focus()
            return

        # Vi-mode: switch from INSERT to NORMAL mode
        from claudechic.widgets.input.vi_mode import ViMode

        if self.chat_input.vi_mode == ViMode.INSERT:
            if self.chat_input._vi_handler:
                self.chat_input._vi_handler.handle_key("escape", None)
            return

    def on_agent_item_selected(self, event: AgentItem.Selected) -> None:
        """Handle agent selection from sidebar."""
        # Close overlay when selecting (even if same agent - user is done with sidebar)
        self._close_sidebar_overlay()
        if event.agent_id == self.active_agent_id:
            return
        # Update sidebar immediately for responsive feel (before heavier switch work)
        self.agent_section.set_active(event.agent_id)
        if self.agent_mgr:
            self.agent_mgr.switch(event.agent_id)

    def on_agent_tool_widget_go_to_agent(
        self, event: AgentToolWidget.GoToAgent
    ) -> None:
        """Handle 'Go to agent' button click from AgentToolWidget."""
        if not self.agent_mgr:
            return
        for agent_id, agent in self.agents.items():
            if agent.name == event.agent_name:
                self.agent_mgr.switch(agent_id)
                return
        self.notify(f"Agent '{event.agent_name}' not found", severity="warning")

    def on_worktree_item_selected(self, event: WorktreeItem.Selected) -> None:
        """Handle ghost worktree selection - create an agent there."""
        self._close_sidebar_overlay()
        self._create_new_agent(
            event.branch, event.path, worktree=event.branch, auto_resume=True
        )

    def on_plan_item_plan_requested(self, event: PlanItem.PlanRequested) -> None:
        """Handle plan item click - open plan file in editor."""
        editor = os.environ.get("EDITOR", "vi")
        handle_command(self, f"/shell -i {editor} {event.plan_path}")

    def on_file_item_selected(self, event: FileItem.Selected) -> None:
        """Handle file item click - open diff view focused on that file."""
        self._toggle_diff_mode_for_file(str(event.file_path))

    def on_hamburger_button_sidebar_toggled(
        self, event: HamburgerButton.SidebarToggled
    ) -> None:
        """Handle hamburger button press - toggle sidebar overlay."""
        self._sidebar_overlay_open = not self._sidebar_overlay_open
        self._position_right_sidebar()

    def on_permission_mode_label_toggled(
        self, event: PermissionModeLabel.Toggled
    ) -> None:  # noqa: ARG002
        """Handle permission mode label press - cycle through modes."""
        self.action_cycle_permission_mode()

    def on_model_label_model_change_requested(
        self, event: ModelLabel.ModelChangeRequested
    ) -> None:
        """Handle model label press - open model selector."""
        self._handle_model_prompt()

    def on_effort_label_effort_change_requested(
        self, event: EffortLabel.EffortChangeRequested
    ) -> None:
        """Handle effort label press - open effort selector."""
        self._handle_effort_prompt()

    def _close_sidebar_overlay(self) -> None:
        """Close sidebar overlay if open."""
        if self._sidebar_overlay_open:
            self._sidebar_overlay_open = False
            self._position_right_sidebar()

    def _update_hamburger_attention(self) -> None:
        """Update hamburger button color based on agent attention needs."""
        try:
            needs_attention = any(
                a.status == AgentStatus.NEEDS_INPUT for a in self.agents.values()
            )
            if needs_attention:
                self.hamburger_btn.add_class("needs-attention")
            else:
                self.hamburger_btn.remove_class("needs-attention")
        except Exception:
            pass  # Widget may not be mounted

    def on_edit_plan_requested(self, event: EditPlanRequested) -> None:
        """Handle edit plan button click in ExitPlanMode widget."""
        editor = os.environ.get("EDITOR", "vi")
        handle_command(self, f"/shell -i {editor} {event.plan_path}")

    def on_edit_file_requested(self, event: EditFileRequested) -> None:
        """Handle edit file icon click in diff view."""
        editor = os.environ.get("EDITOR", "vi")
        # Resolve path relative to current agent's cwd
        cwd = (
            self.agent_mgr.active.cwd
            if self.agent_mgr and self.agent_mgr.active
            else Path.cwd()
        )
        path = cwd / event.path
        handle_command(self, f"/shell -i {editor} {path}")

    def _populate_worktrees(self) -> None:
        """Populate sidebar with ghost worktrees for feature branches."""
        try:
            worktrees = list_worktrees()
        except Exception:
            return  # Not a git repo or git not available
        # Get names of existing agents to skip
        agent_names = {a.name for a in self.agents.values()}
        for wt in worktrees:
            if wt.is_main:
                continue  # Skip main worktree
            if wt.branch in agent_names:
                continue  # Already have an agent
            self.agent_section.add_worktree(wt.branch, wt.path)

    def on_agent_item_close_requested(self, event: AgentItem.CloseRequested) -> None:
        """Handle close button click on agent item."""
        if len(self.agents) <= 1:
            self.notify("Cannot close the last agent", severity="error")
            return
        self._do_close_agent(event.agent_id)

    async def _reconnect_agent(self, agent: "Agent", session_id: str) -> None:
        """Disconnect and reconnect an agent to reload its session."""
        await agent.disconnect()
        options = self._make_options(
            cwd=agent.cwd,
            resume=session_id,
            agent_name=agent.name,
            model=agent.model,
            effort=agent.effort,
        )
        await agent.connect(options, resume=session_id)

    @work(group="clear", exclusive=True, exit_on_error=False)
    async def _start_new_session(self) -> None:
        """Start a fresh session for the current agent."""
        agent = self._agent
        if not agent:
            return
        chat_view = self._chat_view
        if chat_view:
            chat_view.clear()
        await agent.disconnect()
        options = self._make_options(
            cwd=agent.cwd,
            agent_name=agent.name,
            model=agent.model,
            effort=agent.effort,
        )
        await agent.connect(options)
        self.refresh_context()
        self.notify("New session started")

    @work(group="usage", exclusive=True, exit_on_error=False)
    async def _handle_usage_command(self) -> None:
        """Handle /usage command - show API usage limits."""
        from claudechic.usage import fetch_usage
        from claudechic.widgets.reports.usage import UsageReport

        chat_view = self._chat_view
        if not chat_view:
            return

        usage = await fetch_usage()
        widget = UsageReport(usage)
        chat_view.mount(widget)
        chat_view.scroll_if_tailing()

    @work(group="model_switch", exclusive=True, exit_on_error=False)
    async def _set_agent_model(self, model: str) -> None:
        """Set model for active agent and reconnect."""
        agent = self._agent
        if not agent:
            self.notify("No active agent", severity="warning")
            return
        if model == agent.model:
            return
        old_model = agent.model or "default"
        agent.model = model
        self.run_worker(
            capture(
                "model_changed",
                from_model=old_model,
                to_model=model,
                agent_id=agent.analytics_id,
            )
        )
        self._update_footer_model(model)
        if agent.client:
            self.notify(f"Switching to {model}...")
            session_id = agent.session_id
            await agent.disconnect()
            options = self._make_options(
                cwd=agent.cwd,
                agent_name=agent.name,
                model=model,
                effort=agent.effort,
                resume=session_id,
            )
            await agent.connect(options)

    @work(group="model_prompt", exclusive=True, exit_on_error=False)
    async def _handle_model_prompt(self) -> None:
        """Show model selection prompt and handle result for active agent."""
        from textual.containers import Center

        agent = self._agent
        if not agent:
            self.notify("No active agent", severity="warning")
            return

        if not self._available_models:
            self.notify("No models available", severity="warning")
            return

        prompt = ModelPrompt(self._available_models, current_value=agent.model)
        container = Center(prompt, id="model-modal")
        self.mount(container)

        try:
            result = await prompt.wait()
        finally:
            container.remove()

        if result and result != agent.model:
            self._set_agent_model(result)

    @work(group="effort_switch", exclusive=True, exit_on_error=False)
    async def _set_agent_effort(self, effort: str) -> None:
        """Set effort for active agent and reconnect."""
        agent = self._agent
        if not agent:
            self.notify("No active agent", severity="warning")
            return
        effective = None if effort == "default" else effort
        if effective == agent.effort:
            return
        old_effort = agent.effort or "default"
        agent.effort = effective
        self.status_footer.effort = effective or ""
        self.run_worker(
            capture(
                "effort_changed",
                from_effort=old_effort,
                to_effort=effort,
                agent_id=agent.analytics_id,
            )
        )
        if agent.client:
            label = effort if effort != "default" else "auto"
            self.notify(f"Switching effort to {label}...")
            session_id = agent.session_id
            await agent.disconnect()
            options = self._make_options(
                cwd=agent.cwd,
                agent_name=agent.name,
                model=agent.model,
                effort=agent.effort,
                resume=session_id,
            )
            await agent.connect(options)

    @work(group="effort_prompt", exclusive=True, exit_on_error=False)
    async def _handle_effort_prompt(self) -> None:
        """Show effort selection prompt and handle result for active agent."""
        from textual.containers import Center

        agent = self._agent
        if not agent:
            self.notify("No active agent", severity="warning")
            return

        prompt = EffortPrompt(current_value=agent.effort)
        container = Center(prompt, id="effort-modal")
        self.mount(container)

        try:
            result = await prompt.wait()
        finally:
            container.remove()

        if result and result != agent.effort:
            self._set_agent_effort(result)

    # exclusive=False allows parallel agent creation.
    # Race on switch_to is acceptable - last created wins for active agent.
    @work(group="new_agent", exclusive=False, exit_on_error=False)
    async def _create_new_agent(
        self,
        name: str,
        cwd: Path,
        worktree: str | None = None,
        auto_resume: bool = False,
        switch_to: bool = True,
        model: str | None = None,
        initial_prompt: tuple[str, str] | None = None,
    ) -> None:
        """Create a new agent via AgentManager.

        Args:
            name: Display name for the agent
            cwd: Working directory
            worktree: Git worktree branch name if applicable
            auto_resume: Try to resume session with most messages in cwd
            switch_to: Whether to switch to the new agent (default True)
            model: Model override (None = SDK default)
            initial_prompt: (prompt, display_as) tuple to send after connecting
        """
        if self.agent_mgr is None:
            self.notify("Agent manager not initialized", severity="error")
            return

        # Create agent immediately for instant UI feedback
        agent = self.agent_mgr.create_unconnected(
            name=name,
            cwd=cwd,
            worktree=worktree,
            switch_to=switch_to,
        )

        # Show "connecting..." in footer and centered indicator in chat view
        self.status_footer.model = "connecting..."
        chat_view = self._chat_views.get(agent.id)
        connecting_indicator = None
        if chat_view:
            connecting_indicator = ConnectingIndicator()
            chat_view.mount(connecting_indicator)

        try:
            # Resolve resume ID if auto_resume
            resume_id = None
            if auto_resume:
                sessions = await get_recent_sessions(limit=100, cwd=cwd)
                if sessions:
                    # Pick session with most messages (index 3)
                    best = max(sessions, key=lambda s: s[3])
                    resume_id = best[0]

            # Connect to SDK (the slow part)
            await self.agent_mgr.connect_agent(agent, resume=resume_id, model=model)
        except Exception as e:
            self.show_error(f"Failed to create agent '{name}'", e)
            await self.agent_mgr.close(agent.id)
            # Reset footer if no agents left (close() switches otherwise)
            if not self.agent_mgr.agents:
                self.status_footer.model = ""
                self.status_footer.effort = ""
            return
        finally:
            # Always remove the connecting indicator
            if connecting_indicator:
                connecting_indicator.remove()

        # Update footer with connected agent's model and effort
        self._update_footer_model(agent.model)
        self.status_footer.effort = agent.effort or ""

        if resume_id:
            await self._load_and_display_history(resume_id, cwd=cwd)
            self.notify(f"Resumed session in '{name}'")
        else:
            label = f"Worktree '{name}'" if worktree else f"Agent '{name}'"
            self.notify(f"{label} ready")

        # Send initial prompt if provided
        if initial_prompt:
            prompt, display_as = initial_prompt
            self._send_to_agent(agent, prompt, display_as=display_as)

    def _execute_plan_fresh(self, agent: Agent) -> None:
        """Clear context and execute plan in fresh session."""
        plan_info = agent.pending_plan_execution
        agent.pending_plan_execution = None

        if not plan_info:
            return

        plan_content = plan_info["plan"]
        mode = plan_info["mode"]
        # Get plan path from info (may have been found via fallback)
        plan_path = plan_info.get("plan_path") or agent.plan_path

        async def clear_and_run():
            # Interrupt any ongoing response
            await agent.interrupt()

            # Clear UI
            chat_view = self._chat_views.get(agent.id)
            if chat_view:
                chat_view.clear()

            # Reconnect with fresh session (like /clear)
            await agent.disconnect()
            options = self._make_options(
                cwd=agent.cwd,
                agent_name=agent.name,
                model=agent.model,
                effort=agent.effort,
            )
            await agent.connect(options)

            # Restore plan path for execution session and show in sidebar
            agent.plan_path = plan_path
            self.plan_section.set_plan(plan_path)
            self._layout_sidebar_contents()

            # Set permission mode (falling back to acceptEdits if e.g. "auto"
            # is rejected) and send the plan.
            await self._set_mode_with_fallback(agent, mode)
            prompt = f"Execute this plan:\n\n{plan_content}"
            await agent.send(prompt)

            self.refresh_context()
            self.notify("Executing plan in fresh session")

        self.run_worker(clear_and_run())

    def _close_agent(self, target: str | None) -> None:
        """Close an agent by name, position, or current if no target."""
        if len(self.agents) <= 1:
            self.notify("Cannot close the last agent", severity="error")
            return

        # Find agent to close
        agent_to_close: Agent | None = None
        if target is None:
            # Close current agent
            agent_to_close = self._agent
        elif target.isdigit():
            # Close by position (1-indexed)
            pos = int(target) - 1
            agent_ids = list(self.agents.keys())
            if 0 <= pos < len(agent_ids):
                agent_to_close = self.agents[agent_ids[pos]]
        else:
            # Close by name
            for agent in self.agents.values():
                if agent.name == target:
                    agent_to_close = agent
                    break

        if not agent_to_close:
            self.notify(f"Agent not found: {target}", severity="error")
            return

        self._do_close_agent(agent_to_close.id)

    @work(group="close_agent", exclusive=True, exit_on_error=False)
    async def _do_close_agent(self, agent_id: str) -> None:
        """Actually close an agent (async for client cleanup)."""
        if self.agent_mgr is None:
            return
        agent = self.agents.get(agent_id)
        if not agent:
            return

        agent_name = agent.name

        # Remove chat view before closing (AgentManager.close removes from agents dict)
        chat_view = self._chat_views.pop(agent_id, None)
        if chat_view:
            await chat_view.remove()
        self._active_prompts.pop(agent_id, None)

        # Close via AgentManager (handles disconnect, removes from agents dict,
        # triggers on_agent_closed, and switches to another agent if needed)
        await self.agent_mgr.close(agent_id)

        self.notify(f"Agent '{agent_name}' closed")

    def on_app_focus(self) -> None:
        if self._chat_input:
            self._chat_input.focus()

    def on_paste(self, event) -> None:
        """App-level paste handler - catches pastes when input isn't focused."""
        if not self._chat_input:
            return
        # Skip if already handled by ChatInput (check if input is focused)
        if self.focused == self._chat_input:
            return  # Let ChatInput handle it

        # Use ChatInput's image detection logic
        images = self._chat_input._is_image_path(event.text)
        if images:
            # Use ChatInput's dedup tracking
            now = time.time()
            last = self._chat_input._last_image_paste
            if last and last[0] == event.text and now - last[1] < 0.5:
                event.prevent_default()
                event.stop()
                return
            self._chat_input._last_image_paste = (event.text, now)

            for path in images:
                self._attach_image(path)
            event.prevent_default()
            event.stop()

    def on_key(self, event) -> None:
        if self.query(SelectionPrompt) or self.query(QuestionPrompt):
            return
        if not self._chat_input or self.focused == self._chat_input:
            return
        if len(event.character or "") == 1 and event.character.isprintable():
            self._chat_input.focus()
            self._chat_input.insert(event.character)
            event.prevent_default()
            event.stop()

    # -----------------------------------------------------------------------
    # AgentManager callbacks (new architecture)
    # -----------------------------------------------------------------------

    def _wire_agent_manager_callbacks(self) -> None:
        """Wire AgentManager callbacks for UI integration.

        ChatApp implements AgentManagerObserver and AgentObserver protocols,
        so we just set self as the observer.
        """
        if self.agent_mgr is None:
            return

        self.agent_mgr.manager_observer = self
        self.agent_mgr.agent_observer = self
        self.agent_mgr.permission_handler = self._handle_agent_permission_ui

    def on_agent_created(self, agent: Agent) -> None:
        """Handle new agent creation from AgentManager."""
        log.info(f"New agent created: {agent.name} (id={agent.id})")

        # Track analytics (same_directory = agent cwd matches app starting cwd)
        same_directory = agent.cwd == Path.cwd()
        self._agent_metadata[agent.id] = {
            "created_at": time.time(),
            "same_directory": same_directory,
        }
        self.run_worker(
            capture(
                "agent_created",
                same_directory=same_directory,
                model=agent.model or "default",
            )
        )

        try:
            # Create chat view for the agent
            is_first_agent = len(self.agent_mgr.agents) == 1 if self.agent_mgr else True
            if is_first_agent:
                # First agent uses the existing chat view from compose()
                chat_view = self.query_one("#chat-view", ChatView)
                chat_view.add_class(
                    "chat-view"
                )  # Add class for consistent query behavior
            else:
                # Additional agents get new chat views - mount in chat-column before input
                chat_view = ChatView(
                    id=f"chat-view-{agent.id.replace('/', '-')}",
                    classes="chat-view hidden",
                )
                chat_column = self.query_one("#chat-column", Vertical)
                chat_column.mount(chat_view, before=self.input_container)

            # Store mapping and set agent reference on ChatView
            self._chat_views[agent.id] = chat_view
            chat_view.set_agent(agent)

            # Add to sidebar
            try:
                self.agent_section.add_agent(agent.id, agent.name)
            except Exception:
                log.debug(f"Sidebar not mounted for agent {agent.id}")

            # Show sidebar if now needed
            self._position_right_sidebar()

            # Populate files section with uncommitted changes
            create_safe_task(self._async_refresh_files(agent), name="refresh-files")

            # Refresh autocomplete with new agent
            self._refresh_dynamic_completions()
        except Exception as e:
            log.exception(f"Failed to create agent UI: {e}")

    def on_agent_switched(self, new_agent: Agent, old_agent: Agent | None) -> None:
        """Handle agent switch from AgentManager."""
        log.info(f"Switched to agent: {new_agent.name}")

        # Use update=False to defer CSS recalculation, refresh_css at end
        if old_agent:
            old_agent.pending_input = self.chat_input.text
            old_chat_view = self._chat_views.get(old_agent.id)
            if old_chat_view:
                old_chat_view.add_class("hidden", update=False)
            old_prompt = self._active_prompts.get(old_agent.id)
            if old_prompt:
                old_prompt.add_class("hidden", update=False)

        # Show new agent's chat view
        new_chat_view = self._chat_views.get(new_agent.id)
        if new_chat_view:
            new_chat_view.remove_class("hidden", update=False)
            new_chat_view.flush_deferred_updates()

        # Restore new agent's input
        self.chat_input.text = new_agent.pending_input

        # Show new agent's prompt if it has one, otherwise show input
        active_prompt = self._active_prompts.get(new_agent.id)
        if active_prompt:
            active_prompt.remove_class("hidden", update=False)
            active_prompt.focus()
            self.input_container.add_class("hidden", update=False)
        else:
            self.input_container.remove_class("hidden", update=False)

        try:
            self.agent_section.set_active(new_agent.id)
        except Exception:
            pass

        # Update footer
        self.status_footer.permission_mode = new_agent.permission_mode
        self._update_footer_model(new_agent.model)
        self.status_footer.effort = new_agent.effort or ""

        # Update todo panel and context
        self.todo_panel.update_todos(new_agent.todos)
        self.refresh_context()

        # Update plan button
        self.plan_section.set_plan(new_agent.plan_path)
        self._position_right_sidebar()

        # Single CSS refresh at the end
        self.refresh_css(animate=False)

        # Stop polling for old agent's reviews and refresh for new agent
        self._stop_review_polling()
        self._refresh_reviews(new_agent)

        # These happen outside (async/focus)
        create_safe_task(self._async_refresh_files(new_agent), name="refresh-files")
        create_safe_task(
            self.status_footer.refresh_branch(str(new_agent.cwd)), name="refresh-branch"
        )
        self.chat_input.focus()

    def on_agent_closed(self, agent_id: str, message_count: int = 0) -> None:
        """Handle agent closure from AgentManager."""
        log.info(f"Agent closed: {agent_id}")

        # Track analytics
        metadata = self._agent_metadata.pop(agent_id, {})
        duration = time.time() - metadata.get("created_at", time.time())
        same_directory = metadata.get("same_directory", True)
        self.run_worker(
            capture(
                "agent_closed",
                duration_seconds=int(duration),
                same_directory=same_directory,
                message_count=message_count,
            )
        )

        # Stop review polling only if the closed agent owned the timer
        self._stop_review_polling(agent_id)

        try:
            self.agent_section.remove_agent(agent_id)
        except Exception:
            pass
        self._position_right_sidebar()

        # Refresh autocomplete after agent closed
        self._refresh_dynamic_completions()

    def on_status_changed(self, agent: Agent) -> None:
        """Handle agent status change."""
        try:
            self.agent_section.update_status(agent.id, agent.status)
            # Update hamburger color if any agent needs attention
            self._update_hamburger_attention()
        except Exception:
            log.debug(f"Failed to update sidebar status for agent {agent.id}")

    def on_permission_mode_changed(self, agent: Agent) -> None:
        """Handle permission mode change."""
        # Only update footer if this is the active agent
        if self._agent and agent.id == self._agent.id:
            self._update_footer_permission_mode()

    def on_message_updated(self, agent: Agent) -> None:  # noqa: ARG002
        """Handle agent message content update (unused - fine-grained callbacks used instead)."""
        pass

    def on_prompt_added(self, agent: Agent, request: PermissionRequest) -> None:  # noqa: ARG002
        """Handle permission prompt queued (handled via permission_handler instead)."""
        pass

    def on_error(self, agent: Agent, message: str, exception: Exception | None) -> None:
        """Handle error from agent."""
        # Show error in UI if this is active agent
        if self.agent_mgr and agent.id == self.agent_mgr.active_id:
            self.show_error(message, exception)

        # Report error to analytics (sanitized - no paths or user data)
        error_type = type(exception).__name__ if exception else "Unknown"
        status_code = 0
        if exception:
            # Extract HTTP status code if present in error message
            err_str = str(exception)
            if "400" in err_str:
                status_code = 400
            elif "401" in err_str:
                status_code = 401
            elif "429" in err_str:
                status_code = 429
            elif "500" in err_str:
                status_code = 500
        self.run_worker(
            capture(
                "error_occurred",
                error_type=error_type,
                context="response",
                status_code=status_code,
                agent_id=agent.analytics_id,
            )
        )

    def on_connection_lost(self, agent: Agent) -> None:
        """Handle lost SDK connection - reconnect."""
        log.info(f"Connection lost for agent {agent.name}, reconnecting...")
        self.run_worker(
            capture(
                "error_occurred",
                error_type="ConnectionLost",
                context="connection_lost",
                agent_id=agent.analytics_id,
            )
        )
        self.notify("Reconnecting...", timeout=2)
        self._reconnect_after_interrupt(agent)

    @work(group="reconnect_after_interrupt", exclusive=True, exit_on_error=False)
    async def _reconnect_after_interrupt(self, agent: Agent) -> None:
        """Reconnect agent after connection was lost due to interrupt."""
        if not agent.session_id:
            log.warning("Cannot reconnect agent without session_id")
            return
        try:
            await self._reconnect_agent(agent, agent.session_id)
            self.notify("Reconnected", timeout=2)
        except Exception as e:
            log.exception("Failed to reconnect after interrupt")
            await capture(
                "error_occurred",
                error_type=type(e).__name__,
                context="reconnect_failed",
                agent_id=agent.analytics_id,
            )
            self.show_error("Reconnect failed", e)

    def on_complete(self, agent: Agent, result: ResultMessage | None) -> None:
        """Handle agent response completion."""
        log.info(f"Agent {agent.name} completed response")

        # Check for unrecognized slash commands (passed through but no Skill invoked)
        pending_cmd = self._pending_slash_commands.pop(agent.id, None)
        if pending_cmd:
            self.notify(
                f"Unknown command: {pending_cmd}\nType /help for available commands."
            )

        # Check for API errors in the result
        if result and result.is_error:
            error_msg = result.result or "Unknown API error"
            log.error(f"API error in response: {error_msg}")
            # Show as system message in chat
            chat_view = self._chat_views.get(agent.id)
            if chat_view:
                from claudechic.widgets.content.message import SystemInfo

                chat_view.mount(
                    SystemInfo(f"⚠️ API Error: {error_msg}", severity="error")
                )
                chat_view.scroll_if_tailing()

        # Post ResponseComplete message for existing UI handler
        self.post_message(ResponseComplete(result, agent_id=agent.id))

    def on_todos_updated(self, agent: Agent) -> None:
        """Handle agent todos update."""
        if self.agent_mgr and agent.id == self.agent_mgr.active_id:
            self.todo_panel.update_todos(agent.todos)
            self._position_right_sidebar()
            # Add inline widget to chat stream
            chat_view = self._get_chat_view(agent.id)
            if chat_view:
                chat_view.mount(TodoWidget(agent.todos))
                chat_view.scroll_if_tailing()

    def on_text_chunk(
        self, agent: Agent, text: str, new_message: bool, parent_tool_use_id: str | None
    ) -> None:
        """Handle text chunk from agent - update UI directly (bypasses message queue)."""
        sampler = get_sampler()
        if sampler:
            sampler.async_metrics.text_chunks += 1
        chat_view = self._chat_views.get(agent.id)
        if chat_view:
            chat_view.append_text(text, new_message, parent_tool_use_id)

    def on_tool_use(self, agent: Agent, tool: ToolUse) -> None:
        """Handle tool use from agent - post Textual Message for UI."""
        sampler = get_sampler()
        if sampler:
            sampler.async_metrics.tool_uses += 1
        # Clear pending slash command if Skill tool was invoked (valid command)
        if tool.name == ToolName.SKILL:
            self._pending_slash_commands.pop(agent.id, None)

        block = ToolUseBlock(id=tool.id, name=tool.name, input=tool.input)
        self.post_message(
            ToolUseMessage(
                block, parent_tool_use_id=tool.parent_tool_use_id, agent_id=agent.id
            )
        )

    def on_tool_result(self, agent: Agent, tool: ToolUse) -> None:
        """Handle tool result from agent - post Textual Message for UI."""
        sampler = get_sampler()
        if sampler:
            sampler.async_metrics.tool_results += 1
        from claude_agent_sdk import ToolResultBlock

        block = ToolResultBlock(
            tool_use_id=tool.id, content=tool.result or "", is_error=tool.is_error
        )
        self.post_message(
            ToolResultMessage(
                block, parent_tool_use_id=tool.parent_tool_use_id, agent_id=agent.id
            )
        )

        # Show tool errors prominently in chat
        if tool.is_error:
            # Strip SDK's <tool_use_error> tags from display
            error_msg = TOOL_USE_ERROR_PATTERN.sub("", tool.result or "Unknown error")
            error_preview = error_msg[:200]
            log.warning(f"Tool error [{tool.name}]: {error_preview}")
            # Note: We don't track tool errors to analytics - these are normal
            # workflow errors (file not found, etc), not system errors
            self.notify(f"Tool error: {tool.name}", severity="warning", timeout=5)

        # Track edited files in sidebar
        if not tool.is_error and tool.name in ("Edit", "Write"):
            file_path = tool.input.get("file_path")
            if file_path:
                self._track_edited_file(tool, Path(file_path))

    def on_system_message(self, agent: Agent, message: SystemMessage) -> None:
        """Handle system message from agent - post Textual Message for UI."""
        self.post_message(SystemNotification(message, agent_id=agent.id))

    def on_command_output(self, agent: Agent, content: str) -> None:
        """Handle command output from agent (e.g., /context)."""
        self.post_message(CommandOutputMessage(content, agent_id=agent.id))

    def on_skill_loaded(self, agent: Agent, skill_name: str) -> None:
        """Handle SDK-loaded skill - clear pending slash command."""
        # SDK recognized the command as a skill, so it's not unknown
        self._pending_slash_commands.pop(agent.id, None)
        log.info(f"Skill loaded: {skill_name}")

    def on_prompt_sent(
        self, agent: Agent, prompt: str, images: list[ImageAttachment]
    ) -> None:
        """Handle prompt sent to agent - display user message in chat view."""
        chat_view = self._chat_views.get(agent.id)
        if not chat_view:
            return

        # Skip UI for /clear command (it just forwards to SDK)
        if prompt.strip() == "/clear":
            return

        chat_view.append_user_message(prompt, images)
        chat_view.start_response()

    async def _handle_agent_permission_ui(
        self, agent: Agent, request: PermissionRequest
    ) -> PermissionResponse:
        """Handle permission UI for an agent.

        This is called by Agent when it needs user input for a permission.
        Returns a PermissionResponse with choice and optional alternative message.

        Each agent can have its own pending prompt. Prompts are shown/hidden
        based on which agent is active (handled by _show_prompt and on_agent_switched).
        """
        # Put in interactions queue for testing
        await self.interactions.put(request)

        return await self._show_permission_prompt(agent, request)

    async def _show_permission_prompt(
        self, agent: Agent, request: PermissionRequest
    ) -> PermissionResponse:
        """Show the permission prompt UI for an agent."""
        if request.tool_name == ToolName.ASK_USER_QUESTION:
            # Handle question prompts
            questions = request.tool_input.get("questions", [])
            async with self._show_prompt(QuestionPrompt(questions), agent) as prompt:
                answers = await prompt.wait()

            choice = PermissionChoice.ALLOW if answers else PermissionChoice.DENY
            self.run_worker(
                capture(
                    "permission_response",
                    tool="AskUserQuestion",
                    choice=choice.value,
                    agent_id=agent.analytics_id,
                )
            )
            if not answers:
                return PermissionResponse(PermissionChoice.DENY)

            # Store answers on request for Agent to retrieve
            request._answers = answers  # type: ignore[attr-defined]
            return PermissionResponse(PermissionChoice.ALLOW)

        # Special handling for ExitPlanMode - custom options
        if request.tool_name == ToolName.EXIT_PLAN_MODE:
            return await self._handle_exit_plan_mode_permission(request, agent)

        # Regular permission prompt
        if request.tool_name in self.AUTO_EDIT_TOOLS:
            # For edit tools, offer auto-edit mode (superset of allow_session)
            options = [
                (PermissionChoice.ALLOW_ALL, "Yes, all edits in this session"),
                (PermissionChoice.ALLOW, "Yes, this time only"),
            ]
        else:
            options = [
                (PermissionChoice.ALLOW, "Yes, this time only"),
                (PermissionChoice.ALLOW_SESSION, "Yes, always in this session"),
            ]
        # "No" option doubles as text input - empty = deny, text = alternative instructions
        text_option = (PermissionChoice.DENY, "No / Do something else...")

        async with self._show_prompt(
            SelectionPrompt(request.title, options, text_option), agent
        ) as prompt:

            def string_to_result(s: str) -> PermissionResponse:
                """Convert SelectionPrompt string result to PermissionResponse."""
                if s.startswith(f"{PermissionChoice.DENY}:"):
                    # Text input: "deny:some alternative message"
                    return PermissionResponse(
                        PermissionChoice.DENY, alternative_message=s[5:]
                    )
                # Direct choice
                try:
                    choice = PermissionChoice(s)
                except ValueError:
                    choice = PermissionChoice.DENY
                return PermissionResponse(choice)

            async def ui_response():
                raw = await prompt.wait()
                if not request._event.is_set():
                    request.respond(string_to_result(raw))

            create_safe_task(ui_response(), name="ui-response")
            result = await request.wait()

        if result.choice == PermissionChoice.ALLOW_ALL:
            self.notify("Auto-edit enabled (Shift+Tab to disable)")
        elif result.choice == PermissionChoice.ALLOW_SESSION:
            self.notify(f"{request.tool_name} allowed for this session")

        # Track permission prompt response
        self.run_worker(
            capture(
                "permission_response",
                tool=request.tool_name,
                choice=result.choice.value,
                has_alternative=bool(result.alternative_message),
                agent_id=agent.analytics_id,
            )
        )

        return result

    async def _handle_exit_plan_mode_permission(
        self, request: PermissionRequest, agent: Agent
    ) -> PermissionResponse:
        """Handle ExitPlanMode with plan-specific options."""
        # Get plan content from agent's plan_path (set when entering plan mode)
        plan_content: str | None = request.tool_input.get("plan")

        # Ensure plan_path is fetched (may not be ready if EnterPlanMode just completed)
        if not agent.plan_path:
            await agent.ensure_plan_path()
        plan_path = agent.plan_path

        if not plan_content and plan_path and plan_path.exists():
            plan_content = plan_path.read_text()

        # Pick the auto-approve target from the user's configured default mode.
        # If they've set `auto` in settings.json, exiting plan mode lands them
        # in `auto`; otherwise we fall back to `acceptEdits` (legacy behavior).
        configured = get_default_permission_mode(agent.cwd)
        auto_target = "auto" if configured == "auto" else "acceptEdits"
        auto_desc = (
            "enable auto mode" if auto_target == "auto" else "auto-approve edits"
        )

        options = [
            ("clear_auto", f"Yes, clear context and {auto_desc}"),
            ("auto", f"Yes, {auto_desc}"),
            ("manual", "Yes, manually approve edits"),
        ]
        text_option = ("deny", "No, stay in plan mode")

        try:
            async with self._show_prompt(
                SelectionPrompt("Execute plan?", options, text_option), agent
            ) as prompt:
                choice = await prompt.wait()
        except Exception as e:
            # If prompting fails, surface it instead of letting the SDK
            # silently mark the tool as denied. Hand feedback back to Claude
            # so the user isn't left wondering what happened.
            log.exception("ExitPlanMode prompt failed: %s", e)
            self.notify(
                f"ExitPlanMode prompt failed: {e}. Staying in plan mode.",
                severity="error",
                timeout=10,
            )
            return PermissionResponse(
                PermissionChoice.DENY,
                alternative_message=(
                    f"The ExitPlanMode prompt failed in the UI ({e!r}). "
                    "Stay in plan mode and tell the user what happened."
                ),
            )

        # Track the response
        self.run_worker(
            capture(
                "permission_response",
                tool="ExitPlanMode",
                choice=choice,
                agent_id=agent.analytics_id,
            )
        )

        if choice == "clear_auto":
            # Execute plan in fresh session immediately
            agent.pending_plan_execution = {
                "plan": plan_content or "No plan content found.",
                "mode": auto_target,
                "plan_path": plan_path,
            }
            self._execute_plan_fresh(agent)
            return PermissionResponse(PermissionChoice.DENY)
        elif choice == "auto":
            target = await self._set_mode_with_fallback(agent, auto_target)
            label = "Auto" if target == "auto" else "Auto-edit"
            self.notify(f"{label} enabled (Shift+Tab to cycle)")
            return PermissionResponse(PermissionChoice.ALLOW)
        elif choice == "manual":
            await agent.set_permission_mode("default")
            return PermissionResponse(PermissionChoice.ALLOW)
        else:
            # Check for feedback in "deny:feedback" format
            if choice.startswith(f"{PermissionChoice.DENY}:"):
                return PermissionResponse(
                    PermissionChoice.DENY, alternative_message=choice[5:]
                )
            return PermissionResponse(PermissionChoice.DENY)

    def _update_footer_model(self, model: str | None) -> None:
        """Update footer to show agent's model."""
        if not self._available_models:
            # No model info yet - show raw value or empty
            self.status_footer.model = (
                trim_model_name(model.capitalize()) if model else ""
            )
            return
        # Find matching model, or default if model is None
        active = self._available_models[0]
        for m in self._available_models:
            if model and m.get("value") == model:
                active = m
                break
            if not model and m.get("value") == "default":
                active = m
                break
        # Extract short name from description like "Opus 4.5 · ..."
        desc = active.get("description", "")
        model_name = (
            desc.split("·")[0].strip() if "·" in desc else active.get("displayName", "")
        )
        self.status_footer.model = trim_model_name(model_name)

    # ── Diff Mode ──────────────────────────────────────────────────────────────

    def _toggle_diff_mode(self, target: str | None = None) -> None:
        """Show diff screen for reviewing changes vs target (default HEAD)."""
        from claudechic.features.diff import HunkComment, format_hunk_comments
        from claudechic.screens import DiffScreen

        agent = self._agent
        if not agent:
            self.notify("No active agent", severity="error")
            return

        def on_dismiss(comments: list[HunkComment] | None) -> None:
            if comments:
                self.chat_input.text = format_hunk_comments(comments)
            self.chat_input.focus()

        self.push_screen(DiffScreen(agent.cwd, target or "HEAD"), on_dismiss)

    def _toggle_diff_mode_for_file(self, file_path: str) -> None:
        """Show diff screen focused on a specific file."""
        from claudechic.features.diff import HunkComment, format_hunk_comments
        from claudechic.screens import DiffScreen

        agent = self._agent
        if not agent:
            self.notify("No active agent", severity="error")
            return

        def on_dismiss(comments: list[HunkComment] | None) -> None:
            if comments:
                self.chat_input.text = format_hunk_comments(comments)
            self.chat_input.focus()

        self.push_screen(
            DiffScreen(agent.cwd, "HEAD", focus_file=file_path), on_dismiss
        )

    def _update_vi_mode(self, enabled: bool) -> None:
        """Update vi-mode on all ChatInput widgets and footer."""
        if chat_input := self.query_one_optional("#input", ChatInput):
            chat_input.enable_vi_mode(enabled)
            # Update footer with initial mode
            mode = chat_input.vi_mode if enabled else None
            self.status_footer.update_vi_mode(mode, enabled)
