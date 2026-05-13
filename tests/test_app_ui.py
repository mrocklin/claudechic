"""App-level UI tests without SDK dependency."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudechic.app import ChatApp
from claudechic.widgets import (
    ChatInput,
    ChatMessage,
    AgentSection,
    TodoPanel,
    StatusFooter,
)
from claudechic.messages import (
    ResponseComplete,
    ToolUseMessage,
    ToolResultMessage,
)
from claude_agent_sdk import ToolUseBlock, ToolResultBlock
from tests.conftest import wait_for_workers, submit_command


@pytest.mark.asyncio
async def test_app_mounts_basic_widgets(mock_sdk):
    """App mounts all expected widgets on startup."""
    app = ChatApp()
    async with app.run_test():
        # Check key widgets exist
        assert app.query_one("#input", ChatInput)
        assert app.query_one("#agent-section", AgentSection)
        assert app.query_one("#todo-panel", TodoPanel)
        assert app.query_one(StatusFooter)


@pytest.mark.asyncio
async def test_permission_mode_cycle(mock_sdk):
    """Shift+Tab cycles permission mode: default -> acceptEdits -> plan -> auto -> default."""
    app = ChatApp()
    async with app.run_test() as pilot:
        assert app._agent is not None
        assert app._agent.permission_mode == "default"

        await pilot.press("shift+tab")
        assert app._agent.permission_mode == "acceptEdits"

        await pilot.press("shift+tab")
        assert app._agent.permission_mode == "plan"

        await pilot.press("shift+tab")
        assert app._agent.permission_mode == "auto"

        await pilot.press("shift+tab")
        assert app._agent.permission_mode == "default"


@pytest.mark.asyncio
async def test_permission_mode_cycle_skips_sdk_rejected(mock_sdk):
    """Shift+Tab through a mode the SDK rejects ("auto" on Sonnet) advances past it.

    First press lands on the unsupported mode, the SDK rejects, the cycle
    catches the ValueError and advances to the next candidate — so a
    single Shift+Tab from "plan" goes straight to "default" rather than
    crashing or sitting in "auto". On subsequent cycles, the cached
    rejection causes a silent skip (no second SDK round-trip).
    """
    app = ChatApp()
    async with app.run_test() as pilot:
        agent = app._agent
        assert agent is not None
        client = agent.client
        assert client is not None

        # Mock the SDK to reject "auto" specifically (mimicking Sonnet/Haiku).
        async def reject_auto(mode):
            if mode == "auto":
                raise Exception("this model does not have Auto mode")

        client.set_permission_mode = AsyncMock(side_effect=reject_auto)

        # Walk default -> acceptEdits -> plan
        await pilot.press("shift+tab")
        await pilot.press("shift+tab")
        assert agent.permission_mode == "plan"

        # Next press would be "auto" — SDK rejects, cycle advances to "default".
        await pilot.press("shift+tab")
        await wait_for_workers(app)
        assert agent.permission_mode == "default"
        assert "auto" in agent.unsupported_modes

        # Second pass: cycle should silently skip "auto" (no extra SDK call).
        client.set_permission_mode.reset_mock()
        await pilot.press("shift+tab")  # default -> acceptEdits
        await pilot.press("shift+tab")  # acceptEdits -> plan
        await pilot.press("shift+tab")  # plan -> (skip auto) -> default
        await wait_for_workers(app)
        assert agent.permission_mode == "default"
        # Three transitions, three SDK calls — none for "auto".
        called_modes = [
            call.args[0] for call in client.set_permission_mode.call_args_list
        ]
        assert "auto" not in called_modes


@pytest.mark.asyncio
async def test_permission_mode_footer_updates(mock_sdk):
    """Footer reflects permission mode state."""
    app = ChatApp()
    async with app.run_test() as pilot:
        footer = app.query_one(StatusFooter)
        assert footer.permission_mode == "default"

        await pilot.press("shift+tab")
        assert footer.permission_mode == "acceptEdits"


@pytest.mark.asyncio
async def test_clear_command(mock_sdk):
    """'/clear' removes chat messages."""
    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        # Add some fake messages
        msg1 = ChatMessage("Test 1")
        msg2 = ChatMessage("Test 2")
        chat_view.mount(msg1)
        chat_view.mount(msg2)
        await pilot.pause()

        assert len(chat_view.children) == 2

        # Send /clear (which clears UI and sends to SDK)
        await submit_command(app, pilot, "/clear")
        await wait_for_workers(app)
        await pilot.pause()  # Let DOM updates complete

        # Chat view should be empty
        messages = list(chat_view.query(ChatMessage))
        assert len(messages) == 0  # Our messages were cleared


@pytest.mark.asyncio
async def test_agent_list_command(mock_sdk):
    """'/agent' lists agents."""
    app = ChatApp()
    async with app.run_test() as pilot:
        # Should have one default agent
        assert len(app.agents) == 1

        await submit_command(app, pilot, "/agent")

        # The command shows notifications - just verify we have one agent
        assert len(app.agents) == 1


@pytest.mark.asyncio
async def test_agent_create_command(mock_sdk):
    """'/agent foo' creates new agent."""
    app = ChatApp()
    async with app.run_test() as pilot:
        assert len(app.agents) == 1

        await submit_command(app, pilot, "/agent test-agent")
        await wait_for_workers(app)

        assert len(app.agents) == 2
        agent_names = [a.name for a in app.agents.values()]
        assert "test-agent" in agent_names


@pytest.mark.asyncio
async def test_agent_switch_keybinding(mock_sdk):
    """Ctrl+1-9 switches agents."""
    app = ChatApp()
    async with app.run_test() as pilot:
        # Create second agent
        await submit_command(app, pilot, "/agent second")
        await wait_for_workers(app)

        assert len(app.agents) == 2
        agent_ids = list(app.agents.keys())

        # Should be on second agent now (just created)
        assert app.active_agent_id == agent_ids[1]

        # Switch to first agent with ctrl+1
        await pilot.press("ctrl+1")
        assert app.active_agent_id == agent_ids[0]

        # Switch to second agent with ctrl+2
        await pilot.press("ctrl+2")
        assert app.active_agent_id == agent_ids[1]


@pytest.mark.asyncio
async def test_agent_close_command(mock_sdk):
    """'/agent close' closes current agent."""
    app = ChatApp()
    async with app.run_test() as pilot:
        # Create second agent first
        await submit_command(app, pilot, "/agent to-close")
        await wait_for_workers(app)

        assert len(app.agents) == 2
        assert any(a.name == "to-close" for a in app.agents.values())

        # Close current agent
        await submit_command(app, pilot, "/agent close")
        await wait_for_workers(app)
        await pilot.pause()  # Let DOM updates complete

        # Should be back to one agent
        assert len(app.agents) == 1


@pytest.mark.asyncio
async def test_cannot_close_last_agent(mock_sdk):
    """Cannot close the last remaining agent."""
    app = ChatApp()
    async with app.run_test() as pilot:
        assert len(app.agents) == 1

        await submit_command(app, pilot, "/agent close")
        await wait_for_workers(app)

        # Still have one agent
        assert len(app.agents) == 1


@pytest.mark.asyncio
async def test_sidebar_agent_selection(mock_sdk):
    """Clicking agent in sidebar switches to it."""
    app = ChatApp()
    async with app.run_test() as pilot:
        # Create second agent
        await submit_command(app, pilot, "/agent sidebar-test")
        await wait_for_workers(app)

        sidebar = app.query_one("#agent-section", AgentSection)
        agent_ids = list(app.agents.keys())

        # Second agent should be active (just created)
        assert app.active_agent_id == agent_ids[1]

        # Simulate clicking first agent
        first_agent_widget = sidebar._agents[agent_ids[0]]
        first_agent_widget.post_message(first_agent_widget.Selected(agent_ids[0]))
        await pilot.pause()

        # First agent should now be active
        assert app.active_agent_id == agent_ids[0]


@pytest.mark.asyncio
async def test_resume_shows_session_screen(mock_sdk):
    """'/resume' shows session screen."""
    from claudechic.screens import SessionScreen

    app = ChatApp()
    async with app.run_test() as pilot:
        await submit_command(app, pilot, "/resume")

        # Session screen should be on screen stack
        assert isinstance(app.screen, SessionScreen)


@pytest.mark.asyncio
async def test_escape_hides_session_screen(mock_sdk):
    """Escape hides session screen."""
    from claudechic.screens import SessionScreen

    app = ChatApp()
    async with app.run_test() as pilot:
        await submit_command(app, pilot, "/resume")

        assert isinstance(app.screen, SessionScreen)

        # Press escape to dismiss screen
        await pilot.press("escape")
        await pilot.pause()

        assert not isinstance(app.screen, SessionScreen)


@pytest.mark.asyncio
async def test_double_ctrl_c_quits(mock_sdk):
    """Double Ctrl+C quits app."""
    app = ChatApp()
    async with app.run_test() as pilot:
        # First Ctrl+C shows warning
        await pilot.press("ctrl+c")
        assert hasattr(app, "_last_quit_time")

        # Second quick Ctrl+C would exit (but we can't test actual exit easily)
        # Just verify the mechanism exists
        import time

        assert time.time() - app._last_quit_time < 2.0


@pytest.mark.asyncio
async def test_stream_chunk_creates_message(mock_sdk):
    """Text streaming creates ChatMessage widget."""
    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        # Simulate text chunk (now direct call, not message)
        chat_view.append_text("Hello ", new_message=True, parent_tool_id=None)
        await pilot.pause()

        # Should have created a ChatMessage
        messages = list(chat_view.query(ChatMessage))
        assert len(messages) == 1
        assert messages[0].get_raw_content() == "Hello "


@pytest.mark.asyncio
async def test_stream_chunk_appends_to_message(mock_sdk):
    """Sequential text chunks append to same message."""
    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        chat_view.append_text("Hello ", new_message=True, parent_tool_id=None)
        await pilot.pause()
        chat_view.append_text("world!", new_message=False, parent_tool_id=None)
        await pilot.pause()

        messages = list(chat_view.query(ChatMessage))
        assert len(messages) == 1
        assert messages[0].get_raw_content() == "Hello world!"


@pytest.mark.asyncio
async def test_stream_chunks_interleaved_with_tools(mock_sdk):
    """Text after tool use creates a new ChatMessage (not appended to first)."""
    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None
        agent_id = app.active_agent_id

        # First text chunk
        chat_view.append_text("Planning...", new_message=True, parent_tool_id=None)
        await pilot.pause()

        # Tool use
        tool_block = ToolUseBlock(
            id="tool-1", name="Read", input={"file_path": "/test.py"}
        )
        app.post_message(ToolUseMessage(tool_block, agent_id=agent_id))
        await pilot.pause()

        # Tool result
        result_block = ToolResultBlock(
            tool_use_id="tool-1", content="file contents", is_error=False
        )
        app.post_message(ToolResultMessage(result_block, agent_id=agent_id))
        await pilot.pause()

        # Second text chunk (should be new_message=True after tool)
        chat_view.append_text("Done!", new_message=True, parent_tool_id=None)
        await pilot.pause()

        messages = list(chat_view.query(ChatMessage))
        assert len(messages) == 2, f"Expected 2 messages, got {len(messages)}"
        assert messages[0].get_raw_content() == "Planning..."
        assert messages[1].get_raw_content() == "Done!"


@pytest.mark.asyncio
async def test_response_complete_enables_input(mock_sdk):
    """ResponseComplete focuses input."""
    app = ChatApp()
    async with app.run_test() as pilot:
        agent_id = app.active_agent_id
        app.post_message(ResponseComplete(None, agent_id=agent_id))
        await pilot.pause()

        input_widget = app.query_one("#input", ChatInput)
        assert app.focused == input_widget


@pytest.mark.asyncio
async def test_sidebar_hidden_when_single_agent(mock_sdk):
    """Right sidebar hidden with single agent and no todos."""
    app = ChatApp()
    async with app.run_test(size=(100, 40)):
        sidebar = app.query_one("#right-sidebar")
        # With single agent and no todos, sidebar should be hidden
        assert sidebar.has_class("hidden")


@pytest.mark.asyncio
async def test_sidebar_shows_with_multiple_agents(mock_sdk):
    """Right sidebar shows with multiple agents when wide enough."""
    app = ChatApp()
    async with app.run_test(size=(160, 40)) as pilot:
        # Create second agent
        await submit_command(app, pilot, "/agent second")
        await wait_for_workers(app)

        # Trigger resize handling
        app._position_right_sidebar()

        sidebar = app.query_one("#right-sidebar")
        # With multiple agents and wide enough, sidebar should show
        assert sidebar.display is True


@pytest.mark.asyncio
async def test_command_output_displays(mock_sdk):
    """CommandOutputMessage displays content in chat."""
    from claudechic.messages import CommandOutputMessage

    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        # Post a command output message
        agent_id = app.active_agent_id
        app.post_message(
            CommandOutputMessage("## Test Output\n\nSome content", agent_id=agent_id)
        )
        await pilot.pause()

        # Should have created a ChatMessage with system-message class
        messages = list(chat_view.query(ChatMessage))
        assert len(messages) == 1
        assert "## Test Output" in messages[0].get_raw_content()
        assert messages[0].has_class("system-message")


@pytest.mark.asyncio
async def test_context_report_displays(mock_sdk):
    """Context command output displays as ContextReport widget."""
    from claudechic.messages import CommandOutputMessage
    from claudechic.widgets.reports.context import ContextReport

    CONTEXT_OUTPUT = """## Context Usage

