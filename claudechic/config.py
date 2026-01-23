"""Configuration management for claudechic via ~/.claude/claudechic.yaml."""

import uuid
from pathlib import Path

import yaml

CONFIG_PATH = Path.home() / ".claude" / "claudechic.yaml"

_config: dict = {}
_loaded: bool = False
_new_install: bool = False  # True if analytics ID was just created


def _load_config() -> dict:
    """Load config from disk, creating with defaults if missing."""
    global _config, _loaded, _new_install
    if _loaded:
        return _config

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _config = yaml.safe_load(f) or {}
        is_new_file = False
    else:
        _config = {}
        is_new_file = True

    # Ensure analytics section with defaults
    if "analytics" not in _config:
        _config["analytics"] = {}
    if "enabled" not in _config["analytics"]:
        _config["analytics"]["enabled"] = True
    if "id" not in _config["analytics"]:
        if is_new_file:
            _config["analytics"]["id"] = str(uuid.uuid4())
            _new_install = True
            _save_config()
        else:
            # File exists but ID missing - don't generate, use fallback
            _config["analytics"]["id"] = "anonymous"

    _loaded = True
    return _config


def _save_config() -> None:
    """Write config to disk."""
    if not _config:
        return
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        yaml.dump(_config, f, default_flow_style=False)


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
