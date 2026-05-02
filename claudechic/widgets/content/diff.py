"""Syntax-highlighted diff widget."""

import difflib
import re
from functools import lru_cache

from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound
from textual.content import Content, Span
from textual.containers import HorizontalScroll
from textual.highlight import HighlightTheme
from textual.widgets import Static

from claudechic.formatting import get_lang_from_path


# Theme-aware diff styles - dark and light variants
DARK_THEME_STYLES = {
    "removed_bg": "on #2a1a1a",  # Very subtle dark red background
    "added_bg": "on #1a2a1a",  # Very subtle dark green background
    "removed_word": "on #3d2525",  # Slightly brighter for changed words
    "added_word": "on #253d25",  # Slightly brighter for changed words
    "removed_indicator": "#664444",  # Dimmed red for - indicator
    "added_indicator": "#446644",  # Dimmed green for + indicator
}
LIGHT_THEME_STYLES = {
    "removed_bg": "on #fff5f5",  # Very subtle light red background
    "added_bg": "on #f5fff5",  # Very subtle light green background
    "removed_word": "on #ffecec",  # Slightly brighter for changed words
    "added_word": "on #ecffec",  # Slightly brighter for changed words
    "removed_indicator": "#aa6666",  # Visible red for - indicator
    "added_indicator": "#66aa66",  # Visible green for + indicator
}


