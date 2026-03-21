"""Configuration management for claudechic via ~/.claude/.claudechic.yaml."""

import os
import tempfile
import uuid
from pathlib import Path

import yaml

CONFIG_PATH = Path.home() / ".claude" / ".claudechic.yaml"
_OLD_CONFIG_PATH = Path.home() / ".claude" / "claudechic.yaml"


def _load() -> tuple[dict, bool]:
    """Load config from disk, creating file atomically if missing.

    Returns (config_dict, is_new_install).
    """
    new_install = False

    # Migrate from old config path if it exists and new doesn't
    if not CONFIG_PATH.exists() and _OLD_CONFIG_PATH.exists():
        _OLD_CONFIG_PATH.rename(CONFIG_PATH)
    elif _OLD_CONFIG_PATH.exists():
        _OLD_CONFIG_PATH.unlink()

    if CONFIG_PATH.exists():
        with open(CONFIG_PATH, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        # Provide defaults for missing keys (don't save - preserve user's file)
        config.setdefault("analytics", {})
        config["analytics"].setdefault("id", "anonymous")
        config["analytics"].setdefault("enabled", True)
        config.setdefault("experimental", {})
        config.setdefault("worktree", {})
        config["worktree"].setdefault("path_template", None)
        config["worktree"].setdefault("enabled", True)
        # Migrate legacy vim key to vi-mode
        if "vim" in config:
            config["vi-mode"] = config.pop("vim")
            _save(config)
    else:
        # New install - create config with fresh ID and save
        config = {
            "analytics": {"enabled": True, "id": str(uuid.uuid4())},
            "recent-tools-expanded": 2,
        }
        new_install = True
        _save(config)

    return config, new_install


def _save(config: dict) -> None:
    """Write config to disk atomically."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=CONFIG_PATH.parent, suffix=".yaml")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False)
        os.replace(tmp_path, CONFIG_PATH)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# Load config once at import time
CONFIG, NEW_INSTALL = _load()


def save() -> None:
    """Save current CONFIG to disk."""
    _save(CONFIG)