**Model:** claude-opus-4-5-20251101
**Tokens:** 81.0k / 200.0k (41%)

### Categories

| Category | Tokens | Percentage |
|----------|--------|------------|
| System prompt | 3.0k | 1.5% |
| Messages | 58.8k | 29.4% |
| Free space | 74.0k | 36.9% |
"""

    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        agent_id = app.active_agent_id
        app.post_message(CommandOutputMessage(CONTEXT_OUTPUT, agent_id=agent_id))
        await pilot.pause()

        # Should have created a ContextReport, not ChatMessage
        reports = list(chat_view.query(ContextReport))
        assert len(reports) == 1

        # Verify data was parsed
        assert reports[0].data["model"] == "claude-opus-4-5-20251101"
        assert reports[0].data["tokens_used"] == 81000


@pytest.mark.asyncio
async def test_refresh_context_reads_sdk_usage(mock_sdk):
    """refresh_context() pulls rawMaxTokens and totalTokens from the SDK.

    Regression guard: if the SDK renames either field, the bar silently
    stays stuck at its previous value — this test locks the contract.
    """
    mock_sdk.get_context_usage = AsyncMock(
        return_value={
            "rawMaxTokens": 1_000_000,
            "totalTokens": 42_000,
        }
    )

    app = ChatApp()
    async with app.run_test():
        app.refresh_context()
        await wait_for_workers(app)

        assert app.context_bar.max_tokens == 1_000_000
        assert app.context_bar.tokens == 42_000


@pytest.mark.asyncio
async def test_refresh_context_survives_sdk_error(mock_sdk):
    """SDK failure is non-fatal — bar keeps its prior value, app doesn't crash."""
    mock_sdk.get_context_usage = AsyncMock(side_effect=RuntimeError("boom"))

    app = ChatApp()
    async with app.run_test():
        app.context_bar.max_tokens = 500_000
        app.context_bar.tokens = 123
        app.refresh_context()
        await wait_for_workers(app)

        # Bar unchanged; no exception propagated to the app.
        assert app.context_bar.max_tokens == 500_000
        assert app.context_bar.tokens == 123