@lru_cache(maxsize=64)
def _get_cached_lexer(language: str):
    """Cache Pygments lexers to avoid repeated loading (~15% CPU savings)."""
    try:
        return get_lexer_by_name(language, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None


def _highlight_text(text: str, language: str) -> Content:
    """Syntax highlight text using cached lexer and default HighlightTheme.

    Accumulates span positions from token-text lengths rather than trusting
    the lexer's reported source indices. Some lexers (notably ``markdown``,
    which delegates fenced code blocks to a sub-lexer) emit indices that
    reset to 0 inside embedded blocks, so using those indices directly would
    splat inner-block spans onto the start of the document.

    Safe because the cached lexer is built with ``stripnl=False,
    ensurenl=False`` (see ``_get_cached_lexer``) and default ``tabsize=0``,
    and we expand tabs at the diff input — so tokens preserve source text
    exactly (verified across markdown/python/go/js/rust).
    """
    if not language:
        return Content(text)

    lexer = _get_cached_lexer(language)
    if lexer is None:
        return Content(text)

    text = "\n".join(text.splitlines())
    spans: list[Span] = []

    pos = 0
    for token_type, token in lexer.get_tokens(text):
        current_type = token_type
        while True:
            if style := HighlightTheme.STYLES.get(current_type):
                spans.append(Span(pos, pos + len(token), style))
                break
            if (current_type := current_type.parent) is None:
                break
        pos += len(token)

    return Content(text, spans=spans).stylize_before("$text")


def _highlight_lines(text: str, language: str) -> list[Content]:
    """Syntax highlight text and split into lines."""
    if not text:
        return []
    return _highlight_text(text, language).split("\n")


def _snap_to_tokens(
    spans: list[tuple[int, int]], content: Content
) -> list[tuple[int, int]]:
    """Expand spans to align with syntax token boundaries.

    This prevents word-diff highlighting from cutting through the middle of
    syntax-highlighted tokens, which causes ugly mid-word color changes.
    """
    if not spans:
        return []

    # Collect token boundaries from syntax highlighting spans
    # Note: _spans is internal to Textual's Content, but no public API exists
    boundaries = {0, len(content)}
    for span in content._spans:
        boundaries.add(span.start)
        boundaries.add(span.end)
    boundaries = sorted(boundaries)

    result = []
    for start, end in spans:
        # Find the boundary <= start (snap left edge left)
        snapped_start = 0
        for b in boundaries:
            if b <= start:
                snapped_start = b
            else:
                break

        # Find the boundary >= end (snap right edge right)
        snapped_end = len(content)
        for b in reversed(boundaries):
            if b >= end:
                snapped_end = b
            else:
                break

        if snapped_start < snapped_end:
            result.append((snapped_start, snapped_end))

    return result


def _word_diff_spans(
    old_line: str, new_line: str
) -> tuple[list[tuple[int, int]], list[tuple[int, int]]]:
    """Compute character spans that changed between two lines.

    Returns (old_spans, new_spans) where each span is (start, end) of changed text.
    """

    def tokenize(s: str) -> list[tuple[str, int, int]]:
        """Return list of (token, start, end). Splits on whitespace and punctuation."""
        result = []
        # Match: words, individual punctuation, or whitespace runs
        for m in re.finditer(r"\w+|[^\w\s]|\s+", s):
            result.append((m.group(), m.start(), m.end()))
        return result

    old_tokens = tokenize(old_line)
    new_tokens = tokenize(new_line)
    old_strs = [t[0] for t in old_tokens]
    new_strs = [t[0] for t in new_tokens]

    sm = difflib.SequenceMatcher(None, old_strs, new_strs)
    old_spans = []
    new_spans = []

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("delete", "replace") and old_tokens:
            start = old_tokens[i1][1] if i1 < len(old_tokens) else len(old_line)
            end = (
                old_tokens[i2 - 1][2]
                if i2 > 0 and i2 - 1 < len(old_tokens)
                else len(old_line)
            )
            if start < end:
                old_spans.append((start, end))
        if tag in ("insert", "replace") and new_tokens:
            start = new_tokens[j1][1] if j1 < len(new_tokens) else len(new_line)
            end = (
                new_tokens[j2 - 1][2]
                if j2 > 0 and j2 - 1 < len(new_tokens)
                else len(new_line)
            )
            if start < end:
                new_spans.append((start, end))

    return old_spans, new_spans


def _build_line_content(
    line_content: Content,
    bg_style: str,
    highlight_spans: list[tuple[int, int]] | None = None,
    highlight_style: str = "",
) -> Content:
    """Apply background style to line, with optional subtle highlights for changed spans.

    Preserves syntax highlighting from the input content. Snaps highlight spans
    to syntax token boundaries to avoid mid-word color changes.
    """
    # Apply base background to entire line (preserves syntax highlighting)
    result = line_content.stylize(bg_style, 0, len(line_content))

    # Snap spans to token boundaries, then apply highlight
    if highlight_spans and highlight_style:
        snapped = _snap_to_tokens(highlight_spans, line_content)
        for start, end in snapped:
            # Clamp to line length
            start = min(start, len(result))
            end = min(end, len(result))
            if start < end:
                result = result.stylize(highlight_style, start, end)

    return result


class DiffContent(Static):
    """Inner static widget that renders the diff content without wrapping."""

    DEFAULT_CSS = """
    DiffContent {
        color: $text;
        width: auto;
    }
    """


class DiffWidget(HorizontalScroll):
    """Displays a syntax-highlighted diff between two code strings."""

    DEFAULT_CSS = """
    DiffWidget {
        height: auto;
        max-height: 100%;
    }
    """

    # Minimum width to show side-by-side (each side needs ~60 chars)
    SIDE_BY_SIDE_MIN_WIDTH = 130

    # Expand tabs at input so ``len(text)`` matches rendered cell width.
    TAB_SIZE = 8

    def __init__(
        self,
        old: str,
        new: str,
        path: str = "",
        context_lines: int = 3,
        replace_all: bool = False,
        old_start: int = 1,
        new_start: int = 1,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        self._old = old.expandtabs(self.TAB_SIZE)
        self._new = new.expandtabs(self.TAB_SIZE)
        self._path = path
        self._context_lines = context_lines
        self._replace_all = replace_all
        self._old_start = old_start
        self._new_start = new_start

    def compose(self):
        content = self._render_diff()
        yield DiffContent(content)

    def on_resize(self) -> None:
        """Re-render diff when width changes (switch between unified/side-by-side)."""
        self._refresh_content()

    def _on_app_theme_changed(self) -> None:
        """Re-render diff when theme changes."""
        self._refresh_content()

    def _refresh_content(self) -> None:
        """Update the diff content widget."""
        try:
            content_widget = self.query_one(DiffContent)
            content_widget.update(self._render_diff())
        except Exception:
            pass

    def _use_side_by_side(self) -> bool:
        """Determine if we should use side-by-side view based on widget's actual width."""
        try:
            return self.size.width >= self.SIDE_BY_SIDE_MIN_WIDTH
        except Exception:
            return False

    def _get_styles(self) -> dict[str, str]:
        """Get theme-aware diff styles. Returns theme-aware colors."""
        try:
            is_dark = self.app.current_theme.dark
        except Exception:
            is_dark = True  # Default to dark theme
        return DARK_THEME_STYLES if is_dark else LIGHT_THEME_STYLES

    def _render_diff(self) -> Content:
        """Build the complete diff display."""
        # For replace_all edits, show simple pattern replacement
        if self._replace_all:
            return Content.assemble(
                Content.styled("- ", "red"),
                Content.styled(self._old, "red underline"),
                Content.styled("\n", ""),
                Content.styled("+ ", "green"),
                Content.styled(self._new, "green underline"),
                Content.styled("\n(all occurrences)", "dim"),
            )

        if self._use_side_by_side():
            return self._render_side_by_side()
        return self._render_unified()

    def _prepare_diff(self):
        """Common setup for both unified and side-by-side rendering.

        Returns (old_lines, new_lines, old_highlighted, new_highlighted, grouped, gutter_width)
        or None if no changes.
        """
        old_lines = self._old.splitlines() if self._old else []
        new_lines = self._new.splitlines() if self._new else []

        if old_lines == new_lines:
            return None

        lang = get_lang_from_path(self._path)
        old_highlighted = _highlight_lines(self._old, lang)
        new_highlighted = _highlight_lines(self._new, lang)

        sm = difflib.SequenceMatcher(None, old_lines, new_lines)
        grouped = list(sm.get_grouped_opcodes(self._context_lines))

        max_old = self._old_start + len(old_lines) - 1 if old_lines else self._old_start
        max_new = self._new_start + len(new_lines) - 1 if new_lines else self._new_start
        gutter_width = max(len(str(max_old)), len(str(max_new)))

        return (
            old_lines,
            new_lines,
            old_highlighted,
            new_highlighted,
            grouped,
            gutter_width,
        )

    def _render_unified(self) -> Content:
        """Render unified diff (stacked - / + lines)."""
        prep = self._prepare_diff()
        if prep is None:
            return Content.styled("No changes", "dim")
        (
            old_lines,
            new_lines,
            old_highlighted,
            new_highlighted,
            grouped,
            gutter_width,
        ) = prep

        # Get theme-aware styles
        styles = self._get_styles()
        removed_bg = styles["removed_bg"]
        added_bg = styles["added_bg"]
        removed_word = styles["removed_word"]
        added_word = styles["added_word"]
        removed_indicator = styles["removed_indicator"]
        added_indicator = styles["added_indicator"]

        def make_gutter(line_num: int | None) -> Content:
            """Single line number column - shows relevant line number."""
            num_str = (
                str(line_num).rjust(gutter_width) if line_num else " " * gutter_width
            )
            return Content.styled(f"{num_str} ", "#555555")

        # Get width to fill background (use widget width, account for potential scrollbar)
        try:
            min_width = self.size.width - 2
        except Exception:
            min_width = 80

        def extend_to_width(content: Content, width: int) -> Content:
            """Extend content to fill width, preserving the last character's style."""
            current_len = len(content)
            if current_len >= width:
                return content
            return content.extend_right(width - current_len)

        parts: list[Content] = []

        for group_idx, group in enumerate(grouped):
            if group_idx > 0:
                # Simple blank line between context groups within a hunk
                parts.append(Content("\n"))

            for tag, i1, i2, j1, j2 in group:
                if tag == "equal":
                    for di, i in enumerate(range(i1, i2)):
                        j = j1 + di
                        gutter = make_gutter(self._new_start + j)
                        code = (
                            old_highlighted[i]
                            if i < len(old_highlighted)
                            else Content("")
                        )
                        line = Content.assemble(
                            gutter,
                            Content.styled("  ", "dim"),
                            code.stylize("dim", 0, len(code)),
                            Content("\n"),
                        )
                        parts.append(line)

                elif tag == "delete":
                    for i in range(i1, i2):
                        gutter = make_gutter(self._old_start + i)
                        code = (
                            old_highlighted[i]
                            if i < len(old_highlighted)
                            else Content("")
                        )
                        styled_code = _build_line_content(code, removed_bg)
                        indicator = Content.styled("- ", removed_indicator)
                        line_content = Content.assemble(gutter, indicator, styled_code)
                        line_content = line_content.stylize(
                            removed_bg, 0, len(line_content)
                        )
                        line_content = extend_to_width(line_content, min_width)
                        parts.append(Content.assemble(line_content, Content("\n")))

                elif tag == "insert":
                    for j in range(j1, j2):
                        gutter = make_gutter(self._new_start + j)
                        code = (
                            new_highlighted[j]
                            if j < len(new_highlighted)
                            else Content("")
                        )
                        styled_code = _build_line_content(code, added_bg)
                        indicator = Content.styled("+ ", added_indicator)
                        line_content = Content.assemble(gutter, indicator, styled_code)
                        line_content = line_content.stylize(
                            added_bg, 0, len(line_content)
                        )
                        line_content = extend_to_width(line_content, min_width)
                        parts.append(Content.assemble(line_content, Content("\n")))

                elif tag == "replace":
                    for idx, i in enumerate(range(i1, i2)):
                        old_line_text = old_lines[i] if i < len(old_lines) else ""
                        j = j1 + idx
                        if j < j2:
                            new_line_text = new_lines[j] if j < len(new_lines) else ""
                            old_spans, _ = _word_diff_spans(
                                old_line_text, new_line_text
                            )
                        else:
                            old_spans = []

                        gutter = make_gutter(self._old_start + i)
                        code = (
                            old_highlighted[i]
                            if i < len(old_highlighted)
                            else Content("")
                        )
                        styled_code = _build_line_content(
                            code, removed_bg, old_spans, removed_word
                        )
                        indicator = Content.styled("- ", removed_indicator)
                        line_content = Content.assemble(gutter, indicator, styled_code)
                        line_content = line_content.stylize(
                            removed_bg, 0, len(line_content)
                        )
                        line_content = extend_to_width(line_content, min_width)
                        parts.append(Content.assemble(line_content, Content("\n")))

                    for idx, j in enumerate(range(j1, j2)):
                        new_line_text = new_lines[j] if j < len(new_lines) else ""
                        i = i1 + idx
                        if i < i2:
                            old_line_text = old_lines[i] if i < len(old_lines) else ""
                            _, new_spans = _word_diff_spans(
                                old_line_text, new_line_text
                            )
                        else:
                            new_spans = []

                        gutter = make_gutter(self._new_start + j)
                        code = (
                            new_highlighted[j]
                            if j < len(new_highlighted)
                            else Content("")
                        )
                        styled_code = _build_line_content(
                            code, added_bg, new_spans, added_word
                        )
                        indicator = Content.styled("+ ", added_indicator)
                        line_content = Content.assemble(gutter, indicator, styled_code)
                        line_content = line_content.stylize(
                            added_bg, 0, len(line_content)
                        )
                        line_content = extend_to_width(line_content, min_width)
                        parts.append(Content.assemble(line_content, Content("\n")))

        if not parts:
            return Content.styled("No changes", "dim")

        return Content.assemble(*parts).rstrip("\n")

    def _render_side_by_side(self) -> Content:
        """Render side-by-side diff with old on left, new on right."""
        prep = self._prepare_diff()
        if prep is None:
            return Content.styled("No changes", "dim")
        (
            old_lines,
            new_lines,
            old_highlighted,
            new_highlighted,
            grouped,
            gutter_width,
        ) = prep
        gutter_width = max(gutter_width, 3)  # Minimum width for side-by-side

        # Get theme-aware styles
        styles = self._get_styles()
        removed_bg = styles["removed_bg"]
        added_bg = styles["added_bg"]
        removed_word = styles["removed_word"]
        added_word = styles["added_word"]

        # Calculate column width - use widget's actual width, account for potential scrollbar
        try:
            total_width = self.size.width - 2
        except Exception:
            total_width = 80
        # Layout: [gutter code] │ [gutter code]
        col_width = max((total_width - 3) // 2, 40)  # -3 for separator
        code_width = col_width - gutter_width - 1

        def fit_to_width(content: Content, width: int) -> Content:
            """Fit content to width: truncate if too long, extend if too short."""
            text_len = len(content)
            if text_len >= width:
                return content[:width]
            return content.extend_right(width - text_len)

        def make_col(
            line_num: int | None,
            code: Content,
            bg: str = "",
            word_spans: list[tuple[int, int]] | None = None,
            word_style: str = "",
        ) -> Content:
            """Build a column: gutter + code, fitted to width."""
            gutter = (
                Content.styled(str(line_num).rjust(gutter_width) + " ", "#666666")
                if line_num
                else Content.styled(" " * (gutter_width + 1), "#666666")
            )
            if bg:
                code = _build_line_content(code, bg, word_spans, word_style)
            col = Content.assemble(gutter, fit_to_width(code, code_width))
            if bg:
                col = col.stylize(bg, 0, len(col))
            return col

        separator = Content.styled(" │ ", "dim")
        parts: list[Content] = []

        for group_idx, group in enumerate(grouped):
            if group_idx > 0:
                # Simple blank line between context groups within a hunk
                parts.append(Content("\n"))

            for tag, i1, i2, j1, j2 in group:
                if tag == "equal":
                    # Both sides show the same content
                    for di, i in enumerate(range(i1, i2)):
                        j = j1 + di
                        old_code = (
                            old_highlighted[i]
                            if i < len(old_highlighted)
                            else Content("")
                        )
                        new_code = (
                            new_highlighted[j]
                            if j < len(new_highlighted)
                            else Content("")
                        )
                        left = make_col(self._old_start + i, old_code)
                        right = make_col(self._new_start + j, new_code)
                        line = Content.assemble(
                            left.stylize("dim", 0, len(left)),
                            separator,
                            right.stylize("dim", 0, len(right)),
                            Content("\n"),
                        )
                        parts.append(line)

                elif tag == "delete":
                    # Left side has content, right side empty
                    for i in range(i1, i2):
                        old_code = (
                            old_highlighted[i]
                            if i < len(old_highlighted)
                            else Content("")
                        )
                        left = make_col(self._old_start + i, old_code, removed_bg)
                        right = make_col(None, Content(""))
                        line = Content.assemble(left, separator, right, Content("\n"))
                        parts.append(line)

                elif tag == "insert":
                    # Left side empty, right side has content
                    for j in range(j1, j2):
                        new_code = (
                            new_highlighted[j]
                            if j < len(new_highlighted)
                            else Content("")
                        )
                        left = make_col(None, Content(""))
                        left = fit_to_width(left, col_width)
                        right = make_col(self._new_start + j, new_code, added_bg)
                        line = Content.assemble(left, separator, right, Content("\n"))
                        parts.append(line)

                elif tag == "replace":
                    # Pair up old/new lines side by side
                    old_count = i2 - i1
                    new_count = j2 - j1
                    max_count = max(old_count, new_count)

                    for idx in range(max_count):
                        # Left side (old)
                        if idx < old_count:
                            i = i1 + idx
                            old_code = (
                                old_highlighted[i]
                                if i < len(old_highlighted)
                                else Content("")
                            )
                            old_text = old_lines[i] if i < len(old_lines) else ""
                            if idx < new_count:
                                j = j1 + idx
                                new_text = new_lines[j] if j < len(new_lines) else ""
                                old_spans, _ = _word_diff_spans(old_text, new_text)
                            else:
                                old_spans = []
                            left = make_col(
                                self._old_start + i,
                                old_code,
                                removed_bg,
                                old_spans,
                                removed_word,
                            )
                        else:
                            left = make_col(None, Content(""))

                        left = fit_to_width(left, col_width)

                        # Right side (new)
                        if idx < new_count:
                            j = j1 + idx
                            new_code = (
                                new_highlighted[j]
                                if j < len(new_highlighted)
                                else Content("")
                            )
                            new_text = new_lines[j] if j < len(new_lines) else ""
                            if idx < old_count:
                                i = i1 + idx
                                old_text = old_lines[i] if i < len(old_lines) else ""
                                _, new_spans = _word_diff_spans(old_text, new_text)
                            else:
                                new_spans = []
                            right = make_col(
                                self._new_start + j,
                                new_code,
                                added_bg,
                                new_spans,
                                added_word,
                            )
                        else:
                            right = make_col(None, Content(""))

                        line = Content.assemble(left, separator, right, Content("\n"))
                        parts.append(line)

        if not parts:
            return Content.styled("No changes", "dim")

        return Content.assemble(*parts).rstrip("\n")
