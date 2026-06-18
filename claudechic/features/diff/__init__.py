"""Git diff view feature."""

from .git import (
    FileChange,
    FileStat,
    Hunk,
    HunkComment,
    format_hunk_comments,
    get_base_branch,
    get_changes,
    get_file_stats,
)
from .widgets import DiffSidebar, DiffView, EditFileRequested, FileDiffPanel, HunkWidget

__all__ = [
    "FileChange",
    "FileStat",
    "Hunk",
    "HunkComment",
    "format_hunk_comments",
    "get_base_branch",
    "get_changes",
    "get_file_stats",
    "DiffSidebar",
    "DiffView",
    "EditFileRequested",
    "FileDiffPanel",
    "HunkWidget",
]
