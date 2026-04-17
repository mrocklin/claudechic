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
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from claudechic.analytics import capture

if TYPE_CHECKING:
    from claudechic.app import ChatApp

# Commands that should always run in interactive mode (TUI editors, pagers, etc.)
INTERACTIVE_COMMANDS = frozenset(
    {
        "nvim",
        "vim",
        "vi",
        "nano",
        "emacs",
        "pico",
        "joe",
        "micro",  # editors
        "less",
        "more",
        "most",  # pagers
        "htop",
        "top",
        "btop",
        "glances",  # monitors
        "tmux",
        "screen",  # terminal multiplexers
        "mc",
        "ranger",
        "nnn",
        "lf",  # file managers
        "python",
        "python3",
        "ipython",
        "bpython",  # REPLs
        "node",
        "irb",
        "ghci",
        "lua",  # more REPLs
        "psql",
        "mysql",
        "sqlite3",  # database CLIs
        "ssh",
        "telnet",  # remote shells
    }
)

# Two-word commands that use a pager by default
INTERACTIVE_SUBCOMMANDS = frozenset(
    {
        "git diff",
        "git log",
        "git show",
        "git blame",
    }
)

# Bare words mapped to their equivalent slash commands
BARE_WORDS: dict[str, str] = {
    "quit": "/exit",
    "exit": "/exit",
}

