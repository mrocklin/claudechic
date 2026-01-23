"""Command handlers for slash commands.

This module extracts command routing from app.py. Commands receive an app
reference and access only what they need.

The COMMANDS registry is the single source of truth for all slash commands.
It's used by autocomplete (app.py) and help (help_data.py).
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claudechic.app import ChatApp

# Command registry: (name, description, [variants for autocomplete])
# Variants are additional completions like "/agent close" for "/agent"
COMMANDS: list[tuple[str, str, list[str]]] = [
    ("/clear", "Clear chat and start new session", []),
    ("/diff", "Review uncommitted changes", []),
    ("/resume", "Resume a previous session", []),
    (
        "/worktree",
        "Create git worktree with agent",
        ["/worktree finish", "/worktree cleanup"],
    ),
    ("/agent", "Create or list agents", ["/agent close"]),
    ("/shell", "Run shell command (or -i for interactive)", []),
    ("/theme", "Search themes", []),
    ("/compactish", "Compact session to reduce context", []),
    ("/usage", "Show API rate limit usage", []),
    ("/model", "Change model", []),
    ("/processes", "Show background processes", []),
    (
        "/analytics",
        "Analytics settings (opt-in/opt-out)",
        ["/analytics opt-in", "/analytics opt-out"],
    ),
    ("/welcome", "Show welcome message", []),
    ("/help", "Show help", []),
    ("/exit", "Quit", []),
    ("!<cmd>", "Shell command alias", []),
]


def get_autocomplete_commands() -> list[str]:
    """Get flat list of commands for autocomplete (includes variants)."""
    result = []
    for name, _, variants in COMMANDS:
        if not name.startswith("!"):  # Skip ! alias, not useful in autocomplete
            result.append(name)
            result.extend(variants)
    return result


def get_help_commands() -> list[tuple[str, str]]:
    """Get (command, description) pairs for help display."""
    # For help, show base command with [args] notation
    result = []
    for name, desc, _ in COMMANDS:
        # Add [args] hints for commands that take arguments
        display_name = name
        if name == "/resume":
            display_name = "/resume [id]"
        elif name == "/agent":
            display_name = "/agent [name] [path]"
        elif name == "/shell":
            display_name = "/shell <cmd>"
        elif name == "/compactish":
            display_name = "/compactish [-n]"
        elif name == "/worktree":
            display_name = "/worktree <name>"
        result.append((display_name, desc))
    return result


def handle_command(app: "ChatApp", prompt: str) -> bool:
    """Route slash commands. Returns True if handled, False to send to Claude."""
    cmd = prompt.strip()

    # Handle ! prefix for inline shell commands
    if cmd.startswith("!"):
        return _handle_bang(app, cmd[1:].strip())

    if cmd == "/clear":
        app._start_new_session()
        return True

    if cmd.startswith("/resume"):
        return _handle_resume(app, cmd)

    if cmd.startswith("/worktree"):
        from claudechic.features.worktree import handle_worktree_command

        handle_worktree_command(app, cmd)
        return True

    if cmd.startswith("/agent"):
        return _handle_agent(app, cmd)

    if cmd.startswith("/shell"):
        return _handle_shell(app, cmd)

    if cmd == "/theme":
        app.search_themes()
        return True

    if cmd.startswith("/compactish"):
        return _handle_compactish(app, cmd)

    if cmd == "/usage":
        app._handle_usage_command()
        return True

    if cmd == "/model" or cmd.startswith("/model "):
        parts = cmd.split(maxsplit=1)
        if len(parts) == 1:
            # No argument - show prompt
            app._handle_model_prompt()
        else:
            # Direct model selection: /model sonnet
            model = parts[1].lower()
            valid_models = {"opus", "sonnet", "haiku"}
            if model not in valid_models:
                app.notify(
                    f"Invalid model '{model}'. Use: opus, sonnet, haiku",
                    severity="error",
                )
            else:
                app._set_agent_model(model)
        return True

    if cmd == "/exit":
        app.exit()
        return True

    if cmd == "/welcome":
        return _handle_welcome(app)

    if cmd == "/help":
        app.run_worker(_handle_help(app))
        return True

    if cmd == "/processes":
        _handle_processes(app)
        return True

    if cmd.startswith("/analytics"):
        return _handle_analytics(app, cmd)

    if cmd == "/diff" or cmd == "/d":
        app._toggle_diff_mode()
        return True

    return False


def _handle_resume(app: "ChatApp", command: str) -> bool:
    """Handle /resume [session_id] command."""
    parts = command.split(maxsplit=1)
    if len(parts) > 1:
        session_id = parts[1]
        app.run_worker(app._load_and_display_history(session_id))
        app.notify(f"Resuming {session_id[:8]}...")
        app.resume_session(session_id)
    else:
        app._show_session_picker()
    return True


def _handle_agent(app: "ChatApp", command: str) -> bool:
    """Handle /agent commands: list, create, close."""
    from claudechic.widgets import ChatMessage

    parts = command.split(maxsplit=2)

    if len(parts) == 1:
        # In narrow mode, open the sidebar overlay instead of listing
        width = app.size.width
        has_content = (
            len(app.agents) > 1 or app.agent_section._worktrees or app.todo_panel.todos
        )
        if width < app.SIDEBAR_MIN_WIDTH and has_content:
            app._sidebar_overlay_open = True
            app._position_right_sidebar()
            return True

        # List agents as markdown table
        lines = [
            "| # | Agent | Status | Directory |",
            "|---|-------|--------|-----------|",
        ]
        for i, (aid, agent) in enumerate(app.agents.items(), 1):
            marker = "▸" if aid == app.active_agent_id else " "
            # Shorten home directory
            path = str(agent.cwd).replace(str(Path.home()), "~")
            lines.append(f"| {marker}{i} | {agent.name} | {agent.status} | {path} |")

        chat_view = app._chat_view
        if chat_view:
            msg = ChatMessage("\n".join(lines))
            msg.add_class("system-message")
            chat_view.mount(msg)
            chat_view.scroll_if_tailing()
        return True

    subcommand = parts[1]
    if subcommand == "close":
        target = parts[2] if len(parts) > 2 else None
        app._close_agent(target)
        return True

    # Check if agent with this name exists - switch to it
    name = subcommand
    existing = app.agent_mgr.find_by_name(name) if app.agent_mgr else None
    if existing:
        app._switch_to_agent(existing.id)
        return True

    # Create new agent - parse optional --model flag (supports --model=x or --model x)
    cwd: Path | None = None
    model = None
    valid_models = {"opus", "sonnet", "haiku"}
    args = parts[2:]
    i = 0
    while i < len(args):
        part = args[i]
        if part.startswith("--model="):
            model = part[8:].lower()
        elif part == "--model" and i + 1 < len(args):
            model = args[i + 1].lower()
            i += 1
        elif not part.startswith("-") and cwd is None:
            cwd = Path(part)
        i += 1
    if model and model not in valid_models:
        app.notify(
            f"Invalid model '{model}'. Use: opus, sonnet, haiku", severity="error"
        )
        return True
    # Default to current agent's cwd, fallback to app's cwd
    default_cwd = app._agent.cwd if app._agent else Path.cwd()
    app._create_new_agent(name, cwd or default_cwd, model=model)
    return True


def _handle_shell(app: "ChatApp", command: str) -> bool:
    """Run shell command inline, or interactive shell if no command or -i flag.

    NOTE: On Windows, only interactive mode is supported (no PTY capture).
    """
    import sys

    parts = command.split(maxsplit=1)
    cmd = parts[1] if len(parts) > 1 else None

    # Check for -i flag (interactive mode)
    interactive = False
    if cmd and cmd.startswith("-i "):
        interactive = True
        cmd = cmd[3:].lstrip()

    # Windows doesn't have PTY support for captured output - force interactive mode
    is_windows = sys.platform == "win32"
    if is_windows and cmd and not interactive:
        app.notify(
            "Captured shell output not supported on Windows. Running interactively.",
            severity="warning",
        )
        interactive = True

    agent = app._agent
    cwd = str(agent.cwd) if agent else None
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}

    # Platform-specific shell and args
    if is_windows:
        # Windows: use cmd.exe or PowerShell
        shell = os.environ.get("COMSPEC", "cmd.exe")
        if cmd:
            args = [shell, "/c", cmd]
        else:
            args = [shell]
    else:
        # Unix: use SHELL env var or fallback to /bin/sh
        # Force color output, disable pagers for captured output
        env.update(
            {
                "FORCE_COLOR": "1",
                "CLICOLOR_FORCE": "1",
                "TERM": "xterm-256color",
                "BAT_PAGER": "",
                "PAGER": "",
            }
        )
        shell = os.environ.get("SHELL", "/bin/sh")
        if cmd:
            args = [shell, "-lc", cmd] if not interactive else [shell, "-c", cmd]
        else:
            args = [shell, "-l"]

    if cmd and not interactive:
        # Async execution with captured output (Unix only)
        app.run_shell_command(cmd, shell, cwd, env)
    else:
        # Interactive: suspend TUI and run in real terminal
        try:
            with app.suspend():
                start = time.monotonic()
                subprocess.run(args, cwd=cwd, env=env)
                # If command was fast, wait for keypress so user can see output
                if cmd and time.monotonic() - start < 1.0:
                    _wait_for_keypress()
        except Exception as e:
            # SuspendNotSupported or other errors (e.g., in test environments)
            app.notify(
                f"Shell suspend not supported in this environment: {e}",
                severity="error",
            )

    return True


def _wait_for_keypress() -> None:
    """Wait for a keypress. Cross-platform (Unix uses termios, Windows uses input)."""
    import sys

    if sys.platform == "win32":
        input("\nPress Enter to continue...")
    else:
        import termios
        import tty

        print("\nPress any key to continue...", end="", flush=True)
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            sys.stdin.read(1)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _handle_bang(app: "ChatApp", command: str) -> bool:
    """Alias for /shell <command>. Empty command opens interactive shell."""
    if not command:
        return _handle_shell(app, "/shell")
    return _handle_shell(app, f"/shell {command}")


def _handle_welcome(app: "ChatApp") -> bool:
    """Send welcome message to Claude to present to user."""
    welcome_prompt = """\