@pytest.mark.asyncio
async def test_system_notification_shows_in_chat(mock_sdk):
    """SystemNotification creates SystemInfo widget in chat."""
    from claudechic.messages import SystemNotification
    from claudechic.widgets import SystemInfo
    from claude_agent_sdk import SystemMessage

    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        # Create a system message (simulating SDK)
        sdk_msg = SystemMessage(
            subtype="test_notification",
            data={"content": "Test system message", "level": "info"},
        )

        # Post the notification
        app.post_message(SystemNotification(sdk_msg, agent_id=app.active_agent_id))
        await pilot.pause()

        # Should have a SystemInfo widget in chat
        info_widgets = list(chat_view.query(SystemInfo))
        assert len(info_widgets) == 1
        assert info_widgets[0]._message == "Test system message"


@pytest.mark.asyncio
async def test_system_notification_api_error(mock_sdk):
    """API error notification displays correctly."""
    from claudechic.messages import SystemNotification
    from claudechic.widgets import SystemInfo
    from claude_agent_sdk import SystemMessage

    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        # Create an api_error system message
        sdk_msg = SystemMessage(
            subtype="api_error",
            data={
                "level": "error",
                "error": {"error": {"message": "Rate limited"}},
                "retryAttempt": 2,
                "maxRetries": 10,
            },
        )

        app.post_message(SystemNotification(sdk_msg, agent_id=app.active_agent_id))
        await pilot.pause()

        info_widgets = list(chat_view.query(SystemInfo))
        assert len(info_widgets) == 1
        assert "retry 2/10" in info_widgets[0]._message
        assert "Rate limited" in info_widgets[0]._message


