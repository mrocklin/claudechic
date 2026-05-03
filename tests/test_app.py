"""Unit tests for ChatApp methods."""

import asyncio
import base64
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from claudechic.agent import Agent, ImageAttachment
from claudechic.enums import AgentStatus


def test_image_attachment_message_building():
    """Test that images are correctly formatted in messages."""
    agent = Agent(name="test", cwd=Path.cwd())

    # Add a test image
    test_data = base64.b64encode(b"fake image data").decode()
    agent.pending_images.append(
        ImageAttachment("/tmp/test.png", "test.png", "image/png", test_data)
    )

    # Build message
    msg = agent._build_message_with_images("What is this?")

    # Verify structure
    assert msg["type"] == "user"
    content = msg["message"]["content"]
    assert len(content) == 2
    assert content[0] == {"type": "text", "text": "What is this?"}
    assert content[1]["type"] == "image"
    assert content[1]["source"]["type"] == "base64"
    assert content[1]["source"]["media_type"] == "image/png"
    assert content[1]["source"]["data"] == test_data


@pytest.mark.asyncio
async def test_context_command_assistant_message_routes_to_command_output():
    """/context output arrives as an AssistantMessage with a single
    non-streamed TextBlock containing the markdown. It must be routed to
    observer.on_command_output so the ContextReport widget renders.

    Regression guard: TextBlocks in AssistantMessage are normally skipped
    (already streamed via StreamEvent). The exception is non-streamed
    slash-command output like /context.
    """
    agent = Agent(name="test", cwd=Path.cwd())
    observer = MagicMock()
    observer.on_command_output = MagicMock()
    agent.observer = observer

    msg = AssistantMessage(
        content=[TextBlock(text="## Context Usage\n\n**Tokens:** 19k / 1m\n")],
        model="claude-opus-4-7",
    )
    await agent._handle_sdk_message(msg)

    observer.on_command_output.assert_called_once()
    payload = observer.on_command_output.call_args[0][1]
    assert "## Context Usage" in payload
    assert "19k / 1m" in payload


@pytest.mark.asyncio
async def test_streamed_textblock_does_not_duplicate_command_output():
    """Normal streamed TextBlocks must NOT be re-routed as command output —
    they're already shown via StreamEvent deltas. Guard against double rendering.
    """
    agent = Agent(name="test", cwd=Path.cwd())
    observer = MagicMock()
    observer.on_command_output = MagicMock()
    agent.observer = observer

    # Simulate prior streaming: buffer is non-empty.
    agent._current_text_buffer = "## Context Usage already streamed"

    msg = AssistantMessage(
        content=[TextBlock(text="## Context Usage already streamed")],
        model="claude-opus-4-7",
    )
    await agent._handle_sdk_message(msg)

    observer.on_command_output.assert_not_called()


@pytest.mark.asyncio
async def test_reader_handles_post_turn_wakeup():
    """Wakeup regression test.

    Tools like ScheduleWakeup/Monitor cause the SDK to emit a fresh batch of
    messages after a ResultMessage. The reader must keep listening past
    ResultMessage so the wakeup-triggered work surfaces in the UI without the
    user having to send a "ping" message to wake things up.
    """
    # ResultMessage has many required fields; only session_id is read by the
    # agent. The rest are set to inert minimums.
    result = ResultMessage(
        subtype="success",
        duration_ms=0,
        duration_api_ms=0,
        is_error=False,
        num_turns=1,
        session_id="sess-1",
    )
    text = lambda s: AssistantMessage(  # noqa: E731
        content=[TextBlock(text=s)], model="claude-opus-4-7"
    )

    queue: asyncio.Queue = asyncio.Queue()

    async def fake_receive_messages():
        while True:
            msg = await queue.get()
            if msg is None:
                return
            yield msg

    async def wait_until(predicate, msg: str, timeout: float = 0.5):
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if predicate():
                return
            await asyncio.sleep(0.01)
        raise AssertionError(msg)

    agent = Agent(name="test", cwd=Path.cwd())
    agent.client = MagicMock()
    agent.client.receive_messages = fake_receive_messages

    observer = MagicMock()
    agent.observer = observer

    # Simulate the BUSY transition that send() performs before query().
    agent._set_status(AgentStatus.BUSY)

    reader = asyncio.create_task(agent._read_messages_forever())
    try:
        # Turn 1: assistant text + ResultMessage → status returns to IDLE.
        await queue.put(text("hello from turn 1"))
        await queue.put(result)
        await wait_until(
            lambda: agent.status == AgentStatus.IDLE
            and observer.on_complete.call_count == 1,
            "Turn 1 did not complete",
        )

        # SystemMessages while IDLE must NOT trigger a spurious BUSY (this is
        # what would otherwise happen when the SDK emits the connection-init
        # message at startup).
        await queue.put(SystemMessage(subtype="init", data={"session_id": "sess-1"}))
        # Drain the message without flipping state.
        await asyncio.sleep(0.05)
        assert agent.status == AgentStatus.IDLE, (
            "SystemMessage spuriously flipped status to BUSY"
        )

        # Dirty per-turn state to prove the wakeup transition resets it.
        agent.response_had_tools = True
        agent._current_text_buffer = "stale"

        # Turn 2: a wakeup fires — pre-fix this never reached the reader.
        await queue.put(text("hello from wakeup"))
        await wait_until(
            lambda: agent.status == AgentStatus.BUSY,
            "Reader did not transition to BUSY on wakeup-triggered message",
        )
        # State was reset before processing the wakeup message. The wakeup
        # message is an AssistantMessage whose TextBlock is skipped by
        # _handle_sdk_message (treated as a streaming duplicate), so the
        # buffer should be exactly empty — not just "stale gone".
        assert agent.response_had_tools is False, (
            "Wakeup transition did not reset response_had_tools"
        )
        assert agent._current_text_buffer == "", (
            "Wakeup transition did not reset the text buffer"
        )

        await queue.put(result)
        await wait_until(
            lambda: observer.on_complete.call_count == 2
            and agent.status == AgentStatus.IDLE,
            "Wakeup turn's ResultMessage was not observed",
        )
    finally:
        await queue.put(None)
        reader.cancel()
        try:
            await reader
        except asyncio.CancelledError:
            pass
