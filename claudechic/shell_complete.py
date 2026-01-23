"""Shell completion utilities - command and path completion."""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Cached executables from PATH
_executable_cache: list[str] | None = None

# Windows executable extensions (from PATHEXT env var)
_WINDOWS_EXE_EXTS = {".exe", ".cmd", ".bat", ".com", ".ps1"}


def _is_executable(entry: Path) -> bool:
    """Check if a file is executable (cross-platform)."""
    if not entry.is_file():
        return False

    if sys.platform == "win32":
        # Windows: check file extension against PATHEXT
        pathext = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD").lower()
        valid_exts = {ext.strip() for ext in pathext.split(";")} | _WINDOWS_EXE_EXTS
        return entry.suffix.lower() in valid_exts
    else:
        # Unix: use execute permission bit
        return os.access(entry, os.X_OK)


def get_executables() -> list[str]:
    """Get all executable commands from PATH (cached)."""
    global _executable_cache
    if _executable_cache is not None:
        return _executable_cache

    executables = set()
    path_dirs = os.environ.get("PATH", "").split(os.pathsep)

    for dir_path in path_dirs:
        p = Path(dir_path)
        if not p.is_dir():
            continue
        try:
            for entry in p.iterdir():
                if _is_executable(entry):
                    # On Windows, add both with and without extension for common exts
                    name = entry.name
                    executables.add(name)
                    if (
                        sys.platform == "win32"
                        and entry.suffix.lower() in _WINDOWS_EXE_EXTS
                    ):
                        executables.add(entry.stem)  # Add without extension too
        except (PermissionError, OSError):
            continue

    _executable_cache = sorted(executables)
    return _executable_cache


def complete_command(prefix: str, limit: int = 20) -> list[str]:
    """Complete a command name prefix."""
    prefix_lower = prefix.lower()
    executables = get_executables()

    # Prioritize exact prefix matches, then contains
    exact = [e for e in executables if e.lower().startswith(prefix_lower)]
    return exact[:limit]


def _is_absolute_path(partial: str) -> bool:
    """Check if a partial path is absolute (cross-platform)."""
    if partial.startswith("/"):
        return True
    # Windows drive letter (e.g., C:, D:\)
    if len(partial) >= 2 and partial[1] == ":" and partial[0].isalpha():
        return True
    return False


def _split_path(partial: str) -> tuple[str, str]:
    """Split a path into (directory, filename) handling both / and \\ separators."""
    # Normalize to find the last separator
    for sep in ("/", "\\"):
        if sep in partial:
            idx = max(partial.rfind("/"), partial.rfind("\\"))
            return partial[: idx + 1], partial[idx + 1 :]
    return "", partial


def _ends_with_separator(partial: str) -> bool:
    """Check if path ends with a directory separator."""
    return partial.endswith("/") or partial.endswith("\\")


def _get_separator() -> str:
    """Get the preferred path separator for completions."""
    # Use forward slash on all platforms for consistency (works on Windows too)
    return "/"


def complete_path(partial: str, cwd: Path | None = None, limit: int = 20) -> list[str]:
    """Complete a partial file/directory path."""
    cwd = cwd or Path.cwd()
    sep = _get_separator()

    if not partial:
        base = cwd
        prefix = ""
        match_part = ""
    elif _is_absolute_path(partial):
        # Absolute path (Unix / or Windows C:\)
        p = Path(partial)
        if p.is_dir() and _ends_with_separator(partial):
            base = p
            prefix = partial.rstrip("/\\") + sep
            match_part = ""
        else:
            base = (
                p.parent if p.parent.exists() else (Path(p.anchor) if p.anchor else cwd)
            )
            prefix = str(base).rstrip("/\\") + sep
            match_part = p.name
    elif partial.startswith("~"):
        # Home directory
        expanded = Path(partial).expanduser()
        if expanded.is_dir() and _ends_with_separator(partial):
            base = expanded
            prefix = partial.rstrip("/\\") + sep
            match_part = ""
        else:
            base = expanded.parent if expanded.parent.exists() else Path.home()
            # Keep the ~ prefix in output
            dir_part, _ = _split_path(partial)
            prefix = dir_part if dir_part else "~" + sep
            match_part = expanded.name
    else:
        # Relative path
        p = cwd / partial
        if p.is_dir() and _ends_with_separator(partial):
            base = p
            prefix = partial.rstrip("/\\") + sep
            match_part = ""
        else:
            base = p.parent if p.parent.exists() else cwd
            dir_part, _ = _split_path(partial)
            prefix = dir_part
            match_part = p.name

    results = []
    match_lower = match_part.lower()

    try:
        for entry in base.iterdir():
            name = entry.name
            # Skip hidden unless explicitly requested
            if name.startswith(".") and not match_part.startswith("."):
                continue
            if match_part and not name.lower().startswith(match_lower):
                continue
            suffix = sep if entry.is_dir() else ""
            results.append(prefix + name + suffix)
    except (PermissionError, OSError):
        pass

    return sorted(results)[:limit]


def parse_shell_input(text: str) -> tuple[str, str]:
    """Parse shell input into (command, current_arg).

    Returns:
        (command, current_arg) where current_arg is what's being typed
    """
    # Strip leading ! or "/shell "
    if text.startswith("!"):
        text = text[1:]
    elif text.startswith("/shell "):
        text = text[7:]
    else:
        return "", text

    # Split by whitespace, keeping track of what's being typed
    parts = text.split()

    if not parts:
        return "", ""

    if text.endswith(" "):
        # Space after last word - completing new argument
        return parts[0], ""
    elif len(parts) == 1:
        # Still typing the command
        return "", parts[0]
    else:
        # Typing an argument
        return parts[0], parts[-1]
