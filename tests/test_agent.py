"""Tests for claudechic.agent module-level helpers."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from claudechic.agent import get_default_permission_mode, to_ui_permission_mode


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
