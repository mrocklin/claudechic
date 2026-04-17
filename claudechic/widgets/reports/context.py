"""Context report widget for /context command output."""

import re
from textual.app import ComposeResult
from textual.widget import Widget
from textual.widgets import Static

from claudechic.formatting import DEFAULT_CONTEXT_WINDOW


def parse_context_markdown(content: str) -> dict:
    """Parse context markdown into structured data.

    ``tokens_total`` comes from the rendered markdown's ``**Tokens:**`` line
    (whatever Claude Code reports for the active model, including the 1M
    beta window for Opus 4.7). Only falls back to ``DEFAULT_CONTEXT_WINDOW``
    if that line is missing from the markdown.
    """
    # Parse model line
    model = ""
    model_match = re.search(r"\*\*Model:\*\*\s*(\S+)", content)
    if model_match:
        model = model_match.group(1)

    data = {
        "model": model,
        "tokens_used": 0,
        "tokens_total": DEFAULT_CONTEXT_WINDOW,
        "categories": [],
    }

    # Parse tokens line: **Tokens:** 18.4k / 200.0k (9%) or 184.2k / 1000.0k
    tokens_match = re.search(r"\*\*Tokens:\*\*\s*([\d.]+)k?\s*/\s*([\d.]+)k", content)
    if tokens_match:
        used_str, total_str = tokens_match.groups()
        data["tokens_used"] = int(float(used_str) * 1000)
        data["tokens_total"] = int(float(total_str) * 1000)

    # Parse category rows from markdown table
    # | System prompt | 2.9k | 1.5% |
    for match in re.finditer(
        r"\|\s*([^|]+?)\s*\|\s*([\d.]+)(k?)\s*\|\s*([\d.]+)%\s*\|", content
    ):
        name, tokens_raw, suffix, pct_str = match.groups()
        name = name.strip()
        if name in ("Category", "-------"):
            continue
        tokens = int(float(tokens_raw) * (1000 if suffix == "k" else 1))
        data["categories"].append(
            {
                "name": name,
                "tokens": tokens,
                "percentage": float(pct_str),
            }
        )

    return data


