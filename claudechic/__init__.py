"""Claude Chic - A stylish terminal UI for Claude Code."""

from importlib.metadata import version

# Apply Textual monkey-patches before any MarkdownFence is constructed.
from claudechic._textual_patches import apply_patches as _apply_textual_patches

_apply_textual_patches()

from claudechic.app import ChatApp  # noqa: E402 - must follow apply_patches
from claudechic.theme import CHIC_THEME  # noqa: E402
from claudechic.protocols import (  # noqa: E402
    AgentManagerObserver,
    AgentObserver,
    PermissionHandler,
)

__all__ = [
    "ChatApp",
    "CHIC_THEME",
    "AgentManagerObserver",
    "AgentObserver",
    "PermissionHandler",
]
__version__ = version("claudechic")