@pytest.mark.asyncio
async def test_system_notification_compact_boundary(mock_sdk):
    """Compact boundary notification displays."""
    from claudechic.messages import SystemNotification
    from claudechic.widgets import SystemInfo
    from claude_agent_sdk import SystemMessage

    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        sdk_msg = SystemMessage(
            subtype="compact_boundary",
            data={"content": "Conversation compacted", "level": "info"},
        )

        app.post_message(SystemNotification(sdk_msg, agent_id=app.active_agent_id))
        await pilot.pause()

        info_widgets = list(chat_view.query(SystemInfo))
        assert len(info_widgets) == 1
        assert "compacted" in info_widgets[0]._message.lower()


@pytest.mark.asyncio
async def test_system_notification_ignored_subtypes(mock_sdk):
    """Certain subtypes are silently ignored."""
    from claudechic.messages import SystemNotification
    from claudechic.widgets import SystemInfo
    from claude_agent_sdk import SystemMessage

    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        # These subtypes should not create widgets
        for subtype in ["stop_hook_summary", "turn_duration", "local_command"]:
            sdk_msg = SystemMessage(subtype=subtype, data={"level": "info"})
            app.post_message(SystemNotification(sdk_msg, agent_id=app.active_agent_id))

        await pilot.pause()

        # No SystemInfo widgets should be created
        info_widgets = list(chat_view.query(SystemInfo))
        assert len(info_widgets) == 0


