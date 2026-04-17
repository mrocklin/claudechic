"""Custom footer widget."""

import asyncio

from textual.app import ComposeResult
from textual.message import Message
from textual.reactive import reactive
from textual.containers import Horizontal
from textual.widgets import Static

from claudechic.widgets.base.clickable import ClickableLabel
from claudechic.widgets.layout.indicators import CPUBar, ContextBar, ProcessIndicator
from claudechic.processes import BackgroundProcess
from claudechic.widgets.input.vi_mode import ViMode


class PermissionModeLabel(ClickableLabel):
    """Clickable permission mode status label."""

    class Toggled(Message):
        """Emitted when permission mode is toggled."""

    def on_click(self, event) -> None:
        self.post_message(self.Toggled())


class ModelLabel(ClickableLabel):
    """Clickable model label."""

    class ModelChangeRequested(Message):
        """Emitted when user wants to change the model."""

    def on_click(self, event) -> None:
        self.post_message(self.ModelChangeRequested())


class EffortLabel(ClickableLabel):
    """Clickable effort label. Shows current effort level, opens EffortPrompt on click."""

    class EffortChangeRequested(Message):
        """Emitted when user wants to change the effort level."""

    def on_click(self, event) -> None:
        self.post_message(self.EffortChangeRequested())


class ViModeLabel(Static):
    """Shows current vim mode: INSERT, NORMAL, VISUAL."""

    DEFAULT_CSS = """
    ViModeLabel {
        width: auto;
        padding: 0 1;
        text-style: bold;
        &.vi-insert { color: $success; }
        &.vi-normal { color: $primary; }
        &.vi-visual { color: $warning; }
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._mode: ViMode | None = None
        self._enabled: bool = False

    def set_mode(self, mode: ViMode | None, enabled: bool = True) -> None:
        """Update the displayed mode."""
        self._mode = mode
        self._enabled = enabled

        self.remove_class("vi-insert", "vi-normal", "vi-visual", "hidden")

        if not enabled:
            self.add_class("hidden")
            return

        if mode == ViMode.INSERT:
            self.update("INSERT")
            self.add_class("vi-insert")
        elif mode == ViMode.NORMAL:
            self.update("NORMAL")
            self.add_class("vi-normal")
        elif mode == ViMode.VISUAL:
            self.update("VISUAL")
            self.add_class("vi-visual")


async def get_git_branch(cwd: str | None = None) -> str:
    """Get current git branch name (async)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "branch",
            "--show-current",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=1)
        return stdout.decode().strip() or "detached"
    except Exception:
        return ""


class StatusFooter(Static):
    """Footer showing git branch, model, auto-edit status, and resource indicators."""

    can_focus = False
    permission_mode = reactive("default")  # default, acceptEdits, plan, planSwarm, auto
    model = reactive("")
    # "" = SDK default (shown muted), else low/medium/high/xhigh/max
    effort = reactive("")
    branch = reactive("")

    async def on_mount(self) -> None:
        self.branch = await get_git_branch()

    async def refresh_branch(self, cwd: str | None = None) -> None:
        """Update branch from given directory (async)."""
        self.branch = await get_git_branch(cwd)

    def compose(self) -> ComposeResult:
        with Horizontal(id="footer-content"):
            yield ViModeLabel("", id="vi-mode-label", classes="hidden")
            yield ModelLabel("", id="model-label", classes="footer-label")
            yield EffortLabel("default", id="effort-label", classes="footer-label")
            yield Static("·", classes="footer-sep")
            yield PermissionModeLabel(
                "Auto-edit: off", id="permission-mode-label", classes="footer-label"
            )
            yield Static("", id="footer-spacer")
            yield ProcessIndicator(id="process-indicator", classes="hidden")
            yield ContextBar(id="context-bar")
            yield CPUBar(id="cpu-bar")
            yield Static("", id="branch-label", classes="footer-label")

    def watch_branch(self, value: str) -> None:
        """Update branch label when branch changes."""
        if label := self.query_one_optional("#branch-label", Static):
            label.update(f"⎇ {value}" if value else "")

    def watch_model(self, value: str) -> None:
        """Update model label when model changes."""
        if label := self.query_one_optional("#model-label", ModelLabel):
            label.update(value if value else "")

    def watch_effort(self, value: str) -> None:
        """Update effort label. Shows "default" muted when unset, value elevated when set."""
        if label := self.query_one_optional("#effort-label", EffortLabel):
            label.update(value or "default")
            label.set_class(bool(value), "elevated")

    # Maps permission_mode → (display text, active CSS class or None).
    # "default" gets no class and keeps plain styling. Adding a new mode
    # means one entry here; _MODE_CLASSES is derived below.
    _MODE_DISPLAY: dict[str, tuple[str, str | None]] = {
        "default": ("Auto-edit: off", None),
        "planSwarm": ("Plan swarm", "plan-swarm-mode"),
        "plan": ("Plan mode", "plan-mode"),
        "acceptEdits": ("Auto-edit: on", "active"),
        "auto": ("Auto", "auto-mode"),
    }
    _MODE_CLASSES = tuple(cls for _, cls in _MODE_DISPLAY.values() if cls)

    def watch_permission_mode(self, value: str) -> None:
        """Update permission mode label when setting changes."""
        if label := self.query_one_optional(
            "#permission-mode-label", PermissionModeLabel
        ):
            text, active = self._MODE_DISPLAY.get(value, self._MODE_DISPLAY["default"])
            label.update(text)
            for cls in self._MODE_CLASSES:
                label.set_class(cls == active, cls)

    def update_processes(self, processes: list[BackgroundProcess]) -> None:
        """Update the process indicator."""
        if indicator := self.query_one_optional("#process-indicator", ProcessIndicator):
            indicator.update_processes(processes)

    def update_vi_mode(self, mode: ViMode | None, enabled: bool = True) -> None:
        """Update the vi-mode indicator."""
        if label := self.query_one_optional("#vi-mode-label", ViModeLabel):
            label.set_mode(mode, enabled)
