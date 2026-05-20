"""Resource indicator widgets - context bar, CPU monitor, and process indicator."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claudechic.usage import UsageInfo

import psutil

from textual.app import RenderResult
from textual.reactive import reactive
from textual.widgets import Static
from rich.text import Text

from claudechic.formatting import DEFAULT_CONTEXT_WINDOW
from claudechic.profiling import profile, timed
from claudechic.processes import BackgroundProcess


class IndicatorWidget(Static):
    """Base class for clickable indicator widgets in the footer.

    Pointer cursor is set via CSS (pointer: pointer).
    Override on_click() to handle click events.
    """

    DEFAULT_CSS = """
    IndicatorWidget {
        pointer: pointer;
    }
    """

    can_focus = True


class CPUBar(IndicatorWidget):
    """Display CPU usage. Click to show profiling stats."""

    cpu_pct = reactive(0.0)

    def on_mount(self) -> None:
        self._process = psutil.Process()
        self._process.cpu_percent()  # Prime the measurement
        self.set_interval(2.0, self._update_cpu)

    @profile
    def _update_cpu(self) -> None:
        try:
            with timed("CPUBar.psutil_call"):
                pct = self._process.cpu_percent()
            # Only update if rounded value changed (avoids unnecessary refresh)
            if round(pct) != round(self.cpu_pct):
                with timed("CPUBar.reactive_set"):
                    self.cpu_pct = pct
        except Exception:
            pass  # Process may have exited

    def render(self) -> RenderResult:
        pct = min(self.cpu_pct / 100.0, 1.0)
        if pct < 0.3:
            color = "dim"
        elif pct < 0.7:
            color = "yellow"
        else:
            color = "red"
        return Text.assemble(("CPU ", "dim"), (f"{self.cpu_pct:3.0f}%", color))

    def on_click(self, event) -> None:
        """Show profile modal on click."""
        from claudechic.widgets.modals.profile import ProfileModal

        self.app.push_screen(ProfileModal())


class ContextBar(IndicatorWidget):
    """Display context usage as a progress bar. Click to run /context."""

    tokens = reactive(0)
    max_tokens = reactive(DEFAULT_CONTEXT_WINDOW)

    def render(self) -> RenderResult:
        pct = min(self.tokens / self.max_tokens, 1.0) if self.max_tokens else 0
        bar_width = 10
        filled = int(pct * bar_width)
        # Fill color intensifies as context usage grows
        theme = self.app.current_theme
        warning = theme.warning if isinstance(theme.warning, str) else "#aaaa00"
        error = theme.error if isinstance(theme.error, str) else "#cc3333"
        # Theme-aware colors
        if theme.dark:
            low_fill, empty_color, empty_text = "#666666", "#333333", "white"
        else:
            low_fill, empty_color, empty_text = "#999999", "#dddddd", "black"
        if pct < 0.5:
            fill_color, text_color = low_fill, empty_text
        elif pct < 0.8:
            fill_color, text_color = warning, "black"
        else:
            fill_color, text_color = error, "white"
        # Center percentage text in bar
        pct_str = f"{pct * 100:.0f}%"
        start = (bar_width - len(pct_str)) // 2
        result = Text()
        for i in range(bar_width):
            bg = fill_color if i < filled else empty_color
            if start <= i < start + len(pct_str):
                fg = text_color if i < filled else empty_text
                result.append(pct_str[i - start], style=f"{fg} on {bg}")
            else:
                result.append(" ", style=f"on {bg}")
        return result

    def on_click(self, event) -> None:
        """Run /context command on click."""
        from claudechic.app import ChatApp

        if isinstance(self.app, ChatApp):
            self.app._handle_prompt("/context")


class UsageIndicator(IndicatorWidget):
    """Display API rate limit as a compact bar in the footer.

    Shows the most constrained (highest utilization) of the 5-hour or 7-day
    limits.  Hidden until the first fetch completes.  Click to open the full
    /usage report.
    """

    DEFAULT_CSS = """
    UsageIndicator {
        width: auto;
    }
    UsageIndicator.hidden {
        display: none;
    }
    """

    # -1 means "not yet fetched / unavailable"
    utilization = reactive(-1.0)
    limit_label = reactive("")  # "5hr" or "7d"

    def update_usage(self, usage: UsageInfo) -> None:
        """Push fresh usage data into the widget."""
        candidates = [
            ("5hr", usage.five_hour),
            ("7d", usage.seven_day),
        ]
        available = [(lbl, lim) for lbl, lim in candidates if lim is not None]
        if not available:
            self.utilization = -1.0
            self.add_class("hidden")
            return

        lbl, lim = max(available, key=lambda x: x[1].utilization)
        self.limit_label = lbl
        self.utilization = lim.utilization
        self.remove_class("hidden")

    # Clockwise fill sequence: 12 o'clock → 3 → 6 → 9 → full.
    # Each character adds one quarter-turn of fill.
    _PIE_STEPS = [
        (0.20, "○"),  #   0–20 %: empty ring
        (0.40, "◔"),  #  20–40 %: upper-right quadrant (12→3)
        (0.60, "◑"),  #  40–60 %: right half (12→6)
        (0.80, "◕"),  #  60–80 %: all but upper-left (12→9)
        (1.01, "●"),  #  80–100%: full disc
    ]

    def render(self) -> RenderResult:
        if self.utilization < 0:
            return Text("")

        pct = min(self.utilization / 100.0, 1.0)

        pie = next(ch for threshold, ch in self._PIE_STEPS if pct < threshold)

        theme = self.app.current_theme
        warning = theme.warning if isinstance(theme.warning, str) else "#aaaa00"
        error = theme.error if isinstance(theme.error, str) else "#cc3333"
        muted = "#888888" if theme.dark else "#666666"

        if pct < 0.5:
            color = muted
        elif pct < 0.8:
            color = warning
        else:
            color = error

        result = Text()
        result.append(pie, style=color)
        result.append(f" {self.limit_label}", style="dim")
        return result

    def on_click(self, event) -> None:
        """Show the full /usage report on click."""
        from claudechic.app import ChatApp

        if isinstance(self.app, ChatApp):
            self.app._handle_usage_command()


class ProcessIndicator(IndicatorWidget):
    """Display count of background processes. Click to show details."""

    DEFAULT_CSS = """
    ProcessIndicator {
        width: auto;
        padding: 0 1;
    }
    ProcessIndicator:hover {
        background: $panel;
    }
    ProcessIndicator.hidden {
        display: none;
    }
    """

    count = reactive(0)

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._processes: list[BackgroundProcess] = []

    def update_processes(self, processes: list[BackgroundProcess]) -> None:
        """Update the process list and count."""
        self._processes = processes
        self.count = len(processes)
        self.set_class(self.count == 0, "hidden")

    def render(self) -> RenderResult:
        return Text.assemble(("⚙ ", "yellow"), (f"{self.count}", ""))

    def on_click(self, event) -> None:
        """Show process modal on click."""
        from claudechic.widgets.modals.process_modal import ProcessModal

        self.app.push_screen(ProcessModal(self._processes))