@pytest.mark.asyncio
async def test_sdk_stderr_shows_in_chat(mock_sdk):
    """SDK stderr callback routes messages to chat view."""
    from claudechic.widgets import SystemInfo

    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        # Simulate SDK stderr output
        app._handle_sdk_stderr("An update to our Terms of Service")
        await pilot.pause()

        # Should create a SystemInfo widget
        info_widgets = list(chat_view.query(SystemInfo))
        assert len(info_widgets) == 1
        assert "Terms of Service" in info_widgets[0]._message


@pytest.mark.asyncio
async def test_sdk_stderr_ignores_empty(mock_sdk):
    """SDK stderr callback ignores empty/whitespace messages."""
    from claudechic.widgets import SystemInfo

    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        # Simulate empty stderr output
        app._handle_sdk_stderr("")
        app._handle_sdk_stderr("   ")
        app._handle_sdk_stderr("\n")
        await pilot.pause()

        # No widgets should be created
        info_widgets = list(chat_view.query(SystemInfo))
        assert len(info_widgets) == 0


@pytest.mark.asyncio
async def test_bang_command_inline_shell(mock_sdk):
    """'!cmd' runs shell command and displays output inline."""
    from claudechic.widgets import ShellOutputWidget

    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        input_widget = app.query_one("#input", ChatInput)
        input_widget.text = "!echo hello"
        await pilot.press("enter")
        await pilot.pause()

        # Should create a ShellOutputWidget
        widgets = list(chat_view.query(ShellOutputWidget))
        assert len(widgets) == 1
        assert widgets[0].command == "echo hello"
        assert "hello" in widgets[0].stdout


