"""Claude Code Textual UI - Main application."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from importlib.resources import files
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from claudechic.screens.chat import ChatScreen

from textual.app import App
from textual.screen import Screen

from claudechic.theme import CHIC_THEME
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
from claudechic.messages import (
    ResponseComplete,
    SystemNotification,
    ToolUseMessage,
    ToolResultMessage,
    CommandOutputMessage,
)
from claudechic.sessions import (
    find_session_by_prefix,
    get_context_from_session,
    get_plan_path_for_session,
    get_recent_sessions,
)
from claudechic.features.diff import EditFileRequested
from claudechic.features.worktree import list_worktrees
from claudechic.commands import BARE_WORDS, handle_command
from claudechic.features.worktree.commands import on_response_complete_finish
from claudechic.permissions import PermissionRequest, PermissionResponse
from claudechic.agent import Agent, ImageAttachment, ToolUse
from claudechic.agent_manager import AgentManager
from claudechic.analytics import capture
from claudechic.config import get_theme, set_theme, is_new_install
from claudechic.enums import AgentStatus, PermissionChoice, ToolName
from claudechic.mcp import set_app, create_chic_server
from claudechic.file_index import FileIndex
from claudechic.history import append_to_history
from claudechic.widgets import (
    ContextBar,
    ChatMessage,
    ChatInput,
    ImageAttachments,
    ErrorMessage,
    AgentToolWidget,
    TodoWidget,
    TodoPanel,
    ProcessPanel,
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
from claudechic.widgets.layout.footer import AutoEditLabel, ModelLabel, StatusFooter
from claudechic.widgets.prompts import ModelPrompt
from claudechic.errors import setup_logging  # noqa: F401 - used at startup
from claudechic.profiling import profile
from claudechic.sampling import start_sampler

log = logging.getLogger(__name__)


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
    ) -> None:
        super().__init__()
        self.scroll_sensitivity_y = 1.0  # Smoother scrolling (default is 2.0)
        # AgentManager is the single source of truth for agents
        self.agent_mgr: AgentManager | None = None

        self._resume_on_start = resume_session_id
        self._initial_prompt = initial_prompt
        self._remote_port = remote_port
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
        """Toggle auto-approve for Edit/Write tools for current agent."""
        if self._agent:
            self._agent._set_auto_edit(not self._agent.auto_approve_edits)
            self.notify(
                f"Auto-edit: {'ON' if self._agent.auto_approve_edits else 'OFF'}"
            )

    def _update_footer_auto_edit(self) -> None:
        """Update footer to reflect current agent's auto-edit state."""
        try:
            self.status_footer.auto_edit = (
                self._agent.auto_approve_edits if self._agent else False
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

    def _make_options(
        self,
        cwd: Path | None = None,
        resume: str | None = None,
        agent_name: str | None = None,
        model: str | None = None,
    ) -> ClaudeAgentOptions:
        """Create SDK options with common settings.

        Note: can_use_tool is set by Agent.connect() to its own handler,
        which routes to permission_ui_callback set by AgentManager.
        """
        # Load system prompt from bundled context file
        context_file = files("claudechic").joinpath("context.md")
        system_prompt = context_file.read_text()

        return ClaudeAgentOptions(
            permission_mode="default",
            env={"ANTHROPIC_API_KEY": ""},
            setting_sources=["user", "project", "local"],
            cwd=cwd,
            resume=resume,
            model=model,
            system_prompt=system_prompt,
            mcp_servers={"chic": create_chic_server(caller_name=agent_name)},
            include_partial_messages=True,
            stderr=self._handle_sdk_stderr,
        )

    async def on_mount(self) -> None:
        # Track app start (and install if new user)
        self._app_start_time = time.time()
        if is_new_install():
            self.run_worker(capture("app_installed"))
        self.run_worker(capture("app_started", resumed=bool(self._resume_on_start)))

        # Start CPU sampling profiler
        start_sampler()

        # Start background process polling
        self.set_interval(2.0, self._poll_background_processes)

        # Register app for MCP tools
        set_app(self)

        # Start remote control server if requested
        if self._remote_port:
            from claudechic.remote import start_server

            await start_server(self, self._remote_port)

        # Register and activate custom theme (use saved preference or default to chic)
        self.register_theme(CHIC_THEME)
        self.theme = get_theme() or "chic"

        # Initialize AgentManager (but don't create agent yet - wait for screen ready)
        self.agent_mgr = AgentManager(self._make_options)
        self._wire_agent_manager_callbacks()

        # Initialize file index for fuzzy file search (doesn't need widgets)
        self._cwd = Path.cwd()
        self.file_index = FileIndex(root=self._cwd)
        self._refresh_file_index()

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
        from claudechic.config import get_vi_mode

        if get_vi_mode():
            self._update_vi_mode(True)

        # Connect SDK in background - UI renders while this happens
        self._connect_initial_client()

    def watch_theme(self, theme: str) -> None:
        """Save theme preference when changed."""
        if theme != get_theme():
            set_theme(theme)

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
            cwd=agent.cwd, resume=resume, agent_name=agent.name, model=agent.model
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
        except Exception as e:
            log.warning(f"Failed to fetch SDK commands: {e}")
        self.refresh_context()

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
        """Update context bar from session file (no API call)."""
        agent = self._agent
        if not agent or not agent.session_id:
            self.context_bar.tokens = 0
            return
        tokens = await get_context_from_session(agent.session_id, cwd=agent.cwd)
        if tokens is not None:
            self.context_bar.tokens = tokens

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
        from claudechic.config import get_vi_mode

        enabled = get_vi_mode()
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
        try:
            self.query_one("#image-attachments", ImageAttachments).clear()
        except Exception:
            pass

        # Start async send (returns immediately, callbacks handle UI)
        asyncio.create_task(
            agent.send(prompt, display_as=display_as), name=f"send-{agent.id}"
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
        has_content = (
            agent_count > 1 or self.agent_section._worktrees or self.todo_panel.todos
        )
        width = self.size.width
        main = self.query_one("#main", Horizontal)

        # Check if any agent needs attention (for hamburger color)
        needs_attention = any(
            a.status == AgentStatus.NEEDS_INPUT for a in self.agents.values()
        )

        if width >= self.SIDEBAR_MIN_WIDTH and has_content:
            # Wide enough - show sidebar inline, hide hamburger
            self.right_sidebar.remove_class("hidden")
            self.right_sidebar.remove_class("overlay")
            self.hamburger_btn.remove_class("visible")
            self._sidebar_overlay_open = False
            # Layout sidebar contents based on available space
            self._layout_sidebar_contents()
            # Wide enough to center chat while showing sidebar
            if width >= self.CENTERED_SIDEBAR_WIDTH:
                main.remove_class("sidebar-shift")
            else:
                # Shift left to make room for sidebar
                main.add_class("sidebar-shift")
        elif has_content:
            # Narrow but has content - show hamburger, sidebar as overlay when open
            main.remove_class("sidebar-shift")

            if self._sidebar_overlay_open:
                # Sidebar open - hide hamburger, show sidebar
                self.hamburger_btn.remove_class("visible")
                self.right_sidebar.remove_class("hidden")
                self.right_sidebar.add_class("overlay")
                # Layout sidebar contents
                self._layout_sidebar_contents()
            else:
                # Sidebar closed - show hamburger
                self.hamburger_btn.add_class("visible")
                if needs_attention:
                    self.hamburger_btn.add_class("needs-attention")
                else:
                    self.hamburger_btn.remove_class("needs-attention")
                self.right_sidebar.add_class("hidden")
                self.right_sidebar.remove_class("overlay")
        else:
            # No content - hide everything
            self.right_sidebar.add_class("hidden")
            self.right_sidebar.remove_class("overlay")
            self.hamburger_btn.remove_class("visible")
            self._sidebar_overlay_open = False
            main.remove_class("sidebar-shift")

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
                if compact:
                    widget.add_class("compact-height")
                else:
                    widget.remove_class("compact-height")
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

    def _check_and_copy_selection(self) -> None:
        selected = self.screen.get_selected_text()
        if selected and len(selected.strip()) > 0:
            self.copy_to_clipboard(selected)

    def action_quit(self) -> None:  # type: ignore[override]
        # If history search is visible, cancel it
        try:
            hs = self.query_one("#history-search", HistorySearch)
            if hs.styles.display != "none":
                hs.action_cancel()
                return
        except Exception:
            pass

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
        try:
            chat_input = self.query_one("ChatInput", ChatInput)
            if chat_input.text:
                chat_input.text = ""
                return
        except Exception:
            pass  # No input widget or not mounted

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

        # Suppress SDK stderr noise during exit (stream closed errors)
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

                tip_task = asyncio.create_task(show_tip_after_delay())

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

    def on_auto_edit_label_toggled(self, event: AutoEditLabel.Toggled) -> None:
        """Handle auto-edit label press - toggle auto-edit mode."""
        self.action_cycle_permission_mode()

    def on_model_label_model_change_requested(
        self, event: ModelLabel.ModelChangeRequested
    ) -> None:
        """Handle model label press - open model selector."""
        self._handle_model_prompt()

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
            cwd=agent.cwd, resume=session_id, agent_name=agent.name, model=agent.model
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
            cwd=agent.cwd, agent_name=agent.name, model=agent.model
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
            await agent.disconnect()
            options = self._make_options(
                cwd=agent.cwd, agent_name=agent.name, model=model
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

    @work(group="new_agent", exclusive=True, exit_on_error=False)
    async def _create_new_agent(
        self,
        name: str,
        cwd: Path,
        worktree: str | None = None,
        auto_resume: bool = False,
        switch_to: bool = True,
        model: str | None = None,
    ) -> None:
        """Create a new agent via AgentManager.

        Args:
            name: Display name for the agent
            cwd: Working directory
            worktree: Git worktree branch name if applicable
            auto_resume: Try to resume session with most messages in cwd
            switch_to: Whether to switch to the new agent (default True)
            model: Model override (None = SDK default)
        """
        if self.agent_mgr is None:
            self.notify("Agent manager not initialized", severity="error")
            return

        try:
            # Resolve resume ID if auto_resume
            resume_id = None
            if auto_resume:
                sessions = await get_recent_sessions(limit=100, cwd=cwd)
                if sessions:
                    # Pick session with most messages (index 3)
                    best = max(sessions, key=lambda s: s[3])
                    resume_id = best[0]

            # Create agent via AgentManager (handles SDK connection, UI callbacks)
            await self.agent_mgr.create(
                name=name,
                cwd=cwd,
                worktree=worktree,
                resume=resume_id,
                switch_to=switch_to,
                model=model,
            )
        except Exception as e:
            self.show_error(f"Failed to create agent '{name}'", e)
            return

        if resume_id:
            await self._load_and_display_history(resume_id, cwd=cwd)
            self.notify(f"Resumed session in '{name}'")
        else:
            label = f"Worktree '{name}'" if worktree else f"Agent '{name}'"
            self.notify(f"{label} ready")

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
            asyncio.create_task(self._async_refresh_files(agent))
        except Exception as e:
            log.exception(f"Failed to create agent UI: {e}")

    def on_agent_switched(self, new_agent: Agent, old_agent: Agent | None) -> None:
        """Handle agent switch from AgentManager."""
        log.info(f"Switched to agent: {new_agent.name}")

        # Batch all class changes to trigger single CSS recalculation
        with self.batch_update():
            # Save current input and hide old agent's UI
            if old_agent:
                old_agent.pending_input = self.chat_input.text
                old_chat_view = self._chat_views.get(old_agent.id)
                if old_chat_view:
                    old_chat_view.add_class("hidden")
                old_prompt = self._active_prompts.get(old_agent.id)
                if old_prompt:
                    old_prompt.add_class("hidden")

            # Show new agent's chat view
            new_chat_view = self._chat_views.get(new_agent.id)
            if new_chat_view:
                new_chat_view.remove_class("hidden")

            # Restore new agent's input
            self.chat_input.text = new_agent.pending_input

            # Show new agent's prompt if it has one, otherwise show input
            active_prompt = self._active_prompts.get(new_agent.id)
            if active_prompt:
                active_prompt.remove_class("hidden")
                active_prompt.focus()
                self.input_container.add_class("hidden")
            else:
                self.input_container.remove_class("hidden")

            # Update sidebar
            try:
                self.agent_section.set_active(new_agent.id)
            except Exception:
                pass

            # Update footer
            self.status_footer.auto_edit = new_agent.auto_approve_edits
            self._update_footer_model(new_agent.model)

            # Update todo panel and context
            self.todo_panel.update_todos(new_agent.todos)
            self.refresh_context()

            # Update plan button
            self.plan_section.set_plan(new_agent.plan_path)
            self._position_right_sidebar()

        # These happen outside batch (async/focus)
        asyncio.create_task(self._async_refresh_files(new_agent))
        asyncio.create_task(self.status_footer.refresh_branch(str(new_agent.cwd)))
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

        try:
            self.agent_section.remove_agent(agent_id)
        except Exception:
            pass
        self._position_right_sidebar()

    def on_status_changed(self, agent: Agent) -> None:
        """Handle agent status change."""
        try:
            self.agent_section.update_status(agent.id, agent.status)
            # Update hamburger color if any agent needs attention
            self._update_hamburger_attention()
        except Exception:
            log.debug(f"Failed to update sidebar status for agent {agent.id}")

    def on_auto_edit_changed(self, agent: Agent) -> None:
        """Handle auto-edit mode change."""
        # Only update footer if this is the active agent
        if self._agent and agent.id == self._agent.id:
            self._update_footer_auto_edit()

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
        chat_view = self._chat_views.get(agent.id)
        if chat_view:
            chat_view.append_text(text, new_message, parent_tool_use_id)

    def on_tool_use(self, agent: Agent, tool: ToolUse) -> None:
        """Handle tool use from agent - post Textual Message for UI."""
        # Clear pending slash command if Skill tool was invoked (valid command)
        if tool.name == ToolName.SKILL:
            self._pending_slash_commands.pop(agent.id, None)

        block = ToolUseBlock(id=tool.id, name=tool.name, input=tool.input)
        self.post_message(
            ToolUseMessage(block, parent_tool_use_id=None, agent_id=agent.id)
        )

    def on_tool_result(self, agent: Agent, tool: ToolUse) -> None:
        """Handle tool result from agent - post Textual Message for UI."""
        from claude_agent_sdk import ToolResultBlock

        block = ToolResultBlock(
            tool_use_id=tool.id, content=tool.result or "", is_error=tool.is_error
        )
        self.post_message(
            ToolResultMessage(block, parent_tool_use_id=None, agent_id=agent.id)
        )

        # Show tool errors prominently in chat
        if tool.is_error:
            error_preview = (tool.result or "Unknown error")[:200]
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

            asyncio.create_task(ui_response())
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

    def _update_footer_model(self, model: str | None) -> None:
        """Update footer to show agent's model."""
        if not self._available_models:
            # No model info yet - show raw value or empty
            self.status_footer.model = model.capitalize() if model else ""
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
        self.status_footer.model = model_name

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
        try:
            chat_input = self.query_one("#input", ChatInput)
            chat_input.enable_vi_mode(enabled)
            # Update footer with initial mode
            mode = chat_input.vi_mode if enabled else None
            self.status_footer.update_vi_mode(mode, enabled)
        except Exception:
            pass