Welcome the user to Claude Chic, the Claude Code app that they're using. Present this information to them exactly:

---

# Welcome to Claude Chic 👋

Claude Chic is a drop-in replacement for the `claude` terminal UI that adds the following:

## Visual Design

Information is organized to be easy to scan and explore.

-  Tool outputs are collapsible
-  Border colors indicates user (orange), assistant (blue), and tools (gray) messages
-  Context and CPU indicators are in the footer
-  Maximum width makes scanning easier on the eyes

**Example**: Resume (`/resume`) a long Claude session to see the differnce side-by-side.

## Multi-Agent

Run multiple Claude agents concurrently in the same UI.

-  Create agents with `/agent <name>` (or ask me to make them).
-  Enable parallel workstreams without lots of terminals
-  Agents able to communicate with each other

**Example:** Ask me to set up a game of chess between two agents.

## Git Worktrees

Create isolated git worktrees for parallel feature development.

-  Launch multiple improvements simultaneously without branch conflicts
-  Use `/worktree branch-nanme` to create a worktree
-  Use `/worktree finish` to merge it back into main with linear history

**Example:** Run `claudechic` in your repo, and ask me to do a thorough review, launching different workstreams for each issue.

## Hackable

Claude Chic is open source and written in Python with Textual.  It's easy to extend with fun tools.

