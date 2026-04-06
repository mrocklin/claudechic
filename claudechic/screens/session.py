"""Session browser screen."""

from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.screen import Screen
from textual.widgets import ListView, Input, Static

from claudechic.widgets.layout.sidebar import SessionItem


class SessionScreen(Screen[str | None]):
    """Full-screen session browser for resuming sessions.

    Args:
        cwd: Project directory to filter sessions by. If None, uses Path.cwd().
    """

    def __init__(self, cwd: Path | None = None) -> None:
        super().__init__()
        self._cwd = cwd

    BINDINGS = [
        Binding("escape", "go_back", "Back"),
    ]

    DEFAULT_CSS = """
    SessionScreen {
        background: $background;
        align: center top;
    }

    SessionScreen #session-container {
        width: 100%;
        max-width: 80;
        height: 100%;
        padding: 1 2;
    }

    SessionScreen #session-title {
        height: 1;
        margin-bottom: 1;
        text-style: bold;
    }

    SessionScreen #session-search {
        height: 3;
        margin-bottom: 1;
    }

    SessionScreen #session-list,
    SessionScreen #session-list:focus {
        height: 1fr;
        background: transparent;
    }

    SessionScreen #session-list > SessionItem {
        padding: 0 0 0 1;
        height: auto;
        margin: 0 0 1 0;
        border-left: tall $panel;
    }

    SessionScreen #session-list > SessionItem:hover,
    SessionScreen #session-list > SessionItem.-highlight {
        background: $surface-darken-1;
        border-left: tall $primary;
    }

    SessionScreen .session-meta {
        color: $text-muted;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="session-container"):
            yield Static("Resume Session", id="session-title")
            yield Input(placeholder="Search sessions...", id="session-search")
            yield ListView(id="session-list")

    def on_mount(self) -> None:
        from claudechic.sessions import count_sessions

        total = count_sessions(self._cwd)
        title = self.query_one("#session-title", Static)
        title.update(f"Resume Session ({total} sessions)")
        self._update_list("")
        self.query_one("#session-search", Input).focus()

    def on_key(self, event) -> None:
        """Forward navigation keys to list."""
        list_view = self.query_one("#session-list", ListView)
        if event.key == "down":
            list_view.action_cursor_down()
            event.prevent_default()
        elif event.key == "up":
            list_view.action_cursor_up()
            event.prevent_default()

    def action_go_back(self) -> None:
        """Return to chat without selecting a session."""
        self.dismiss(None)

    async def _fetch_sessions(self, search: str) -> list[tuple[str, str, float, int, str]]:
        from claudechic.sessions import get_recent_sessions

        return await get_recent_sessions(search=search, cwd=self._cwd)

    def _update_list(self, search: str) -> None:
        self.run_worker(self._do_update(search))

    async def _do_update(self, search: str) -> None:
        sessions = await self._fetch_sessions(search)
        list_view = self.query_one("#session-list", ListView)
        list_view.clear()
        for session_id, title, mtime, msg_count, last_msg in sessions:
            list_view.append(SessionItem(session_id, title, mtime, msg_count, last_msg))
        if sessions:
            list_view.index = 0

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "session-search":
            self._update_list(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in search box selects first item."""
        if event.input.id == "session-search":
            list_view = self.query_one("#session-list", ListView)
            if list_view.index is not None and list_view.highlighted_child:
                item = list_view.highlighted_child
                list_view.post_message(
                    ListView.Selected(list_view, item, list_view.index)
                )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, SessionItem):
            self.dismiss(event.item.session_id)
