"""Configuration management for claudechic via ~/.claude/.claudechic.yaml."""

import os
import tempfile
import uuid
from pathlib import Path

import yaml

CONFIG_PATH = Path.home() / ".claude" / ".claudechic.yaml"
_OLD_CONFIG_PATH = Path.home() / ".claude" / "claudechic.yaml"

_config: dict = {}
_new_install: bool = False


def _load_config() -> dict:
    """Load config from disk, creating file atomically if missing."""
    global _config, _new_install
    if _config:
        return _config

    # Migrate from old config path if it exists and new doesn't
    if not CONFIG_PATH.exists() and _OLD_CONFIG_PATH.exists():
        _OLD_CONFIG_PATH.rename(CONFIG_PATH)
    elif _OLD_CONFIG_PATH.exists():
        # Clean up old config file if new one already exists
        _OLD_CONFIG_PATH.unlink()

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _config = yaml.safe_load(f) or {}
        # Provide defaults for missing keys (don't save - preserve user's file)
        _config.setdefault("analytics", {})
        _config["analytics"].setdefault("id", "anonymous")
        _config["analytics"].setdefault("enabled", True)
        # Migrate legacy vim key to vi-mode
        if "vim" in _config:
            _config["vi-mode"] = _config.pop("vim")
            _save_config()
    else:
        # New install - create config with fresh ID and save
        _config = {
            "analytics": {"enabled": True, "id": str(uuid.uuid4())},
            "recent-tools-expanded": 2,
        }
        _new_install = True
        _save_config()

    return _config


def _save_config() -> None:
    """Write config to disk atomically."""
    if not _config:
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: write to temp file, then rename
    fd, tmp_path = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(_config, f, default_flow_style=False)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def get_analytics_enabled() -> bool:
    """Check if analytics collection is enabled."""
    return _load_config()["analytics"]["enabled"]


def get_analytics_id() -> str:
    """Get the anonymous analytics ID, generating if needed."""
    return _load_config()["analytics"]["id"]


def set_analytics_enabled(enabled: bool) -> None:
    """Enable or disable analytics collection."""
    _load_config()["analytics"]["enabled"] = enabled
    _save_config()


def get_theme() -> str | None:
    """Get saved theme preference, or None if not set."""
    return _load_config().get("theme")


def set_theme(theme: str) -> None:
    """Save theme preference."""
    _load_config()["theme"] = theme
    _save_config()


def is_new_install() -> bool:
    """Check if this is a new install (analytics ID was just created)."""
    _load_config()  # Ensure config is loaded
    return _new_install


def get_vi_mode() -> bool:
    """Check if vi mode is enabled."""
    return _load_config().get("vi-mode", False)


def set_vi_mode(enabled: bool) -> None:
    """Enable or disable vi mode."""
    _load_config()["vi-mode"] = enabled
    _save_config()


# Claude Code settings.json helpers (shared with claude CLI)
CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"


def get_claude_model() -> str | None:
    """Read model from ~/.claude/settings.json.

    Returns:
        Model name if configured, None otherwise.
    """
    import json

    if not CLAUDE_SETTINGS_PATH.exists():
        return None

    try:
        with open(CLAUDE_SETTINGS_PATH, encoding="utf-8") as f:
            settings = json.load(f)
        return settings.get("model")
    except (json.JSONDecodeError, OSError):
        return None


def set_claude_model(model: str) -> bool:
    """Write model to ~/.claude/settings.json (atomic write).

    If model is "default", removes the key.

    Args:
        model: Model name to set, or "default" to remove.

    Returns:
        True if save succeeded, False otherwise.
    """
    import json

    settings: dict = {}

    # Load existing settings
    if CLAUDE_SETTINGS_PATH.exists():
        try:
            with open(CLAUDE_SETTINGS_PATH, encoding="utf-8") as f:
                settings = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass  # Start fresh if file is corrupt

    # Update model key
    if model == "default":
        settings.pop("model", None)
    else:
        settings["model"] = model

    # Atomic write
    try:
        CLAUDE_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=CLAUDE_SETTINGS_PATH.parent, suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
                f.write("\n")
            os.replace(tmp_path, CLAUDE_SETTINGS_PATH)
            return True
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            return False
    except OSError:
        return False
