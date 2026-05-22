"""Agent sidebar widget for multi-agent management."""

import re
import time
from pathlib import Path

from textual.app import ComposeResult
from textual.events import Click
from textual.message import Message
from textual.reactive import reactive
from textual.timer import Timer
from textual.widget import Widget
from textual.widgets import Static, Label, ListItem
from rich.text import Text

from claudechic.enums import AgentStatus
from claudechic.widgets.primitives.button import Button


class SidebarItem(Widget):
    """Base class for clickable sidebar items."""

    DEFAULT_CSS = """
    SidebarItem {
        height: 3;
        min-height: 3;
        padding: 1 1 1 2;
        pointer: pointer;
    }
    SidebarItem.compact {
        height: 1;
        min-height: 1;
        padding: 0 1 0 2;
    }
    SidebarItem:hover {
        background: $surface-lighten-1;
    }
    """

    max_name_length: int = 16

    def truncate_name(self, name: str) -> str:
        """Truncate name with ellipsis if too long."""
        if len(name) > self.max_name_length:
            return name[: self.max_name_length - 1] + "…"
        return name


class SidebarSection(Widget):
    """Base component for sidebar sections with a title and items."""

    DEFAULT_CSS = """
    SidebarSection {
        width: 100%;
        height: auto;
        padding: 0;
    }
    SidebarSection .section-title {
        color: $text-muted;
        text-style: bold;
        padding: 1 1 1 1;
    }
    SidebarSection.hidden {
        display: none;
    }
    """

    def __init__(self, title: str, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._title = title

    def compose(self) -> ComposeResult:
        yield Static(self._title, classes="section-title")


def _format_time_ago(mtime: float) -> str:
    """Format a timestamp as relative time (e.g., '2 hours ago')."""
    delta = time.time() - mtime
    if delta < 60:
        return "just now"
    elif delta < 3600:
        mins = int(delta / 60)
        return f"{mins} min{'s' if mins != 1 else ''} ago"
    elif delta < 86400:
        hours = int(delta / 3600)
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif delta < 604800:
        days = int(delta / 86400)
        return f"{days} day{'s' if days != 1 else ''} ago"
    else:
        weeks = int(delta / 604800)
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"


class SessionItem(ListItem):
    """A session in the session picker sidebar."""

    DEFAULT_CSS = """
    SessionItem {
        pointer: pointer;
    }
    """

    def __init__(
        self, session_id: str, title: str, mtime: float, msg_count: int = 0
    ) -> None:
        super().__init__()
        self.session_id = session_id
        self.title = title
        self.mtime = mtime
        self.msg_count = msg_count

    def compose(self) -> ComposeResult:
        yield Label(self.title, classes="session-preview")
        time_ago = _format_time_ago(self.mtime)
        yield Label(f"{time_ago} · {self.msg_count} msgs", classes="session-meta")


class HamburgerButton(Button):
    """Floating hamburger button for narrow screens."""

    class SidebarToggled(Message):
        """Posted when hamburger is pressed to toggle sidebar."""

        pass

    DEFAULT_CSS = """
    HamburgerButton {
        layer: above;
        width: 16;
        height: 3;
        content-align: center middle;
        background: $panel;
        color: $text-muted;
        display: none;
        /* Position top-right */
        offset: -1 1;
        dock: right;
        border: round $panel;
    }
    HamburgerButton:hover {
        color: $text;
    }
    HamburgerButton.visible {
        display: block;
    }
    HamburgerButton.needs-attention {
        color: $primary;
        border: round $primary;
    }
    """

    def __init__(self, id: str | None = None) -> None:
        super().__init__(id=id)

    def render(self) -> str:
        return "Open Sidebar"

    def on_click(self, event) -> None:
        self.post_message(self.SidebarToggled())


class PlanItem(SidebarItem):
    """Clickable plan item that opens the plan file."""

    class PlanRequested(Message):
        """Posted when plan item is clicked."""

        def __init__(self, plan_path: Path) -> None:
            self.plan_path = plan_path
            super().__init__()

    max_name_length: int = 18

    def __init__(self, plan_path: Path) -> None:
        super().__init__()
        self.plan_path = plan_path

    def render(self) -> Text:
        name = self.truncate_name(self.plan_path.name)
        return Text.assemble(("📋", ""), " ", (name, ""))

    def on_click(self, event) -> None:
        self.post_message(self.PlanRequested(self.plan_path))


class FileItem(SidebarItem):
    """An edited file in the sidebar."""

    DEFAULT_CSS = """
    FileItem {
        height: 1;
        min-height: 1;
        padding: 0 1 0 2;
    }
    FileItem:hover {
        background: $surface-lighten-1;
    }
    """

    class Selected(Message):
        """Posted when file is clicked.

        - plain click: diff vs HEAD (uncommitted changes)
        - ctrl+click: diff this branch vs its base branch
        - double-click: open the file in $EDITOR
        """

        def __init__(
            self,
            file_path: Path,
            *,
            ctrl: bool = False,
            double_click: bool = False,
        ) -> None:
            self.file_path = file_path
            self.ctrl = ctrl
            self.double_click = double_click
            super().__init__()

    # Wait this long after a single click before firing it, so a double-click
    # can cancel and supersede. Trade-off: single-click diff has ~300ms lag.
    DOUBLE_CLICK_INTERVAL = 0.3

    max_name_length: int = 14

    def __init__(
        self,
        file_path: Path,
        additions: int = 0,
        deletions: int = 0,
        untracked: bool = False,
    ) -> None:
        super().__init__()
        self.file_path = file_path
        self.additions = additions
        self.deletions = deletions
        self.untracked = untracked
        self._click_timer: Timer | None = None

    def _truncate_front(self, name: str) -> str:
        """Truncate from front with ellipsis if too long."""
        if len(name) > self.max_name_length:
            return "…" + name[-(self.max_name_length - 1) :]
        return name

    def render(self) -> Text:
        """Render the file item text."""
        name = self._truncate_front(str(self.file_path))
        parts: list[tuple[str, str]] = []
        if self.untracked:
            parts.append(("U ", "dim yellow"))
        parts.append((name, "dim"))
        if self.additions:
            parts.append((f" +{self.additions}", "dim green"))
        if self.deletions:
            parts.append((f" -{self.deletions}", "dim red"))
        return Text.assemble(*parts)

    def on_click(self, event: Click) -> None:
        # Double-click supersedes any pending single-click action.
        if event.chain >= 2:
            if self._click_timer is not None:
                self._click_timer.stop()
                self._click_timer = None
            self.post_message(self.Selected(self.file_path, double_click=True))
            return

        # Single click: defer briefly so a second click can cancel and upgrade
        # this into a double-click before the diff screen pushes.
        if self._click_timer is not None:
            self._click_timer.stop()
        ctrl = event.ctrl

        def fire_single() -> None:
            self._click_timer = None
            self.post_message(self.Selected(self.file_path, ctrl=ctrl))

        self._click_timer = self.set_timer(self.DOUBLE_CLICK_INTERVAL, fire_single)

    def on_unmount(self) -> None:
        # Pending click timer would otherwise fire on a removed widget.
        if self._click_timer is not None:
            self._click_timer.stop()
            self._click_timer = None


class FilesSection(SidebarSection):
    """Sidebar section for edited files."""

    DEFAULT_CSS = """
    FilesSection {
        border-top: solid $panel;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__("Files", *args, **kwargs)
        self._files: dict[Path, FileItem] = {}  # path -> item
        self._compact = False

    @property
    def item_count(self) -> int:
        """Number of files in the section."""
        return len(self._files)

    def set_compact(self, compact: bool) -> None:
        """Set compact mode for all items."""
        if self._compact == compact:
            return
        self._compact = compact
        for item in self._files.values():
            item.set_class(compact, "compact")

    def _make_file_item(
        self, file_path: Path, additions: int, deletions: int, untracked: bool = False
    ) -> FileItem:
        """Create a FileItem with proper ID and styling."""
        item = FileItem(file_path, additions, deletions, untracked)
        safe_id = str(file_path).replace("/", "-").replace(".", "-").replace(" ", "-")
        item.id = f"file-{safe_id}"
        item.set_class(self._compact, "compact")
        return item

    def add_file(self, file_path: Path, additions: int = 0, deletions: int = 0) -> None:
        """Add or update a file in the section."""
        if file_path in self._files:
            item = self._files[file_path]
            item.additions += additions
            item.deletions += deletions
            item.refresh()
        else:
            item = self._make_file_item(file_path, additions, deletions)
            self._files[file_path] = item
            self.mount(item)
        if self._files:
            self.remove_class("hidden")

    def mount_all_files(self, files: dict[Path, tuple[int, int, bool]]) -> None:
        """Mount multiple files at once."""
        items = []
        for file_path, (additions, deletions, untracked) in files.items():
            if file_path not in self._files:
                item = self._make_file_item(file_path, additions, deletions, untracked)
                self._files[file_path] = item
                items.append(item)
        if items:
            self.mount(*items)
            self.remove_class("hidden")

    def clear(self) -> None:
        """Remove all files from the section (sync)."""
        for item in self._files.values():
            item.remove()
        self._files.clear()
        self.add_class("hidden")

    async def async_clear(self) -> None:
        """Remove all files from the section (async, awaits removal)."""
        if self._files:
            items = list(self._files.values())
            self._files.clear()
            for item in items:
                await item.remove()


class PlanSection(SidebarSection):
    """Sidebar section for plan files."""

    DEFAULT_CSS = """
    PlanSection {
        border-top: solid $panel;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__("Plan", *args, **kwargs)
        self._plan_item: PlanItem | None = None
        self._plan_path: Path | None = None

    @property
    def has_plan(self) -> bool:
        """Whether a plan is set (regardless of visibility)."""
        return self._plan_path is not None

    def set_plan(self, plan_path: Path | None) -> None:
        """Set the plan path. Visibility is controlled by set_visible()."""
        self._plan_path = plan_path
        if plan_path:
            if self._plan_item is None:
                self._plan_item = PlanItem(plan_path)
                self.mount(self._plan_item)
            else:
                self._plan_item.plan_path = plan_path
                self._plan_item.refresh()
        else:
            if self._plan_item is not None:
                self._plan_item.remove()
                self._plan_item = None
            self.add_class("hidden")

    def set_visible(self, visible: bool) -> None:
        """Control visibility (only shows if has plan and visible=True)."""
        if visible and self._plan_path:
            self.remove_class("hidden")
        else:
            self.add_class("hidden")


class WorktreeItem(SidebarItem):
    """A ghost worktree in the sidebar (not yet an agent)."""

    class Selected(Message):
        """Posted when worktree is clicked."""

        def __init__(self, branch: str, path: Path) -> None:
            self.branch = branch
            self.path = path
            super().__init__()

    def __init__(self, branch: str, path: Path) -> None:
        super().__init__()
        self.branch = branch
        self.path = path

    def render(self) -> Text:
        name = self.truncate_name(self.branch)
        return Text.assemble(("◌", ""), " ", (name, "dim"))

    def on_click(self, event) -> None:
        self.post_message(self.Selected(self.branch, self.path))


class AgentItem(SidebarItem):
    """A single agent in the sidebar."""

    class Selected(Message):
        """Posted when agent is clicked."""

        def __init__(self, agent_id: str) -> None:
            self.agent_id = agent_id
            super().__init__()

    class CloseRequested(Message):
        """Posted when close button is clicked."""

        def __init__(self, agent_id: str) -> None:
            self.agent_id = agent_id
            super().__init__()

    DEFAULT_CSS = """
    AgentItem {
        layout: horizontal;
    }
    AgentItem.active {
        padding: 1 1 1 1;
        border-left: wide $primary;
        background: $surface;
    }
    AgentItem.active.compact {
        padding: 0 1 0 1;
    }
    AgentItem .agent-label {
        width: 1fr;
        height: 1;
        overflow: hidden;
        text-overflow: ellipsis;
    }
    AgentItem .agent-close {
        width: 3;
        min-width: 3;
        height: 1;
        padding: 0;
        background: $panel;
        color: $text-muted;
        text-align: center;
    }
    AgentItem .agent-close:hover {
        color: $error;
        background: $panel-lighten-1;
    }
    """

    max_name_length: int = 14

    status: reactive[AgentStatus] = reactive(AgentStatus.IDLE)

    def __init__(
        self, agent_id: str, display_name: str, status: AgentStatus = AgentStatus.IDLE
    ) -> None:
        super().__init__()
        self.agent_id = agent_id
        self.display_name = display_name
        self.status = status

    def compose(self) -> ComposeResult:
        yield Static(self._render_label(), classes="agent-label")
        yield Static(Text("X"), classes="agent-close")

    def _render_label(self) -> Text:
        if self.status == AgentStatus.BUSY:
            indicator = "\u25cf"
            style = ""  # default text color
        elif self.status == AgentStatus.NEEDS_INPUT:
            indicator = "\u25cf"
            style = self.app.current_theme.primary if self.app else "bold"
        else:
            indicator = "\u25cb"
            style = "dim"
        name = self.truncate_name(self.display_name)
        return Text.assemble((indicator, style), " ", (name, ""))

    def watch_status(self, _status: str) -> None:
        """Update label when status changes."""
        if label := self.query_one_optional(".agent-label", Static):
            label.update(self._render_label())

    def on_click(self, event: Click) -> None:
        """Handle clicks - check if on close button."""
        if event.widget and event.widget.has_class("agent-close"):
            event.stop()
            self.post_message(self.CloseRequested(self.agent_id))
        else:
            self.post_message(self.Selected(self.agent_id))


class AgentSection(SidebarSection):
    """Sidebar section showing all agents with status indicators."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__("Agents", *args, **kwargs)
        self._agents: dict[str, AgentItem] = {}
        self._worktrees: dict[str, WorktreeItem] = {}  # branch -> item
        self._compact = False

    @property
    def item_count(self) -> int:
        """Total number of items (agents + worktrees)."""
        return len(self._agents) + len(self._worktrees)

    def set_compact(self, compact: bool) -> None:
        """Set compact mode for all items."""
        if self._compact == compact:
            return
        self._compact = compact
        for item in self._agents.values():
            item.set_class(compact, "compact")
        for item in self._worktrees.values():
            item.set_class(compact, "compact")

    def add_agent(
        self, agent_id: str, name: str, status: AgentStatus = AgentStatus.IDLE
    ) -> None:
        """Add an agent to the sidebar."""
        if agent_id in self._agents:
            return
        # Remove ghost worktree if there's one for this name
        if name in self._worktrees:
            self._worktrees[name].remove()
            del self._worktrees[name]
        item = AgentItem(agent_id, name, status)
        # Sanitize for Textual ID (no slashes allowed)
        item.id = f"agent-{agent_id.replace('/', '-')}"
        # Apply current compact mode to new item
        item.set_class(self._compact, "compact")
        self._agents[agent_id] = item
        self.mount(item)

    def remove_agent(self, agent_id: str) -> None:
        """Remove an agent from the sidebar."""
        if agent_id in self._agents:
            self._agents[agent_id].remove()
            del self._agents[agent_id]

    def set_active(self, agent_id: str) -> None:
        """Mark an agent as active (selected)."""
        # Use update=False to defer CSS recalculation (caller will refresh)
        for aid, item in self._agents.items():
            item.set_class(aid == agent_id, "active", update=False)

    def update_status(self, agent_id: str, status: AgentStatus) -> None:
        """Update an agent's status."""
        if agent_id in self._agents:
            self._agents[agent_id].status = status

    def add_worktree(self, branch: str, path: Path) -> None:
        """Add a ghost worktree to the sidebar."""
        # Skip if already have an agent with this name
        for agent_item in self._agents.values():
            if agent_item.display_name == branch:
                return
        if branch in self._worktrees:
            return
        item = WorktreeItem(branch, path)
        # Sanitize branch name for valid Textual ID (letters, numbers, _, - only)
        safe_id = re.sub(r"[^a-zA-Z0-9_-]", "-", branch)
        item.id = f"worktree-{safe_id}"
        # Apply current compact mode to new item
        item.set_class(self._compact, "compact")
        self._worktrees[branch] = item
        self.mount(item)

    def remove_worktree(self, branch: str) -> None:
        """Remove a ghost worktree from the sidebar."""
        if branch in self._worktrees:
            self._worktrees[branch].remove()
            del self._worktrees[branch]
