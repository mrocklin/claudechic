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


class AutoEditLabel(ClickableLabel):
    """Clickable auto-edit status label."""

    class Toggled(Message):
        """Emitted when auto-edit is toggled."""

    def on_click(self, event) -> None:
        self.post_message(self.Toggled())


class ModelLabel(ClickableLabel):
    """Clickable model label."""

    class ModelChangeRequested(Message):
        """Emitted when user wants to change the model."""

    def on_click(self, event) -> None:
        self.post_message(self.ModelChangeRequested())


class ViModeLabel(Static):
    """Shows current vi mode: INSERT, NORMAL, VISUAL."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._mode: ViMode | None = None
        self._enabled: bool = False

    def set_mode(self, mode: ViMode | None, enabled: bool = True) -> None:
        """Update the displayed mode."""
        self._mode = mode
        self._enabled = enabled

        # Remove all mode classes
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
    auto_edit = reactive(False)
    model = reactive("")
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
            yield Static("·", classes="footer-sep")
            yield AutoEditLabel(
                "Auto-edit: off", id="auto-edit-label", classes="footer-label"
            )
            yield Static("", id="footer-spacer")
            yield ProcessIndicator(id="process-indicator", classes="hidden")
            yield ContextBar(id="context-bar")
            yield CPUBar(id="cpu-bar")
            yield Static("", id="branch-label", classes="footer-label")

    def watch_branch(self, value: str) -> None:
        """Update branch label when branch changes."""
        try:
            label = self.query_one("#branch-label", Static)
            label.update(f"⎇ {value}" if value else "")
        except Exception:
            pass

    def watch_model(self, value: str) -> None:
        """Update model label when model changes."""
        try:
            label = self.query_one("#model-label", ModelLabel)
            label.update(value if value else "")
        except Exception:
            pass

    def watch_auto_edit(self, value: bool) -> None:
        """Update auto-edit label when setting changes."""
        try:
            label = self.query_one("#auto-edit-label", AutoEditLabel)
            label.update("Auto-edit: on" if value else "Auto-edit: off")
            label.set_class(value, "active")
        except Exception:
            pass

    def update_processes(self, processes: list[BackgroundProcess]) -> None:
        """Update the process indicator."""
        try:
            indicator = self.query_one("#process-indicator", ProcessIndicator)
            indicator.update_processes(processes)
        except Exception:
            pass

    def update_vi_mode(self, mode: ViMode | None, enabled: bool = True) -> None:
        """Update the vi-mode indicator."""
        try:
            label = self.query_one("#vi-mode-label", ViModeLabel)
            label.set_mode(mode, enabled)
        except Exception:
            pass
