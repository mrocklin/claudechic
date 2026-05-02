"""Unit tests for ChatApp methods."""

import base64
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from claude_agent_sdk import AssistantMessage, TextBlock

from claudechic.agent import Agent, ImageAttachment


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
    await agent._handle_sdk_message(msg, {})

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
    await agent._handle_sdk_message(msg, {})

    observer.on_command_output.assert_not_called()
