"""Tests for claudechic.agent module-level helpers and Agent permission handling."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from claude_agent_sdk import PermissionResultAllow

from claudechic.agent import Agent, get_default_permission_mode, to_ui_permission_mode


def _write_settings(path: Path, default_mode: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": {"defaultMode": default_mode}}))


def test_default_permission_mode_falls_back_to_default(tmp_path):
    """No settings anywhere → 'default'."""
    with patch("claudechic.agent.Path.home", return_value=tmp_path / "empty-home"):
        assert get_default_permission_mode(tmp_path / "empty-proj") == "default"


def test_default_permission_mode_reads_user_settings(tmp_path):
    """User-level ~/.claude/settings.json is honored."""
    home = tmp_path / "home"
    _write_settings(home / ".claude" / "settings.json", "auto")
    with patch("claudechic.agent.Path.home", return_value=home):
        assert get_default_permission_mode(tmp_path / "proj") == "auto"


def test_default_permission_mode_local_beats_project_beats_user(tmp_path):
    """Layering: local > project > user."""
    home = tmp_path / "home"
    proj = tmp_path / "proj"
    _write_settings(home / ".claude" / "settings.json", "auto")
    _write_settings(proj / ".claude" / "settings.json", "acceptEdits")
    _write_settings(proj / ".claude" / "settings.local.json", "plan")
    with patch("claudechic.agent.Path.home", return_value=home):
        assert get_default_permission_mode(proj) == "plan"


def test_default_permission_mode_passes_sdk_only_modes_through(tmp_path):
    """Modes the SDK understands but the UI doesn't (e.g. bypassPermissions) are
    returned verbatim, so the SDK still honors the user's intent."""
    home = tmp_path / "home"
    _write_settings(home / ".claude" / "settings.json", "bypassPermissions")
    with patch("claudechic.agent.Path.home", return_value=home):
        assert get_default_permission_mode(tmp_path / "proj") == "bypassPermissions"


def test_default_permission_mode_rejects_invalid_mode(tmp_path):
    """Totally bogus values fall back to 'default' rather than propagating."""
    home = tmp_path / "home"
    _write_settings(home / ".claude" / "settings.json", "nonsense")
    with patch("claudechic.agent.Path.home", return_value=home):
        assert get_default_permission_mode(tmp_path / "proj") == "default"


def test_to_ui_permission_mode_collapses_sdk_only_modes():
    """bypassPermissions / dontAsk → default (UI can't represent them)."""
    assert to_ui_permission_mode("bypassPermissions") == "default"
    assert to_ui_permission_mode("dontAsk") == "default"


def test_to_ui_permission_mode_passes_ui_modes_through():
    for mode in ("default", "acceptEdits", "plan", "auto"):
        assert to_ui_permission_mode(mode) == mode


@pytest.mark.asyncio
async def test_handle_permission_auto_mode_approves_all(tmp_path):
    """In auto mode, every tool is auto-approved without prompting.

    Regression test: switching back into auto mode mid-session via
    set_permission_mode used to leave the CLI calling can_use_tool, and
    _handle_permission had no branch for "auto" — so it prompted the user
    instead of auto-approving.
    """
    agent = Agent(name="t", cwd=tmp_path, permission_mode="auto")
    ctx = MagicMock()
    cases = [
        ("Bash", {"command": "rm -rf /"}),
        ("Edit", {"file_path": "/x", "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": "/x", "content": "y"}),
        ("Read", {"file_path": "/x"}),
    ]
    for tool, inp in cases:
        result = await agent._handle_permission(tool, inp, ctx)
        assert isinstance(result, PermissionResultAllow), tool


@pytest.mark.asyncio
async def test_set_permission_mode_rolls_back_on_sdk_rejection(tmp_path):
    """SDK rejection (e.g. "auto" on Sonnet) rolls back, caches, raises ValueError.

    This is the contract the UI relies on: the agent's prior mode is
    preserved (so the footer doesn't lie about what the CLI is in),
    ``unsupported_modes`` records the rejection so the UI can avoid
    re-asking, and a typed ``ValueError`` lets callers render a friendly
    message instead of crashing.
    """
    agent = Agent(name="t", cwd=tmp_path, permission_mode="acceptEdits")
    agent.client = MagicMock()
    agent.client.set_permission_mode = AsyncMock(
        side_effect=Exception("this model does not have Auto mode")
    )

    with pytest.raises(ValueError, match="Auto mode"):
        await agent.set_permission_mode("auto")

    assert agent.permission_mode == "acceptEdits"  # rolled back
    assert "auto" in agent.unsupported_modes


@pytest.mark.asyncio
async def test_set_permission_mode_no_op_when_already_at_target(tmp_path):
    """Setting the same mode is a no-op — no SDK call, no rollback drama."""
    agent = Agent(name="t", cwd=tmp_path, permission_mode="auto")
    agent.client = MagicMock()
    agent.client.set_permission_mode = AsyncMock()

    await agent.set_permission_mode("auto")

    agent.client.set_permission_mode.assert_not_awaited()


@pytest.mark.asyncio
async def test_set_permission_mode_clears_unsupported_on_disconnect(tmp_path):
    """Cached SDK rejections are tied to the current connection; reconnect resets.

    A user could be on Sonnet (auto rejected), switch to Opus, and
    reasonably expect "auto" to work again. ``disconnect`` is the seam
    where that capability might change, so we clear the cache there.
    """
    agent = Agent(name="t", cwd=tmp_path, permission_mode="acceptEdits")
    agent.unsupported_modes.add("auto")

    await agent.disconnect()

    assert agent.unsupported_modes == set()