**Example:** Use simple quality of life features like shell support with `!ls`. or `!git diff`.

For more information, read
[the docs](https://matthewrocklin.com/claudechic),
[GitHub](https://github.com/mrocklin/claudechic),
or this [introductory video](https://www.youtube.com/watch?v=2HcORToX5sU).

Enjoy!

---

Repeat this message verbatim.  Help them if they have further questions.
"""

    app._send_to_active_agent(welcome_prompt, display_as="/welcome")
    return True


def _handle_compactish(app: "ChatApp", command: str) -> bool:
    """Handle /compactish command - compact the current session.

    Flags:
        -n, --dry: Show stats without modifying
        -a, --aggressive: Use lower size thresholds
        --no-reconnect: Don't reconnect after compaction
    """
    from claudechic.compact import compact_session, format_compact_summary
    from claudechic.widgets import ChatMessage

    agent = app._agent
    if not agent or not agent.session_id:
        app.notify("No active session to compact", severity="warning")
        return True

    session_id = agent.session_id
    parts = command.split()

    # Parse flags
    dry_run = "--dry" in parts or "-n" in parts
    aggressive = "--aggressive" in parts or "-a" in parts
    reconnect = "--no-reconnect" not in parts

    result = compact_session(
        session_id, cwd=agent.cwd, aggressive=aggressive, dry_run=dry_run
    )
    if "error" in result:
        app.notify(f"Error: {result['error']}", severity="error")
        return True

    # Display summary table
    summary_md = format_compact_summary(result, dry_run=dry_run)
    chat_view = app._chat_view
    if chat_view:
        summary_msg = ChatMessage(summary_md)
        summary_msg.add_class("system-message")
        chat_view.mount(summary_msg)
        chat_view.scroll_if_tailing()

    if dry_run:
        app.notify("Dry run - no changes made", timeout=3)
    elif reconnect:
        app.run_worker(app._reconnect_agent(agent, session_id))
        app.notify("Session compacted, reconnecting...", timeout=3)
    else:
        app.notify("Session compacted", timeout=3)

    return True


async def _handle_help(app: "ChatApp") -> None:
    """Display help information."""
    from claudechic.help_data import format_help
    from claudechic.widgets import ChatMessage

    agent = app._agent
    help_text = await format_help(agent)

    chat_view = app._chat_view
    if chat_view:
        msg = ChatMessage(help_text)
        msg.add_class("system-message")
        chat_view.mount(msg)
        chat_view.scroll_if_tailing()


def _handle_processes(app: "ChatApp") -> None:
    """Show process modal with current background processes."""
    from claudechic.widgets.modals.process_modal import ProcessModal

    agent = app._agent
    if agent:
        processes = agent.get_background_processes()
    else:
        processes = []
    app.push_screen(ProcessModal(processes))


def _handle_analytics(app: "ChatApp", command: str) -> bool:
    """Handle /analytics commands: opt-in, opt-out."""
    from claudechic.config import (
        get_analytics_enabled,
        get_analytics_id,
        set_analytics_enabled,
    )

    parts = command.split()
    subcommand = parts[1] if len(parts) > 1 else ""

    if subcommand == "opt-in":
        set_analytics_enabled(True)
        app.notify("Analytics enabled")
        return True

    if subcommand == "opt-out":
        set_analytics_enabled(False)
        app.notify("Analytics disabled")
        return True

    # Show current status
    enabled = get_analytics_enabled()
    user_id = get_analytics_id()
    status = "enabled" if enabled else "disabled"
    app.notify(f"Analytics {status}, ID: {user_id[:8]}...")
    return True
