"""Tests for config module."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from claudechic.config import (
    get_claude_model,
    set_claude_model,
    CLAUDE_SETTINGS_PATH,
)


@pytest.fixture
def temp_settings(tmp_path, monkeypatch):
    """Use a temporary settings.json path."""
    settings_path = tmp_path / "settings.json"
    monkeypatch.setattr("claudechic.config.CLAUDE_SETTINGS_PATH", settings_path)
    return settings_path


class TestGetClaudeModel:
    def test_returns_none_when_file_missing(self, temp_settings):
        assert not temp_settings.exists()
        assert get_claude_model() is None

    def test_returns_none_when_model_key_missing(self, temp_settings):
        temp_settings.write_text('{"other": "value"}')
        assert get_claude_model() is None

    def test_returns_model_value(self, temp_settings):
        temp_settings.write_text('{"model": "opus"}')
        assert get_claude_model() == "opus"

    def test_returns_none_on_malformed_json(self, temp_settings):
        temp_settings.write_text("not valid json {")
        assert get_claude_model() is None

    def test_preserves_other_settings(self, temp_settings):
        temp_settings.write_text('{"model": "sonnet", "apiKey": "sk-123"}')
        assert get_claude_model() == "sonnet"


class TestSetClaudeModel:
    def test_creates_file_if_missing(self, temp_settings):
        assert not temp_settings.exists()
        assert set_claude_model("opus") is True
        assert temp_settings.exists()
        data = json.loads(temp_settings.read_text())
        assert data["model"] == "opus"

    def test_updates_existing_model(self, temp_settings):
        temp_settings.write_text('{"model": "sonnet"}')
        assert set_claude_model("opus") is True
        data = json.loads(temp_settings.read_text())
        assert data["model"] == "opus"

    def test_preserves_other_keys(self, temp_settings):
        temp_settings.write_text('{"model": "sonnet", "apiKey": "sk-123", "theme": "dark"}')
        assert set_claude_model("opus") is True
        data = json.loads(temp_settings.read_text())
        assert data["model"] == "opus"
        assert data["apiKey"] == "sk-123"
        assert data["theme"] == "dark"

    def test_default_removes_model_key(self, temp_settings):
        temp_settings.write_text('{"model": "opus", "other": "value"}')
        assert set_claude_model("default") is True
        data = json.loads(temp_settings.read_text())
        assert "model" not in data
        assert data["other"] == "value"

    def test_default_on_missing_key_is_noop(self, temp_settings):
        temp_settings.write_text('{"other": "value"}')
        assert set_claude_model("default") is True
        data = json.loads(temp_settings.read_text())
        assert "model" not in data
        assert data["other"] == "value"

    def test_handles_malformed_json_by_overwriting(self, temp_settings):
        temp_settings.write_text("not valid json")
        assert set_claude_model("opus") is True
        data = json.loads(temp_settings.read_text())
        assert data["model"] == "opus"

    def test_creates_parent_directory(self, tmp_path, monkeypatch):
        settings_path = tmp_path / "subdir" / "settings.json"
        monkeypatch.setattr("claudechic.config.CLAUDE_SETTINGS_PATH", settings_path)
        assert not settings_path.parent.exists()
        assert set_claude_model("opus") is True
        assert settings_path.exists()

    def test_returns_false_on_write_error(self, temp_settings, monkeypatch):
        # Make the directory read-only to cause write failure
        temp_settings.parent.chmod(0o444)
        try:
            result = set_claude_model("opus")
            assert result is False
        finally:
            temp_settings.parent.chmod(0o755)