class ContextReport(Widget):
    """Compact visual display of context usage as a 2D grid."""

    DEFAULT_CSS = """
    ContextReport {
        height: auto;
        margin: 1 0;
        padding: 0 1;
    }

    ContextReport .header {
        height: 1;
        margin-bottom: 0;
    }

    ContextReport .grid-wrapper {
        height: auto;
        width: 100%;
        layout: horizontal;
    }

    ContextReport .grid-container {
        border: round $panel;
        padding: 0;
        height: auto;
        width: 22;
    }

    ContextReport .grid-row {
        height: 1;
        margin: 0;
        width: 20;
    }

    ContextReport .legend-container {
        height: auto;
        width: 1fr;
        padding-left: 1;
        padding-top: 1;
    }

    ContextReport .legend-row {
        height: 1;
        margin: 0;
    }
    """

    # Grid dimensions
    GRID_WIDTH = 20
    GRID_HEIGHT = 10
    GRID_TOTAL = GRID_WIDTH * GRID_HEIGHT  # 200 cells

    # Single source of truth: category -> (color_key, legend_label)
    # Colors: prompt/memory share one color, tools another, messages another, free/buffer share one
    # Order in this list determines grid order AND legend order within a row
    CATEGORY_CONFIG = [
        ("System prompt", "neutral", "prompt"),
        ("Memory files", "neutral_alt", "memory"),
        ("System tools", "tools", "tools"),
        ("MCP tools", "tools", "tools"),
        ("Skills", "tools", "tools"),
        ("Messages", "messages", "messages"),
        ("Free space", "free", "free"),
        ("Autocompact buffer", "free_alt", "buffer"),
    ]

    # Derived lookups from config
    CATEGORY_ORDER = [c[0] for c in CATEGORY_CONFIG]
    CATEGORY_TO_COLOR_KEY = {c[0]: c[1] for c in CATEGORY_CONFIG}
    CATEGORY_TO_LABEL = {c[0]: c[2] for c in CATEGORY_CONFIG}

    def __init__(self, content: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.data = parse_context_markdown(content)

    def _get_color_map(self) -> dict[str, str]:
        """Get color map from color_key -> hex color."""
        try:
            theme = self.app.current_theme
            # Theme colors are strings directly, not objects with .hex
            primary = theme.primary if isinstance(theme.primary, str) else "#cc7700"
            secondary = (
                theme.secondary if isinstance(theme.secondary, str) else "#5599dd"
            )
            panel = theme.panel if isinstance(theme.panel, str) else "#333333"
        except Exception:
            primary = "#cc7700"
            secondary = "#5599dd"
            panel = "#333333"

        return {
            "neutral": panel,  # prompt - dark grey
            "neutral_alt": self._lighten(panel, 0.1),  # memory - slightly lighter
            "tools": secondary,  # tools - blue
            "messages": primary,  # messages - orange
            "free": self._lighten(panel, 0.2),  # free - slightly lighter grey
            "free_alt": self._lighten(panel, 0.15),  # buffer - between panel and free
        }

    def _lighten(self, color: str, amount: float) -> str:
        """Lighten a hex color by a fraction toward white."""
        try:
            r, g, b = int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)
            r = min(255, int(r + (255 - r) * amount))
            g = min(255, int(g + (255 - g) * amount))
            b = min(255, int(b + (255 - b) * amount))
            return f"#{r:02x}{g:02x}{b:02x}"
        except Exception:
            return color

    def _get_color(
        self, category_name: str, color_map: dict[str, str] | None = None
    ) -> str:
        """Get color for a category."""
        colors = color_map or self._get_color_map()
        color_key = self.CATEGORY_TO_COLOR_KEY.get(category_name, "neutral")
        return colors.get(color_key, "#666666")

    def compose(self) -> ComposeResult:
        from textual.containers import Vertical, Horizontal

        data = self.data

        # Header: model · usage (strip claude- prefix and any trailing -YYYYMMDD date)
        model_short = re.sub(r"-\d{8}$", "", data["model"].replace("claude-", ""))
        used_k = data["tokens_used"] / 1000
        total_k = data["tokens_total"] / 1000
        pct = (
            (data["tokens_used"] / data["tokens_total"] * 100)
            if data["tokens_total"]
            else 0
        )

        header_text = f"[dim]{model_short}[/dim] · [bold]{used_k:.0f}k/{total_k:.0f}k ({pct:.0f}%)[/bold]"
        yield Static(header_text, classes="header", markup=True)

        # Build the grid with legend on right
        grid_rows, legend_items = self._build_grid()

        with Horizontal(classes="grid-wrapper"):
            # Grid in bordered container
            with Vertical(classes="grid-container"):
                for row in grid_rows:
                    yield Static(row, classes="grid-row", markup=True)

            # Legend outside the border
            with Vertical(classes="legend-container"):
                for i in range(self.GRID_HEIGHT):
                    legend = legend_items.get(i, "")
                    yield Static(f" {legend}", classes="legend-row", markup=True)

    def _build_grid(self) -> tuple[list[str], dict[int, str]]:
        """Build the grid cells and determine legend placement.

        Returns:
            - List of row strings (markup)
            - Dict mapping row index to legend text for that row
        """
        total = self.data["tokens_total"]
        if not total:
            return [f"[#333333]{'░ ' * self.GRID_WIDTH}[/]"] * self.GRID_HEIGHT, {}

        # Reorder categories to preferred order (memory after prompt)
        cat_by_name = {cat["name"]: cat for cat in self.data["categories"]}
        ordered_cats = []
        for name in self.CATEGORY_ORDER:
            if name in cat_by_name:
                ordered_cats.append(cat_by_name[name])
        # Add any categories not in our order list
        for cat in self.data["categories"]:
            if cat not in ordered_cats:
                ordered_cats.append(cat)

        # Calculate cell counts for each category
        cells = []
        for cat in ordered_cats:
            count = round((cat["tokens"] / total) * self.GRID_TOTAL)
            if count > 0 or cat["tokens"] > 0:
                count = max(1, count)  # At least 1 cell if non-zero
            cells.append((cat["name"], count))

        # Adjust to exactly GRID_TOTAL cells
        current = sum(c[1] for c in cells)
        while current > self.GRID_TOTAL:
            # Reduce largest
            max_idx = max(range(len(cells)), key=lambda i: cells[i][1])
            name, cnt = cells[max_idx]
            cells[max_idx] = (name, cnt - 1)
            current -= 1
        while current < self.GRID_TOTAL:
            # Increase "Free space" or last category
            for i, (name, cnt) in enumerate(cells):
                if name == "Free space":
                    cells[i] = (name, cnt + 1)
                    current += 1
                    break
            else:
                cells[-1] = (cells[-1][0], cells[-1][1] + 1)
                current += 1

        # Flatten to a list of cell values
        flat_cells = []
        for name, count in cells:
            flat_cells.extend([name] * count)

        # Cache color map for all lookups
        color_map = self._get_color_map()

        # Build rows and track categories per row
        rows = []
        row_categories: dict[int, list[str]] = {i: [] for i in range(self.GRID_HEIGHT)}

        for row_idx in range(self.GRID_HEIGHT):
            row_chars = []
            start = row_idx * self.GRID_WIDTH
            for cell_idx in range(start, start + self.GRID_WIDTH):
                if cell_idx < len(flat_cells):
                    cat_name = flat_cells[cell_idx]
                    if cat_name not in row_categories[row_idx]:
                        row_categories[row_idx].append(cat_name)
                    color = self._get_color(cat_name, color_map)
                    row_chars.append(f"[{color}]█[/]")
                else:
                    row_chars.append("[#222222]·[/]")
            rows.append("".join(row_chars))

        # Build legend - in order of appearance on each row
        legend_items = {}
        for row_idx in range(self.GRID_HEIGHT):
            legend_parts = []
            seen_labels = set()
            for cat_name in row_categories[row_idx]:
                label = self.CATEGORY_TO_LABEL.get(cat_name, cat_name)
                if label not in seen_labels:
                    seen_labels.add(label)
                    color = self._get_color(cat_name, color_map)
                    legend_parts.append(f"[{color}]█[/] {label}")
            if legend_parts:
                legend_items[row_idx] = "  ".join(legend_parts)

        return rows, legend_items