# Command registry: (name, description, [variants for autocomplete])
# Variants are additional completions like "/agent close" for "/agent"
COMMANDS: list[tuple[str, str, list[str]]] = [
    ("/clear", "Clear chat and start new session", []),
    ("/diff", "Review changes vs target (default HEAD)", []),
    ("/resume", "Resume a previous session", []),
    (
        "/worktree",
        "Create git worktree with agent",
        ["/worktree finish", "/worktree cleanup", "/worktree discard"],
    ),
    ("/agent", "Create or list agents", ["/agent close"]),
    ("/shell", "Run shell command (or -i for interactive)", []),
    ("/theme", "Search themes", []),
    ("/compactish", "Compact session to reduce context", []),
    ("/usage", "Show API rate limit usage", []),
    ("/model", "Change model", []),
    ("/effort", "Set thinking effort level", []),
    ("/vim", "Toggle vi mode for input", []),
    ("/processes", "Show background processes", []),
    ("/reviews", "Show roborev reviews", []),
    (
        "/analytics",
        "Analytics settings (opt-in/opt-out)",
        ["/analytics opt-in", "/analytics opt-out"],
    ),
    ("/welcome", "Show welcome message", []),
    ("/reviewer", "Spawn a review agent for current changes", []),
    ("/plan-swarm", "Start swarm planning with multiple approaches", []),
    ("/rewind", "Rewind to a previous checkpoint", []),
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
        elif name == "/reviews":
            display_name = "/reviews [job_id]"
        elif name == "/reviewer":
            display_name = "/reviewer [focus]"
        elif name == "/plan-swarm":
            display_name = "/plan-swarm"
        elif name == "/rewind":
            display_name = "/rewind [index]"
        elif name == "/effort":
            display_name = "/effort [low|medium|high|xhigh|max]"
        result.append((display_name, desc))
    return result


def _track_command(app: "ChatApp", command: str) -> None:
    """Track command usage for analytics."""
    agent = app._agent
    app.run_worker(
        capture(
            "command_used",
            command=command,
            agent_id=agent.analytics_id if agent else "unknown",
        )
    )


def handle_command(app: "ChatApp", prompt: str) -> bool:
    """Route slash commands. Returns True if handled, False to send to Claude."""
    cmd = prompt.strip()

    # Map bare words to their slash command equivalents
    cmd = BARE_WORDS.get(cmd, cmd)

    # Handle ! prefix for inline shell commands
    if cmd.startswith("!"):
        _track_command(app, "shell")
        return _handle_bang(app, cmd[1:].strip())

    if cmd == "/clear":
        _track_command(app, "clear")
        app._start_new_session()
        return True

    if cmd.startswith("/resume"):
        _track_command(app, "resume")
        return _handle_resume(app, cmd)

    if cmd.startswith("/worktree"):
        from claudechic.features.worktree import handle_worktree_command

        # worktree_action event is tracked separately with more detail
        handle_worktree_command(app, cmd)
        return True

    if cmd.startswith("/agent"):
        _track_command(app, "agent")
        return _handle_agent(app, cmd)

    if cmd.startswith("/shell"):
        _track_command(app, "shell")
        return _handle_shell(app, cmd)

    if cmd == "/theme":
        _track_command(app, "theme")
        app.search_themes()
        return True

    if cmd.startswith("/compactish"):
        _track_command(app, "compactish")
        return _handle_compactish(app, cmd)

    if cmd == "/usage":
        _track_command(app, "usage")
        app._handle_usage_command()
        return True

    if cmd == "/model" or cmd.startswith("/model "):
        _track_command(app, "model")
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

    if cmd == "/effort" or cmd.startswith("/effort "):
        _track_command(app, "effort")
        parts = cmd.split(maxsplit=1)
        if len(parts) == 1:
            # No argument - show current effort
            agent = app._agent
            current = agent.effort if agent else None
            app.notify(f"Current effort: {current or 'default'}")
        else:
            effort = parts[1].lower()
            valid_efforts = {"low", "medium", "high", "xhigh", "max"}
            if effort not in valid_efforts:
                app.notify(
                    f"Invalid effort '{effort}'. Use: low, medium, high, xhigh, max",
                    severity="error",
                )
            else:
                app._set_agent_effort(effort)
        return True

    if cmd == "/exit":
        _track_command(app, "exit")
        app.exit()
        return True

    if cmd == "/vim":
        _track_command(app, "vim")
        return _handle_vim(app)

    if cmd == "/welcome":
        _track_command(app, "welcome")
        return _handle_welcome(app)

    if cmd == "/reviewer" or cmd.startswith("/reviewer "):
        _track_command(app, "reviewer")
        context = cmd.split(maxsplit=1)[1] if cmd.startswith("/reviewer ") else None
        return _handle_review(app, context)

    if cmd == "/plan-swarm":
        _track_command(app, "plan-swarm")
        return _handle_plan_swarm(app)

    if cmd == "/help":
        _track_command(app, "help")
        app.run_worker(_handle_help(app))
        return True

    if cmd == "/reviews" or cmd.startswith("/reviews "):
        _track_command(app, "reviews")
        parts = cmd.split(maxsplit=1)
        job_id = parts[1] if len(parts) > 1 else None
        _handle_reviews(app, job_id)
        return True

    if cmd == "/processes":
        _track_command(app, "processes")
        _handle_processes(app)
        return True

    if cmd.startswith("/analytics"):
        _track_command(app, "analytics")
        return _handle_analytics(app, cmd)

    if cmd == "/diff" or cmd == "/d" or cmd.startswith("/diff "):
        _track_command(app, "diff")
        target = cmd.split(maxsplit=1)[1] if cmd.startswith("/diff ") else None
        app._toggle_diff_mode(target)
        return True

    if cmd == "/rewind" or cmd.startswith("/rewind "):
        _track_command(app, "rewind")
        return _handle_rewind(app, cmd)

    # Unknown slash command - pass through to Claude (may be SDK command or skill)
    cmd_name = cmd.split()[0]
    if cmd_name in CLAUDE_CLI_COMMANDS:
        app.notify(
            f"'{cmd_name}' is not available in claudechic.\nUse 'claude' CLI instead.",
            severity="warning",
            timeout=5,
        )
        return True

    # Check if it's a built-in SDK command (works, no tracking needed)
    if cmd_name in SDK_PASSTHROUGH_COMMANDS:
        return False

    # Check if it's a user-defined command (won't trigger Skill tool)
    agent = app._agent
    cwd = agent.cwd if agent else Path.cwd()
    if _is_user_command(cmd_name, cwd):
        # User command - SDK will inject it, no need to track
        return False

    # Track for typo detection (cleared if Skill tool is invoked)
    if agent:
        app._pending_slash_commands[agent.id] = cmd_name

    return False


def _is_user_command(cmd_name: str, cwd: Path) -> bool:
    """Check if cmd_name is a user-defined command or skill.

    Commands: ~/.claude/commands/<name>.md or .claude/commands/<name>.md
    Skills: ~/.claude/skills/<name>/SKILL.md or .claude/skills/<name>/SKILL.md

    Skill slash commands use colon notation (e.g. /roborev:fix) but the
    directories on disk use hyphens (e.g. roborev-fix/), so we check both.
    """
    name = cmd_name.lstrip("/")
    home = Path.home()

    # Skill directories typically use hyphens on disk, but colons in slash commands
    # e.g. /roborev:fix -> ~/.claude/skills/roborev-fix/SKILL.md
    # Check both forms for backward compatibility with any colon-named directories.
    skill_names = {name}
    if ":" in name:
        skill_names.add(name.replace(":", "-"))

    paths = [
        home / ".claude" / "commands" / f"{name}.md",  # global command
        cwd / ".claude" / "commands" / f"{name}.md",  # project command
    ]
    for sname in skill_names:
        paths.append(home / ".claude" / "skills" / sname / "SKILL.md")  # global skill
        paths.append(cwd / ".claude" / "skills" / sname / "SKILL.md")  # project skill
    return any(p.exists() for p in paths)


# Commands that exist in Claude Code CLI but not in claudechic
CLAUDE_CLI_COMMANDS = frozenset(
    {
        "/mcp",
        "/plugins",
        "/login",
        "/logout",
        "/config",
        "/permissions",
        "/memory",
        "/doctor",
        "/cost",
        "/terminal-setup",
    }
)

# Built-in SDK commands that work in claudechic (pass through, no tracking)
SDK_PASSTHROUGH_COMMANDS = frozenset(
    {
        "/compact",
        "/context",
        "/init",
    }
)


def _handle_resume(app: "ChatApp", command: str) -> bool:
    """Handle /resume [session_id_or_prefix] command."""
    from claudechic.sessions import find_session_by_prefix

    parts = command.split(maxsplit=1)
    if len(parts) > 1:
        prefix = parts[1]
        agent = app._agent
        cwd = agent.cwd if agent else None
        session_id = find_session_by_prefix(prefix, cwd)
        if not session_id:
            app.notify(f"No unique session matching '{prefix}'", severity="error")
            return True
        app.run_worker(app._load_and_display_history(session_id))
        app.notify(f"Resuming {session_id[:8]}...")
        app.resume_session(session_id)
    else:
        app._show_session_picker()
    return True


def _handle_rewind(app: "ChatApp", command: str) -> bool:
    """Handle /rewind [checkpoint_index] command.

    Without argument: shows checkpoint picker UI
    With index: directly rewinds to that checkpoint (prompts for type)
    """
    parts = command.split(maxsplit=1)

    if len(parts) > 1:
        # Direct rewind: /rewind 3
        try:
            index = int(parts[1])
            app._rewind_to_checkpoint_direct(index)
        except ValueError:
            app.notify(f"Invalid checkpoint index: {parts[1]}", severity="error")
    else:
        # Show picker
        app._show_rewind_picker()
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
    if app.agent_mgr:
        existing = app.agent_mgr.find_by_name(name)
        if existing:
            app.agent_mgr.switch(existing.id)
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

    # Auto-detect interactive commands from whitelist
    if cmd and not interactive:
        first_word = cmd.split()[0].split("/")[-1]  # Handle paths like /usr/bin/vim
        if first_word in INTERACTIVE_COMMANDS:
            interactive = True
        # Check two-word subcommands (e.g., "git diff", "git log")
        elif len(cmd.split()) >= 2:
            two_words = " ".join(cmd.split()[:2])
            if two_words in INTERACTIVE_SUBCOMMANDS:
                interactive = True

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
        # Force color output
        env.update(
            {
                "FORCE_COLOR": "1",
                "CLICOLOR_FORCE": "1",
                "TERM": "xterm-256color",
            }
        )
        # Disable pagers only for captured (non-interactive) output
        if not interactive:
            env.update({"BAT_PAGER": "", "PAGER": ""})
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
Welcome the user to Claude Chic. Present this message exactly:

---

# Welcome to Claude Chic 👋

A stylish Claude Code UI with multi-agent superpowers.

**What's different:**

- **Style:** Collapsible tool outputs, color-coded messages, context/CPU in footer
- **Multi-agent:** `/agent <name>` or ask me to spawn agents
- **Git worktrees:** `/worktree <branch>` for parallel feature development
- **Shell shortcuts:** `!ls`, `!git diff`

**Try it:** Ask me to review your codebase, or run `/resume` to revisit a past session.

Links: [Docs](https://matthewrocklin.com/claudechic) · [GitHub](https://github.com/mrocklin/claudechic) · [Video](https://www.youtube.com/watch?v=2HcORToX5sU)

---

Repeat this message verbatim. Help them if they have questions.
"""

    app._send_to_active_agent(welcome_prompt, display_as="/welcome")
    return True


def _handle_review(app: "ChatApp", context: str | None) -> bool:
    """Inject review skill instructions into current agent."""
    agent = app._agent
    if not agent:
        app.notify("No active agent", severity="error")
        return True

    # Load skill from markdown file
    skill_path = Path(__file__).parent / "prompts" / "reviewer.md"
    try:
        instructions = skill_path.read_text()
    except FileNotFoundError:
        app.notify(f"Skill file not found: {skill_path}", severity="error")
        return True

    instructions = instructions.replace("{agent_name}", agent.name)

    if context:
        instructions += f"\nUser-provided focus: {context}\n"

    # Send to current agent
    app._send_to_active_agent(
        instructions, display_as="/reviewer" + (f" {context}" if context else "")
    )
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


def _handle_reviews(app: "ChatApp", job_id: str | None) -> None:
    """Show roborev reviews: list all or show detail for a specific job."""

    agent = app._agent
    if not agent:
        app.notify("No active agent", severity="error")
        return

    if job_id:
        # Show detail for specific job
        app.run_worker(_show_review_detail(app, job_id))
    else:
        # List all reviews for current branch
        app.run_worker(_list_reviews_in_chat(app))


_VERDICT_MAP = {"p": "P", "pass": "P", "f": "F", "fail": "F"}


def _format_verdict(verdict: object) -> str:
    """Normalize a review verdict to a short display string (P/F/…)."""
    return _VERDICT_MAP.get(str(verdict or "").lower(), "…")


async def _list_reviews_in_chat(app: "ChatApp") -> None:
    """List reviews as a markdown table in the chat."""
    import asyncio

    from claudechic.features.roborev import get_current_branch, list_reviews
    from claudechic.widgets import ChatMessage

    agent = app._agent
    if not agent:
        return

    cwd = agent.cwd
    branch = await asyncio.to_thread(get_current_branch, cwd)
    reviews = await asyncio.to_thread(list_reviews, cwd, branch)

    chat_view = app._chat_view
    if not chat_view:
        return

    if not reviews:
        msg = ChatMessage(
            "No roborev reviews found"
            + (f" for branch `{branch}`" if branch else "")
            + "."
        )
        msg.add_class("system-message")
        chat_view.mount(msg)
        chat_view.scroll_if_tailing()
        return

    lines = [
        f"**Reviews** ({branch or 'all branches'})\n",
        "| Job | Verdict | SHA | Subject | Agent | Status |",
        "|-----|---------|-----|---------|-------|--------|",
    ]

    def _esc(s: object) -> str:
        """Escape pipes and normalize newlines for markdown table cells."""
        text = "" if s is None else str(s)
        return text.replace("|", r"\|").replace("\n", " ")

    for r in reviews:
        verdict = _format_verdict(r.verdict)
        sha = r.git_ref[:7] if r.git_ref else ""
        subject = r.commit_subject[:30] + ("…" if len(r.commit_subject) > 30 else "")
        lines.append(
            f"| {_esc(r.id)} | {verdict} | `{sha}` | {_esc(subject)} | {_esc(r.agent)} | {_esc(r.status)} |"
        )
    lines.append("\nUse `/reviews <job_id>` to see detail.")

    msg = ChatMessage("\n".join(lines))
    msg.add_class("system-message")
    chat_view.mount(msg)
    chat_view.scroll_if_tailing()


async def _show_review_detail(app: "ChatApp", job_id: str) -> None:
    """Show detail for a specific review job."""
    import asyncio

    from claudechic.features.roborev import show_review
    from claudechic.widgets import ChatMessage

    agent = app._agent
    if not agent:
        return

    detail = await asyncio.to_thread(show_review, job_id, agent.cwd)

    chat_view = app._chat_view
    if not chat_view:
        return

    if not detail:
        msg = ChatMessage(f"Review `{job_id}` not found.")
        msg.add_class("system-message")
        chat_view.mount(msg)
        chat_view.scroll_if_tailing()
        return

    lines = [
        f"**Review** `{detail.id}`\n",
        f"- **Job:** {detail.job_id}",
        f"- **Agent:** {detail.agent}",
        f"- **Addressed:** {'Yes' if detail.addressed else 'No'}",
    ]
    if detail.job:
        lines.extend(
            [
                f"- **Verdict:** {detail.job.verdict}",
                f"- **Branch:** {detail.job.branch}",
                f"- **Commit:** `{detail.job.git_ref[:7]}` {detail.job.commit_subject}",
            ]
        )
    if detail.output:
        lines.extend(["", "---", "", detail.output])

    msg = ChatMessage("\n".join(lines))
    msg.add_class("system-message")
    chat_view.mount(msg)
    chat_view.scroll_if_tailing()


def _handle_vim(app: "ChatApp") -> bool:
    """Toggle vim mode for input."""
    from claudechic.config import CONFIG, save

    current = CONFIG.get("vi-mode", False)
    new_state = not current
    CONFIG["vi-mode"] = new_state
    save()

    # Update all ChatInput widgets
    app._update_vi_mode(new_state)

    status = "enabled" if new_state else "disabled"
    app.notify(f"Vi mode {status}")
    return True


def _handle_analytics(app: "ChatApp", command: str) -> bool:
    """Handle /analytics commands: opt-in, opt-out."""
    from claudechic.config import CONFIG, save

    parts = command.split()
    subcommand = parts[1] if len(parts) > 1 else ""

    if subcommand == "opt-in":
        CONFIG["analytics"]["enabled"] = True
        save()
        app.notify("Analytics enabled")
        return True

    if subcommand == "opt-out":
        CONFIG["analytics"]["enabled"] = False
        save()
        app.notify("Analytics disabled")
        return True

    # Show current status
    enabled = CONFIG["analytics"]["enabled"]
    user_id = CONFIG["analytics"]["id"]
    status = "enabled" if enabled else "disabled"
    app.notify(f"Analytics {status}, ID: {user_id[:8]}...")
    return True


# =============================================================================
# Plan Swarm - Multi-perspective planning with debate
# =============================================================================

SWARM_PERSPECTIVE_PROMPT = """\
You are the {perspective_upper} planner in a swarm planning session.

YOUR IDENTITY: {swarm_id}-{perspective}
YOUR PEERS:
{peers}
ORCHESTRATOR: {orchestrator}

TASK: {task}

YOUR PERSPECTIVE: {perspective_description}

== RULES ==

- Research and design only - do NOT write or edit code
- Use Task tool with Explore subagents to understand the codebase
- Read files with Read, Glob, Grep
- Form strong opinions based on evidence

== PROTOCOL ==

1. RESEARCH: Launch 2-3 Explore subagents in parallel to understand the codebase.

2. PROPOSE: Send your plan to peers and orchestrator:
   tell_agent("{peer1}", "PROPOSAL from {perspective}: [detailed plan with rationale]")
   tell_agent("{peer2}", "PROPOSAL from {perspective}: [detailed plan with rationale]")
   tell_agent("{orchestrator}", "PROPOSAL SENT: [1-sentence summary]")

3. DEBATE: When you receive peer proposals, respond directly:
   - Challenge assumptions with evidence from the codebase
   - Defend your approach, but acknowledge good points
   - You CAN change your mind if convinced

4. CONCLUDE: After 2-3 rounds, send final position:
   tell_agent("{orchestrator}", "FINAL POSITION: [refined plan, noting changes from debate]")

== DEBATE STYLE ==

Be direct. Use evidence: "The pattern in auth.py uses X, so..." or "I found 3 places that would break..."
"""

PERSPECTIVE_DESCRIPTIONS = {
    "conservative": (
        "Minimize risk. Prefer proven patterns. Prioritize reliability.\n"
        "- Use established solutions over novel approaches\n"
        "- Minimize changes to existing architecture\n"
        "- Ensure backwards compatibility\n"
        "- Focus on maintainability and testability\n"
        "- When in doubt, do less"
    ),
    "balanced": (
        "Balance pragmatism with improvement. Follow best practices.\n"
        "- Consider trade-offs between innovation and stability\n"
        "- Apply industry-standard patterns where appropriate\n"
        "- Weigh implementation cost against long-term benefits\n"
        "- Find the 'Goldilocks' solution - not too much, not too little\n"
        "- Be the voice of reason between extremes"
    ),
    "creative": (
        "Challenge assumptions. Propose novel approaches. Take calculated risks.\n"
        "- Question whether the existing approach is optimal\n"
        "- Consider unconventional solutions that could be game-changers\n"
        "- Identify opportunities others might miss\n"
        "- Accept higher risk for potentially higher reward\n"
        "- Push the boundaries of what's possible"
    ),
}

SWARM_ORCHESTRATOR_PROMPT = """\
You are orchestrating a swarm planning debate for task: {task}

== FIRST: CALL EnterPlanMode ==

== AGENTS (already spawned) ==

- {swarm_id}-conservative (risk-averse, proven patterns)
- {swarm_id}-balanced (pragmatic middle ground)
- {swarm_id}-creative (novel approaches, higher risk/reward)

They will research, propose plans to each other, debate, and send you final positions.

== YOUR JOB ==

1. Call EnterPlanMode now

2. Monitor: You'll receive PROPOSAL SENT and FINAL POSITION messages

3. If stuck: ask_agent("{swarm_id}-conservative", "What's your status?")

4. Synthesize: Once you have final positions, write synthesis to your plan file

5. User choice via AskUserQuestion: Conservative / Balanced / Creative / Hybrid

6. Finalize: Write final plan, call ExitPlanMode, then close_agent on all 3
"""


def _build_perspective_prompt(
    perspective: str, swarm_id: str, task: str, peers: list[str], orchestrator: str
) -> str:
    """Build the prompt for a perspective agent."""
    peer1, peer2 = peers
    peers_list = "\n".join(f"- {p}" for p in peers)

    return SWARM_PERSPECTIVE_PROMPT.format(
        perspective=perspective,
        perspective_upper=perspective.upper(),
        perspective_description=PERSPECTIVE_DESCRIPTIONS[perspective],
        swarm_id=swarm_id,
        task=task,
        peers=peers_list,
        peer1=peer1,
        peer2=peer2,
        orchestrator=orchestrator,
    )


def _handle_plan_swarm(app: "ChatApp") -> bool:
    """Enter plan-swarm mode. Next user message will spawn perspective agents."""
    agent = app._agent
    if not agent:
        app.notify("No active agent", severity="error")
        return True

    async def enter_mode():
        await agent.set_permission_mode("planSwarm")

    app.run_worker(enter_mode(), exclusive=False)
    app.notify("Plan swarm mode - enter your task")
    return True


def start_plan_swarm(app: "ChatApp", task: str) -> None:
    """Spawn perspective agents and send orchestrator prompt. Called when user sends first message in planSwarm mode."""
    agent = app._agent
    if not agent:
        return

    # Reset mode to default (the orchestrator will enter plan mode via EnterPlanMode)
    async def reset_mode():
        await agent.set_permission_mode("default")

    app.run_worker(reset_mode(), exclusive=False)

    cwd = agent.cwd
    orchestrator = agent.name

    # Generate unique swarm ID
    swarm_id = str(uuid.uuid4())[:8]

    # Spawn the 3 perspective agents
    perspectives = ["conservative", "balanced", "creative"]
    for p in perspectives:
        peers = [f"{swarm_id}-{x}" for x in perspectives if x != p]
        prompt = _build_perspective_prompt(p, swarm_id, task, peers, orchestrator)
        agent_name = f"{swarm_id}-{p}"
        app._create_new_agent(
            agent_name, cwd, switch_to=False, initial_prompt=(prompt, "/plan-swarm")
        )

    # Send orchestrator prompt to current agent
    orchestrator_prompt = SWARM_ORCHESTRATOR_PROMPT.format(
        task=task,
        swarm_id=swarm_id,
    )
    app._send_to_active_agent(orchestrator_prompt, display_as="/plan-swarm")
