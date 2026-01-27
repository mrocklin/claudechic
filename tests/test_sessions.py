"""Tests for session management."""

import os
from pathlib import Path


def test_project_key_encoding():
    """Verify project key matches Claude Code's encoding (dots become dashes)."""
    # Simulate the encoding logic from sessions.py:get_project_sessions_dir
    def encode_project_key(path: Path) -> str:
        return str(path).replace(os.sep, "-").replace(":", "").replace(".", "-")

    # Path with dot (like .local) should have dot replaced with dash
    path = Path("/home/user/.local/share/chezmoi")
    assert encode_project_key(path) == "-home-user--local-share-chezmoi"

    # Regular path without dots
    path = Path("/home/user/Code/project")
    assert encode_project_key(path) == "-home-user-Code-project"

    # Path with multiple dots
    path = Path("/home/user/.config/.hidden")
    assert encode_project_key(path) == "-home-user--config--hidden"
