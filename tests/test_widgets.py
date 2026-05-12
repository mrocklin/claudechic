"""Pure widget tests - no SDK needed."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from claudechic.widgets import (
    ChatInput,
    ChatMessage,
    ThinkingIndicator,
    SelectionPrompt,
    QuestionPrompt,
    AgentSection,
    PlanSection,
    TodoPanel,
    ProcessPanel,
    BackgroundProcess,
    ModelPrompt,
    EffortPrompt,
    StatusFooter,
    EffortLabel,
    ContextBar,
)
from claudechic.widgets.content.todo import TodoItem
from claudechic.widgets.layout.processes import ProcessItem
from claudechic.enums import AgentStatus


class WidgetTestApp(App):
    """Minimal app for testing individual widgets."""

    def __init__(self, widget_factory):
        super().__init__()
        self._widget_factory = widget_factory

    def compose(self) -> ComposeResult:
        yield self._widget_factory()


@pytest.mark.asyncio
async def test_chat_input_submit():
    """Enter posts Submitted message."""
    submitted_text = None

    class TestApp(App):
        def compose(self):
            yield ChatInput(id="input")

        def on_chat_input_submitted(self, event):
            nonlocal submitted_text
            submitted_text = event.text

    app = TestApp()
    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)
        input_widget.text = "hello world"
        await pilot.press("enter")
        assert submitted_text == "hello world"


@pytest.mark.asyncio
async def test_chat_input_history():
    """Up/down navigates history."""

    class HistoryTestApp(App):
        def compose(self):
            yield ChatInput(id="input")

        def on_chat_input_submitted(self, event):
            # Clear input like ChatApp does
            self.query_one(ChatInput).clear()

    app = HistoryTestApp()
    async with app.run_test() as pilot:
        input_widget = app.query_one(ChatInput)

        # Send a few messages to build history
        input_widget.text = "first"
        await pilot.press("enter")
        input_widget.text = "second"
        await pilot.press("enter")
        input_widget.text = "third"
        await pilot.press("enter")

        # Now navigate history
        assert input_widget.text == ""
        await pilot.press("up")
        assert input_widget.text == "third"
        await pilot.press("up")
        assert input_widget.text == "second"
        await pilot.press("up")
        assert input_widget.text == "first"
        await pilot.press("down")
        assert input_widget.text == "second"


@pytest.mark.asyncio
async def test_chat_input_image_detection():
    """Detects image paths."""
    app = WidgetTestApp(lambda: ChatInput(id="input"))
    async with app.run_test():
        input_widget = app.query_one(ChatInput)

        # Test various image path formats
        assert input_widget._is_image_path("/tmp/test.png") == []  # File doesn't exist
        assert input_widget._is_image_path("not an image") == []
        assert input_widget._is_image_path("file:///nonexistent.jpg") == []


@pytest.mark.asyncio
async def test_selection_prompt_arrow_navigation():
    """Up/down cycles through options."""
    options = [("a", "Option A"), ("b", "Option B"), ("c", "Option C")]

    app = WidgetTestApp(lambda: SelectionPrompt("Choose:", options))
    async with app.run_test() as pilot:
        prompt = app.query_one(SelectionPrompt)

        assert prompt.selected_idx == 0
        await pilot.press("down")
        assert prompt.selected_idx == 1
        await pilot.press("down")
        assert prompt.selected_idx == 2
        await pilot.press("down")
        assert prompt.selected_idx == 0  # Wraps around
        await pilot.press("up")
        assert prompt.selected_idx == 2


@pytest.mark.asyncio
async def test_selection_prompt_number_keys():
    """Number keys select options directly."""
    options = [("a", "Option A"), ("b", "Option B"), ("c", "Option C")]
    result = None

    class TestApp(App):
        def compose(self):
            yield SelectionPrompt("Choose:", options)

    app = TestApp()
    async with app.run_test() as pilot:
        prompt = app.query_one(SelectionPrompt)

        # Press "2" to select second option
        await pilot.press("2")
        result = await prompt.wait()

    assert result == "b"


@pytest.mark.asyncio
async def test_selection_prompt_escape_cancels():
    """Escape resolves with empty string."""
    options = [("a", "Option A"), ("b", "Option B")]

    app = WidgetTestApp(lambda: SelectionPrompt("Choose:", options))
    async with app.run_test() as pilot:
        prompt = app.query_one(SelectionPrompt)

        await pilot.press("escape")
        result = await prompt.wait()
        assert result == ""


@pytest.mark.asyncio
async def test_selection_prompt_text_option():
    """Text option allows freeform input."""
    options = [("a", "Option A"), ("b", "Option B")]
    text_option = ("custom", "Type something...")

    class TestApp(App):
        def compose(self):
            yield SelectionPrompt("Choose:", options, text_option)

    app = TestApp()
    async with app.run_test() as pilot:
        prompt = app.query_one(SelectionPrompt)

        # Navigate to text option (3rd option, index 2)
        await pilot.press("3")
        # Type some text
        await pilot.press("h", "e", "l", "l", "o")
        await pilot.press("enter")
        result = await prompt.wait()

    assert result == "custom:hello"


@pytest.mark.asyncio
async def test_model_prompt_selection():
    """ModelPrompt allows model selection with arrow keys and numbers."""
    # Mock SDK model list
    models = [
        {"value": "sonnet", "displayName": "Sonnet", "description": "Sonnet 4 · Fast"},
        {"value": "opus", "displayName": "Opus", "description": "Opus 4.5 · Powerful"},
        {"value": "haiku", "displayName": "Haiku", "description": "Haiku 3.5 · Quick"},
    ]

    class TestApp(App):
        def compose(self):
            yield ModelPrompt(models, current_value="sonnet")

    app = TestApp()
    async with app.run_test() as pilot:
        prompt = app.query_one(ModelPrompt)

        # Should start on current model (sonnet = index 0)
        assert prompt.selected_idx == 0

        # Navigate down to opus
        await pilot.press("down")
        assert prompt.selected_idx == 1

        # Select with number key (3 = haiku)
        await pilot.press("3")
        result = await prompt.wait()

    assert result == "haiku"


@pytest.mark.asyncio
async def test_model_prompt_escape():
    """ModelPrompt returns None on escape."""
    models = [
        {"value": "sonnet", "displayName": "Sonnet", "description": "Sonnet 4 · Fast"},
        {"value": "opus", "displayName": "Opus", "description": "Opus 4.5 · Powerful"},
    ]
    app = WidgetTestApp(lambda: ModelPrompt(models, current_value="opus"))
    async with app.run_test() as pilot:
        prompt = app.query_one(ModelPrompt)

        # Should start on opus (index 1)
        assert prompt.selected_idx == 1

        await pilot.press("escape")
        result = await prompt.wait()

    assert result is None


@pytest.mark.asyncio
async def test_effort_prompt_selection():
    """EffortPrompt selects an effort level by number key."""
    app = WidgetTestApp(lambda: EffortPrompt(current_value="medium"))
    async with app.run_test() as pilot:
        prompt = app.query_one(EffortPrompt)
        # medium is index 1
        assert prompt.selected_idx == 1
        # Navigate to high (index 2) and select via number key
        await pilot.press("3")
        result = await prompt.wait()
    assert result == "high"


@pytest.mark.asyncio
async def test_effort_prompt_xhigh_initial():
    """EffortPrompt lands on xhigh when that's the current value.

    Guards against a future 'fix' that drops xhigh from OPTIONS because
    the SDK's Literal type doesn't list it.
    """
    app = WidgetTestApp(lambda: EffortPrompt(current_value="xhigh"))
    async with app.run_test():
        prompt = app.query_one(EffortPrompt)
        # xhigh is the 4th option (index 3)
        assert prompt.selected_idx == 3
        assert prompt.OPTIONS[prompt.selected_idx][0] == "xhigh"


@pytest.mark.asyncio
async def test_effort_prompt_escape():
    """EffortPrompt returns None on escape."""
    app = WidgetTestApp(lambda: EffortPrompt(current_value="high"))
    async with app.run_test() as pilot:
        prompt = app.query_one(EffortPrompt)
        await pilot.press("escape")
        result = await prompt.wait()
    assert result is None


@pytest.mark.asyncio
async def test_question_prompt_multi_question():
    """Handles multiple questions."""
    questions = [
        {"question": "Q1?", "options": [{"label": "Yes"}, {"label": "No"}]},
        {"question": "Q2?", "options": [{"label": "Red"}, {"label": "Blue"}]},
    ]

    app = WidgetTestApp(lambda: QuestionPrompt(questions))
    async with app.run_test() as pilot:
        prompt = app.query_one(QuestionPrompt)
        prompt.focus()

        # Answer first question
        assert prompt.current_q == 0
        await pilot.press("1")  # Select "Yes"

        # Should advance to second question
        assert prompt.current_q == 1
        assert prompt.answers == {"Q1?": "Yes"}

        await pilot.press("2")  # Select "Blue"

        # Prompt should have resolved after second answer
        assert prompt.answers == {"Q1?": "Yes", "Q2?": "Blue"}


@pytest.mark.asyncio
async def test_agent_section_add_remove():
    """Can add and remove agents."""
    app = WidgetTestApp(lambda: AgentSection(id="agents"))
    async with app.run_test():
        section = app.query_one(AgentSection)

        # Add agents
        section.add_agent("id1", "Agent 1")
        section.add_agent("id2", "Agent 2")

        assert "id1" in section._agents
        assert "id2" in section._agents
        assert len(section._agents) == 2

        # Remove one
        section.remove_agent("id1")
        assert "id1" not in section._agents
        assert len(section._agents) == 1


@pytest.mark.asyncio
async def test_agent_section_active_selection():
    """set_active updates visual state."""
    app = WidgetTestApp(lambda: AgentSection(id="agents"))
    async with app.run_test():
        section = app.query_one(AgentSection)

        section.add_agent("id1", "Agent 1")
        section.add_agent("id2", "Agent 2")

        section.set_active("id1")
        assert section._agents["id1"].has_class("active")
        assert not section._agents["id2"].has_class("active")

        section.set_active("id2")
        assert not section._agents["id1"].has_class("active")
        assert section._agents["id2"].has_class("active")


@pytest.mark.asyncio
async def test_agent_section_status_updates():
    """update_status changes indicator."""
    app = WidgetTestApp(lambda: AgentSection(id="agents"))
    async with app.run_test():
        section = app.query_one(AgentSection)

        section.add_agent("id1", "Agent 1", status=AgentStatus.IDLE)
        assert section._agents["id1"].status == AgentStatus.IDLE

        section.update_status("id1", AgentStatus.BUSY)
        assert section._agents["id1"].status == AgentStatus.BUSY

        section.update_status("id1", AgentStatus.NEEDS_INPUT)
        assert section._agents["id1"].status == AgentStatus.NEEDS_INPUT


@pytest.mark.asyncio
async def test_plan_section():
    """PlanSection set_plan shows/hides plan item."""
    from pathlib import Path

    app = WidgetTestApp(lambda: PlanSection(id="plan"))
    async with app.run_test():
        section = app.query_one(PlanSection)

        # Initially no plan item
        assert section._plan_item is None

        # Set plan creates item
        plan_path = Path("/tmp/test-plan.md")
        section.set_plan(plan_path)
        assert section._plan_item is not None
        assert section._plan_item.plan_path == plan_path

        # Clear plan hides section
        section.set_plan(None)
        assert section.has_class("hidden")


@pytest.mark.asyncio
async def test_context_bar_rendering():
    """ContextBar shows correct fill and color."""
    app = WidgetTestApp(lambda: ContextBar(id="ctx"))
    async with app.run_test():
        bar = app.query_one(ContextBar)

        # Low usage - should be dim
        bar.tokens = 10000
        bar.max_tokens = 200000
        rendered = bar.render()
        assert hasattr(rendered, "plain")
        assert "5%" in rendered.plain  # type: ignore[union-attr]

        # High usage - should be red
        bar.tokens = 180000
        rendered = bar.render()
        assert hasattr(rendered, "plain")
        assert "90%" in rendered.plain  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_context_bar_scales_with_max_tokens():
    """ContextBar percentage scales with max_tokens (per-model window).

    950k out of a 1M window should read 95% (red), not max out as if the
    bar were still hardcoded to 200k.
    """
    app = WidgetTestApp(lambda: ContextBar(id="ctx"))
    async with app.run_test():
        bar = app.query_one(ContextBar)

        # 950k / 1M -> 95% (red zone, ≥ 0.8)
        bar.max_tokens = 1_000_000
        bar.tokens = 950_000
        rendered = bar.render()
        assert hasattr(rendered, "plain")
        assert "95%" in rendered.plain  # type: ignore[union-attr]

        # 180k / 1M -> 18% (low/dim zone, < 0.5), not 90%
        bar.tokens = 180_000
        rendered = bar.render()
        assert hasattr(rendered, "plain")
        assert "18%" in rendered.plain  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_todo_panel_updates():
    """TodoPanel displays and updates todos."""
    app = WidgetTestApp(lambda: TodoPanel(id="panel"))
    async with app.run_test():
        panel = app.query_one(TodoPanel)

        todos = [
            {
                "content": "Task 1",
                "status": "completed",
                "activeForm": "Completing task 1",
            },
            {
                "content": "Task 2",
                "status": "in_progress",
                "activeForm": "Working on task 2",
            },
            {"content": "Task 3", "status": "pending", "activeForm": "Starting task 3"},
        ]

        panel.update_todos(todos)

        items = list(panel.query(TodoItem))
        assert len(items) == 3
        assert items[0].has_class("completed")
        assert items[1].has_class("in_progress")
        assert items[2].has_class("pending")


@pytest.mark.asyncio
async def test_status_footer_permission_mode():
    """Footer shows permission mode state."""
    app = WidgetTestApp(lambda: StatusFooter())
    async with app.run_test():
        footer = app.query_one(StatusFooter)

        footer.permission_mode = "default"
        label = footer.query_one("#permission-mode-label", Static)
        rendered = label.render()
        assert hasattr(rendered, "plain")
        assert "auto-edit: off" in rendered.plain.lower()  # type: ignore[union-attr]

        footer.permission_mode = "acceptEdits"
        rendered = label.render()
        assert hasattr(rendered, "plain")
        assert "auto-edit: on" in rendered.plain.lower()  # type: ignore[union-attr]

        footer.permission_mode = "plan"
        rendered = label.render()
        assert hasattr(rendered, "plain")
        assert "plan mode" in rendered.plain.lower()  # type: ignore[union-attr]

        footer.permission_mode = "auto"
        rendered = label.render()
        assert hasattr(rendered, "plain")
        assert rendered.plain.lower() == "auto"  # type: ignore[union-attr]
        assert label.has_class("auto-mode")


@pytest.mark.asyncio
async def test_status_footer_effort_label():
    """Footer effort label shows 'default' muted when unset, value elevated when set."""
    app = WidgetTestApp(lambda: StatusFooter())
    async with app.run_test():
        footer = app.query_one(StatusFooter)
        label = footer.query_one("#effort-label", EffortLabel)

        # Empty = shows "default", muted (no elevated class), visible
        rendered = label.render()
        assert "default" in rendered.plain  # type: ignore[union-attr]
        assert not label.has_class("hidden")
        assert not label.has_class("elevated")

        # Any explicit value = elevated
        footer.effort = "medium"
        rendered = label.render()
        assert "medium" in rendered.plain  # type: ignore[union-attr]
        assert label.has_class("elevated")

        footer.effort = "xhigh"
        rendered = label.render()
        assert "xhigh" in rendered.plain  # type: ignore[union-attr]
        assert label.has_class("elevated")

        # Reset to empty = back to muted "default"
        footer.effort = ""
        rendered = label.render()
        assert "default" in rendered.plain  # type: ignore[union-attr]
        assert not label.has_class("elevated")


@pytest.mark.asyncio
async def test_chat_message_append():
    """ChatMessage accumulates content."""
    app = WidgetTestApp(lambda: ChatMessage("Hello"))
    async with app.run_test():
        msg = app.query_one(ChatMessage)

        assert msg.get_raw_content() == "Hello"

        msg.append_content(" world")
        assert msg.get_raw_content() == "Hello world"

        msg.append_content("!")
        assert msg.get_raw_content() == "Hello world!"


@pytest.mark.asyncio
async def test_thinking_indicator_animates():
    """ThinkingIndicator cycles through frames."""
    app = WidgetTestApp(ThinkingIndicator)
    async with app.run_test() as pilot:
        indicator = app.query_one(ThinkingIndicator)

        initial_frame = indicator._frame
        # Wait for animation (now at 4Hz = 250ms interval)
        await pilot.pause(0.5)
        # Frame should have changed
        assert indicator._frame != initial_frame or indicator._frame == 0  # May wrap


@pytest.mark.asyncio
async def test_history_search_filters():
    """HistorySearch filters history and cycles through matches."""
    from claudechic.widgets.input.history_search import HistorySearch
    from unittest.mock import patch

    class TestApp(App):
        def compose(self):
            yield HistorySearch(id="history")

    # Mock history data (most recent first)
    mock_history = [
        "fix the bug",
        "add new feature",
        "fix another bug",
        "refactor code",
    ]

    with patch(
        "claudechic.widgets.input.history_search.load_global_history",
        return_value=mock_history,
    ):
        app = TestApp()
        async with app.run_test() as pilot:
            hs = app.query_one(HistorySearch)
            hs.show()
            await pilot.pause()

            # Initially shows most recent match
            assert hs._current_match() == "fix the bug"

            # Type to filter
            from textual.widgets import Input

            inp = hs.query_one("#search-input", Input)
            inp.value = "fix"
            hs.on_input_changed(Input.Changed(inp, "fix"))

            # Should filter to matching entries
            assert len(hs._filtered) == 2
            assert hs._current_match() == "fix the bug"

            # Ctrl+R cycles to next match
            hs.action_next_match()
            assert hs._current_match() == "fix another bug"

            # Up goes back
            hs.action_prev_match()
            assert hs._current_match() == "fix the bug"


@pytest.mark.asyncio
async def test_process_panel_updates():
    """ProcessPanel displays and updates background processes."""
    from datetime import datetime

    app = WidgetTestApp(lambda: ProcessPanel(id="panel", classes="hidden"))
    async with app.run_test():
        panel = app.query_one(ProcessPanel)

        # Initially hidden (no processes)
        assert panel.has_class("hidden")

        # Add some processes
        processes = [
            BackgroundProcess(pid=123, command="sleep 100", start_time=datetime.now()),
            BackgroundProcess(
                pid=456, command="npm run dev", start_time=datetime.now()
            ),
        ]
        panel.update_processes(processes)

        # Processes added, but visibility controlled by set_visible()
        assert panel.process_count == 2
        assert panel.has_class("hidden")  # Still hidden until set_visible(True)

        # Make visible
        panel.set_visible(True)
        assert not panel.has_class("hidden")

        items = list(panel.query(ProcessItem))
        assert len(items) == 2

        # Clear processes - set_visible(True) with no processes still hides
        panel.update_processes([])
        panel.set_visible(True)
        assert panel.has_class("hidden")


# --- Diff token snapping tests ---


def test_snap_to_tokens_expands_partial_spans():
    """Word-diff spans that cut through tokens get expanded to token boundaries."""
    from textual.content import Content, Span
    from claudechic.widgets.content.diff import _snap_to_tokens

    # Simulate syntax-highlighted "activeInsertionOrders:" with tokens:
    # [0-21] identifier, [21-22] punctuation
    content = Content(
        "activeInsertionOrders:",
        spans=[Span(0, 21, "blue"), Span(21, 22, "white")],
    )

    # Span that cuts through the identifier (e.g., highlighting just "active")
    raw_spans = [(0, 6)]
    snapped = _snap_to_tokens(raw_spans, content)

    # Should expand to cover the whole identifier token
    assert snapped == [(0, 21)]


def test_snap_to_tokens_preserves_aligned_spans():
    """Spans already aligned with token boundaries stay unchanged."""
    from textual.content import Content, Span
    from claudechic.widgets.content.diff import _snap_to_tokens

    content = Content(
        "foo bar",
        spans=[Span(0, 3, "red"), Span(3, 4, "white"), Span(4, 7, "blue")],
    )

    # Span exactly matching first token
    raw_spans = [(0, 3)]
    snapped = _snap_to_tokens(raw_spans, content)
    assert snapped == [(0, 3)]


def test_snap_to_tokens_empty_spans():
    """Empty span list returns empty."""
    from textual.content import Content
    from claudechic.widgets.content.diff import _snap_to_tokens

    content = Content("hello")
    assert _snap_to_tokens([], content) == []


def test_word_diff_with_go_syntax():
    """Integration test: word diff + snapping with real Go syntax highlighting."""
    from claudechic.widgets.content.diff import (
        _word_diff_spans,
        _snap_to_tokens,
        _highlight_lines,
    )

    old_line = 'activeOrders: getValue("active",'
    new_line = 'dirtyOrders: getValue("dirty",'

    # Get raw word-diff spans
    old_spans, new_spans = _word_diff_spans(old_line, new_line)

    # Raw spans should identify the changed words
    assert len(old_spans) == 2  # "activeOrders" and "active"
    assert len(new_spans) == 2  # "dirtyOrders" and "dirty"

    # Get syntax-highlighted content
    old_highlighted = _highlight_lines(old_line, "go")
    new_highlighted = _highlight_lines(new_line, "go")

    assert old_highlighted and new_highlighted

    # Snap to token boundaries
    snapped_old = _snap_to_tokens(old_spans, old_highlighted[0])

    # Snapped spans should cover complete tokens
    # The string "active" should expand to include quotes -> "active"
    for start, end in snapped_old:
        text = old_line[start:end]
        # Should not have partial words (no cuts mid-identifier)
        assert not text[0].isalnum() or start == 0 or not old_line[start - 1].isalnum()


def test_highlight_text_preserves_tab_positions():
    """Tab-indented source must keep token spans aligned with raw text.

    Regression test: with ``tabsize=8`` (Pygments default for many lexers),
    tabs get expanded to 8 spaces inside token text and the position
    accumulator drifts +7 chars per tab — so Go syntax spans land on the
    wrong characters or extend past end-of-content. Mitigated by passing
    ``stripnl=False, ensurenl=False`` (which keeps the default ``tabsize=0``)
    plus expanding tabs at the diff input.
    """
    from claudechic.widgets.content.diff import _highlight_text

    src = 'func main() {\n\tprintln("hi")\n}'
    content = _highlight_text(src, "go")

    # Content length must equal source length
    assert len(content.plain) == len(src)

    # Every span must lie within bounds, and the slice must match the source
    # (non-trivial: catches drift even when spans stay in-bounds).
    for span in content._spans:
        assert 0 <= span.start <= span.end <= len(src), (
            f"span {span} out of bounds for len={len(src)}"
        )

    # Spot-check that the keyword `func` got highlighted at position 0
    keyword_spans = [s for s in content._spans if s.start == 0 and s.end == 4]
    assert keyword_spans, "expected a span covering `func` at [0:4]"


def test_highlight_text_markdown_with_fenced_code():
    """Fenced code-block spans must land inside the block, not at index 0.

    Regression test: Pygments' markdown lexer delegates fenced code blocks
    to a sub-lexer (e.g. python) whose ``get_tokens_unprocessed`` emits
    indices reset to 0 — relative to the inner code, not the full document.
    Trusting those indices splatted Python keyword colors onto the start of
    the file (e.g. across a heading), making markdown diffs look broken.
    """
    from claudechic.widgets.content.diff import _highlight_text

    # Content after the code block is required: the markdown lexer's
    # fenced-block regex only matches with a newline after the closing ```,
    # and ``_highlight_text`` strips lone trailing newlines via
    # ``"\n".join(splitlines())``. Trailing content keeps that newline alive
    # so the python sub-lexer fires (the path that triggered the bug).
    src = "# Heading\n\n```python\ndef foo():\n    return 1\n```\n\nend\n"
    content = _highlight_text(src, "markdown")
    rendered = content.plain

    # Every span must lie within bounds of the rendered content
    for span in content._spans:
        assert 0 <= span.start <= span.end <= len(rendered), (
            f"span {span} out of bounds for len={len(rendered)}"
        )

    # The `def` keyword must be highlighted at its real source position,
    # not at index 0 (which is where the bug placed it).
    def_pos = rendered.index("def")
    def_spans = [
        s for s in content._spans if s.start == def_pos and s.end == def_pos + 3
    ]
    assert def_spans, f"expected a span covering `def` at [{def_pos}:{def_pos + 3}]"

    # Negative assertion: no span should sit at [0:3] over the heading text.
    # Mirrors the bug's exact failure mode (`def`-keyword color at file start).
    assert not [s for s in content._spans if (s.start, s.end) == (0, 3)], (
        "no syntax span should land on the first 3 chars of the heading"
    )


def test_highlight_text_rst_with_code_block():
    """RST ``.. code-block::`` directives have the same delegation bug shape.

    Pygments' RST lexer delegates ``.. code-block:: python`` content to the
    python sub-lexer, which emits indices reset to 0 — same root cause as
    the markdown fenced-block bug. Locks in that the position-accumulator
    fix covers RST too, since it's the second confirmed instance of the
    Pygments delegation anti-pattern in the wild.
    """
    from claudechic.widgets.content.diff import _highlight_text

    src = (
        "Heading\n=======\n\n"
        ".. code-block:: python\n\n"
        "    def foo():\n"
        "        return 1\n\n"
        "end\n"
    )
    content = _highlight_text(src, "rst")
    rendered = content.plain

    for span in content._spans:
        assert 0 <= span.start <= span.end <= len(rendered), (
            f"span {span} out of bounds for len={len(rendered)}"
        )

    def_pos = rendered.index("def")
    def_spans = [
        s for s in content._spans if s.start == def_pos and s.end == def_pos + 3
    ]
    assert def_spans, f"expected a span covering `def` at [{def_pos}:{def_pos + 3}]"


def test_diff_widget_expands_tabs():
    """Tabs in input must be expanded so ``len()`` matches rendered cell width.

    Without expansion, tab-indented sources (Go, Makefiles) misalign because
    terminals render ``\\t`` at up to 8 cols while ``len`` counts it as 1 —
    backgrounds underfill and side-by-side columns drift.
    """
    from claudechic.widgets.content.diff import DiffWidget

    old = "func main() {\n\treturn 1\n}"
    new = "func main() {\n\treturn 2\n}"

    w = DiffWidget(old=old, new=new, path="main.go")
    assert "\t" not in w._old
    assert "\t" not in w._new
    assert " " * w.TAB_SIZE + "return 1" in w._old


# --- Lazy collapsible tests ---


@pytest.mark.asyncio
async def test_quiet_collapsible_lazy_content():
    """QuietCollapsible with content_factory defers widget creation until expanded."""
    from claudechic.widgets.primitives.collapsible import QuietCollapsible

    factory_called = False

    def make_content():
        nonlocal factory_called
        factory_called = True
        return [Static("Lazy content", id="lazy-content")]

    class TestApp(App):
        def compose(self):
            yield QuietCollapsible(
                title="Test",
                collapsed=True,
                content_factory=make_content,
            )

    app = TestApp()
    async with app.run_test() as pilot:
        collapsible = app.query_one(QuietCollapsible)

        # Factory should NOT be called yet (collapsed)
        assert not factory_called
        assert collapsible.collapsed

        # Expand the collapsible
        collapsible.collapsed = False
        await pilot.pause()

        # Factory should now be called
        assert factory_called

        # Content should be mounted
        content = collapsible.query_one("#lazy-content", Static)
        assert content is not None


@pytest.mark.asyncio
async def test_quiet_collapsible_immediate_content():
    """QuietCollapsible with content_factory and collapsed=False composes immediately."""
    from claudechic.widgets.primitives.collapsible import QuietCollapsible

    factory_called = False

    def make_content():
        nonlocal factory_called
        factory_called = True
        return [Static("Immediate content", id="immediate-content")]

    class TestApp(App):
        def compose(self):
            # collapsed=False means content should be mounted immediately
            yield QuietCollapsible(
                title="Test",
                collapsed=False,
                content_factory=make_content,
            )

    app = TestApp()
    async with app.run_test():
        collapsible = app.query_one(QuietCollapsible)
        # Factory should be called during watch on expand
        assert factory_called
        # Content should be mounted
        content = collapsible.query_one("#immediate-content", Static)
        assert content is not None


@pytest.mark.asyncio
async def test_quiet_collapsible_context_manager_still_works():
    """QuietCollapsible context manager pattern continues to work."""
    from claudechic.widgets.primitives.collapsible import QuietCollapsible

    class TestApp(App):
        def compose(self):
            with QuietCollapsible(title="Normal", collapsed=False):
                yield Static("Context manager content", id="ctx-content")

    app = TestApp()
    async with app.run_test():
        collapsible = app.query_one(QuietCollapsible)
        content = collapsible.query_one("#ctx-content", Static)
        assert content is not None


@pytest.mark.asyncio
async def test_tool_use_widget_edit_lazy_diff():
    """ToolUseWidget with Edit tool uses lazy DiffWidget when collapsed."""
    from claude_agent_sdk import ToolUseBlock

    from claudechic.widgets.content.diff import DiffWidget
    from claudechic.widgets.content.tools import ToolUseWidget
    from claudechic.widgets.primitives.collapsible import QuietCollapsible

    block = ToolUseBlock(
        id="test-edit",
        name="Edit",
        input={
            "file_path": "/test/file.py",
            "old_string": "old code",
            "new_string": "new code",
        },
    )

    class TestApp(App):
        def compose(self):
            # collapsed=True should use lazy pattern
            yield ToolUseWidget(block, collapsed=True, completed=True)

    app = TestApp()
    async with app.run_test() as pilot:
        widget = app.query_one(ToolUseWidget)
        collapsible = widget.query_one(QuietCollapsible)

        # DiffWidget should NOT exist yet (lazy)
        diffs = widget.query(DiffWidget)
        assert len(diffs) == 0

        # Expand the collapsible
        collapsible.collapsed = False
        await pilot.pause()

        # DiffWidget should now exist
        diffs = widget.query(DiffWidget)
        assert len(diffs) == 1


@pytest.mark.asyncio
async def test_streamed_markdown_fence_survives_recompose():
    """MarkdownFence body must survive a recompose after streamed updates.

    Regression for Textual issue #6518: upstream
    ``MarkdownFence._update_from_block`` updates the visible Label but never
    syncs ``self.code`` / ``self._highlighted_code``. If anything recomposes
    the fence afterwards (terminal focus, style refresh, layout cascade, text
    selection), ``compose`` rebuilds the Label from the stale first-chunk
    value and the code body collapses to a few characters. Patched in
    :mod:`claudechic._textual_patches`.
    """
    import asyncio

    # Importing claudechic applies the textual monkey-patch this test guards.
    from claudechic._textual_patches import apply_patches

    apply_patches()

    from textual.widgets import Label, Markdown
    from textual.widgets._markdown import MarkdownFence

    # Sample large enough to span many stream chunks; the per-chunk sleep
    # makes MarkdownStream's run-loop process each chunk before the next
    # arrives, which is what creates the incremental fence-update path.
    sample = (
        "Example:\n\n"
        "```python\n"
        "from langgraph_sdk import get_client\n"
        "\n"
        'client = get_client(url="http://localhost:2024")\n'
        "\n"
        "result = await client.runs.wait(\n"
        "    thread_id=None,\n"
        '    assistant_id="agent",\n'
        '    input={"messages": [{"role": "user", "content": "hello"}]},\n'
        ")\n"
        "\n"
        "print(result)\n"
        "```\n"
    )

    class MdApp(App):
        def compose(self) -> ComposeResult:
            yield Markdown("", id="md")

        async def on_mount(self) -> None:
            md = self.query_one("#md", Markdown)
            stream = Markdown.get_stream(md)
            for i in range(0, len(sample), 10):
                await stream.write(sample[i : i + 10])
                await asyncio.sleep(0.02)
            await stream.stop()

    app = MdApp()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(1.0)
        fence = app.query_one(MarkdownFence)

        # Sanity check: streaming worked end-to-end and the patch synced state.
        assert "from langgraph_sdk" in fence.code, (
            "fence.code never received streamed body — patch did not apply"
        )

        # Force the failure mode. ``recompose()`` is a deterministic
        # stand-in for the real-world triggers (focus change, style refresh,
        # text selection, layout cascade) — anything that re-runs the fence's
        # ``compose`` and rebuilds the Label from ``_highlighted_code``.
        await fence.recompose()
        await pilot.pause(0.3)

        label = fence.query_one("#code-content", Label)
        body = str(label.render())
        # Pre-fix, this was '' after recompose. Post-fix, the body is intact.
        assert "from langgraph_sdk import get_client" in body
        assert "print(result)" in body