@pytest.mark.asyncio
async def test_bang_command_captures_stderr(mock_sdk):
    """'!cmd' captures stderr output (merged with stdout via PTY)."""
    from claudechic.widgets import ShellOutputWidget

    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        input_widget = app.query_one("#input", ChatInput)
        input_widget.text = "!echo error >&2"
        await pilot.press("enter")
        await pilot.pause()

        widgets = list(chat_view.query(ShellOutputWidget))
        assert len(widgets) == 1
        # PTY merges stdout/stderr, so check stdout (which contains both)
        assert "error" in widgets[0].stdout


@pytest.mark.asyncio
async def test_bang_command_shows_exit_code(mock_sdk):
    """'!cmd' shows non-zero exit code in title."""
    from claudechic.widgets import ShellOutputWidget

    app = ChatApp()
    async with app.run_test() as pilot:
        chat_view = app._chat_view
        assert chat_view is not None

        input_widget = app.query_one("#input", ChatInput)
        input_widget.text = "!exit 42"
        await pilot.press("enter")
        await pilot.pause()

        widgets = list(chat_view.query(ShellOutputWidget))
        assert len(widgets) == 1
        assert widgets[0].returncode == 42


@pytest.mark.asyncio
async def test_hamburger_button_narrow_screen(mock_sdk):
    """Hamburger button appears on narrow screens with multiple agents."""
    from claudechic.widgets import HamburgerButton

    app = ChatApp()
    # Start narrow (below SIDEBAR_MIN_WIDTH=110)
    async with app.run_test(size=(80, 40)) as pilot:
        # Create second agent so sidebar has content
        await submit_command(app, pilot, "/agent second")
        await wait_for_workers(app)

        hamburger = app.query_one("#hamburger-btn", HamburgerButton)

        # Trigger layout update
        app._position_right_sidebar()
        await pilot.pause()

        # Hamburger should be visible on narrow screen with multiple agents
        assert hamburger.display is True

        # Sidebar should be hidden (not overlay yet)
        sidebar = app.query_one("#right-sidebar")
        assert sidebar.display is False


@pytest.mark.asyncio
async def test_hamburger_opens_sidebar_overlay(mock_sdk):
    """Clicking hamburger opens sidebar as overlay."""

    app = ChatApp()
    async with app.run_test(size=(80, 40)) as pilot:
        # Create second agent
        await submit_command(app, pilot, "/agent second")
        await wait_for_workers(app)

        app._position_right_sidebar()
        await pilot.pause()

        sidebar = app.query_one("#right-sidebar")

        # Click hamburger
        await pilot.click("#hamburger-btn")
        await pilot.pause()

        # Sidebar should now be visible as overlay
        assert sidebar.display is True
        assert sidebar.has_class("overlay")


@pytest.mark.asyncio
async def test_escape_closes_sidebar_overlay(mock_sdk):
    """Escape key closes sidebar overlay."""

    app = ChatApp()
    async with app.run_test(size=(80, 40)) as pilot:
        # Create second agent
        await submit_command(app, pilot, "/agent second")
        await wait_for_workers(app)

        app._position_right_sidebar()
        await pilot.pause()

        # Open overlay via state directly (more reliable than click in test)
        app._sidebar_overlay_open = True
        app._position_right_sidebar()
        await pilot.pause()

        sidebar = app.query_one("#right-sidebar")
        assert sidebar.display is True, (
            "Sidebar should be visible after opening overlay"
        )
        assert app._sidebar_overlay_open, "Overlay state should be True"

        # Call action_escape directly (escape key may be consumed by input widget)
        app.action_escape()
        await pilot.pause()

        # Sidebar should be hidden again
        assert not app._sidebar_overlay_open, (
            "Overlay state should be False after escape"
        )
        assert sidebar.display is False, "Sidebar should be hidden after escape"


# =============================================================================
# Agent-scoped review polling (_stop_review_polling)
# =============================================================================


@pytest.mark.asyncio
async def test_stop_review_polling_ignores_other_agent(mock_sdk):
    """Stopping polling for agent B does not cancel agent A's timer."""
    app = ChatApp()
    async with app.run_test():
        # Simulate agent A owning the poll timer
        fake_timer = MagicMock()
        app._review_poll_timer = fake_timer
        app._review_poll_agent_id = "agent-a"

        # Stopping for a different agent should be a no-op
        app._stop_review_polling("agent-b")

        fake_timer.stop.assert_not_called()
        assert app._review_poll_timer is fake_timer
        assert app._review_poll_agent_id == "agent-a"


@pytest.mark.asyncio
async def test_stop_review_polling_stops_own_agent(mock_sdk):
    """Stopping polling for the owning agent cancels the timer."""
    app = ChatApp()
    async with app.run_test():
        fake_timer = MagicMock()
        app._review_poll_timer = fake_timer
        app._review_poll_agent_id = "agent-a"

        app._stop_review_polling("agent-a")

        fake_timer.stop.assert_called_once()
        assert app._review_poll_timer is None
        assert app._review_poll_agent_id is None


@pytest.mark.asyncio
async def test_stop_review_polling_unconditional(mock_sdk):
    """Stopping polling with no agent_id cancels unconditionally."""
    app = ChatApp()
    async with app.run_test():
        fake_timer = MagicMock()
        app._review_poll_timer = fake_timer
        app._review_poll_agent_id = "agent-a"

        app._stop_review_polling()  # No agent_id

        fake_timer.stop.assert_called_once()
        assert app._review_poll_timer is None
        assert app._review_poll_agent_id is None


@pytest.mark.asyncio
async def test_model_switch_preserves_session(mock_sdk):
    """Switching model should pass resume=session_id to preserve conversation."""
    app = ChatApp()
    async with app.run_test(size=(120, 40)):
        agent = app._agent
        if not agent:
            pytest.skip("No agent available")
        agent.session_id = "test-session-123"
        with patch.object(app, "_make_options") as mock_opts, \
             patch.object(agent, "disconnect", new_callable=AsyncMock), \
             patch.object(agent, "connect", new_callable=AsyncMock):
            mock_opts.return_value = MagicMock()
            app._set_agent_model("opus")
            await wait_for_workers(app)
            mock_opts.assert_called_once()
            _, kwargs = mock_opts.call_args
            assert kwargs["resume"] == "test-session-123"


@pytest.mark.asyncio
async def test_effort_default_maps_to_none(mock_sdk):
    """effort='default' should pass effort=None to _make_options (SDK auto)."""
    app = ChatApp()
    async with app.run_test(size=(120, 40)):
        agent = app._agent
        if not agent:
            pytest.skip("No agent available")
        agent.session_id = "test-session-456"
        agent.effort = "high"
        with patch.object(app, "_make_options") as mock_opts, \
             patch.object(agent, "disconnect", new_callable=AsyncMock), \
             patch.object(agent, "connect", new_callable=AsyncMock):
            mock_opts.return_value = MagicMock()
            app._set_agent_effort("default")
            await wait_for_workers(app)
            mock_opts.assert_called_once()
            _, kwargs = mock_opts.call_args
            assert kwargs["effort"] is None, "default should map to None"
            assert kwargs["resume"] == "test-session-456"
