"""Help data and formatting for /help command."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from claudechic.agent import Agent


# Claudechic-specific commands (imported from single source of truth)
def _get_chic_commands() -> list[tuple[str, str]]:
    from claudechic.commands import get_help_commands

    return get_help_commands()


# Keyboard shortcuts
SHORTCUTS = [
    ("Ctrl+C (x2)", "Quit"),
    ("Ctrl+L", "Clear chat"),
    ("Ctrl+S", "Screenshot"),
    ("Shift+Tab", "Toggle auto-edit mode"),
    ("Escape", "Cancel / Enter NORMAL mode (vi)"),
    ("Ctrl+N", "New agent hint"),
    ("Ctrl+R", "History search / Redo (vi)"),
    ("Ctrl+1-9", "Switch to agent by position"),
    ("Enter", "Send message"),
    ("Ctrl+J", "Insert newline"),
    ("Up/Down", "Navigate input history"),
]

# Vi mode keybindings (shown when vi mode is enabled)
VI_MODE_KEYS = [
    ("Esc", "Enter NORMAL mode"),
    ("i / I", "Insert before cursor / at line start"),
    ("a / A", "Insert after cursor / at line end"),
    ("o / O", "Open line below / above"),
    ("h / j / k / l", "Move left / down / up / right"),
    ("w / b / e", "Move word forward / back / end"),
    ("0 / $", "Move to line start / end"),
    ("gg / G", "Go to input start / end"),
    ("x", "Delete character"),
    ("dd / D", "Delete line / to end of line"),
    ("cc / C / cw", "Change line / to end / word"),
    ("yy / yw", "Yank (copy) line / word"),
    ("p / P", "Paste after / before cursor"),
    ("u / Ctrl+R", "Undo / Redo"),
    (".", "Repeat last change"),
    ("v", "Enter VISUAL mode"),
]

# MCP tools from claudechic (mcp.py)
MCP_TOOLS = [
    ("spawn_agent", "Create new Claude agent"),
    ("spawn_worktree", "Create git worktree with agent"),
    ("ask_agent", "Send question to another agent"),
    ("tell_agent", "Send message without expecting reply"),
    ("list_agents", "List all running agents"),
    ("close_agent", "Close an agent by name"),
]


def _parse_skill_description(path: Path) -> str:
    """Extract description from SKILL.md frontmatter."""
    try:
        content = path.read_text()
        if content.startswith("---"):
            end = content.find("---", 3)
            if end > 0:
                frontmatter = content[3:end]
                for line in frontmatter.split("\n"):
                    if line.startswith("description:"):
                        return line.split(":", 1)[1].strip()
    except Exception:
        pass
    return "No description"


def discover_skills() -> list[tuple[str, str]]:
    """Discover enabled skills from plugins."""
    skills = []

    # Read settings for enabled plugins
    settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return skills

    try:
        settings = json.loads(settings_path.read_text())
    except Exception:
        return skills

    enabled = settings.get("enabledPlugins", {})

    # Read installed plugins
    installed_path = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    if not installed_path.exists():
        return skills

    try:
        installed = json.loads(installed_path.read_text())
    except Exception:
        return skills

    for plugin_id, is_enabled in enabled.items():
        if not is_enabled:
            continue
        if plugin_id not in installed.get("plugins", {}):
            continue

        installs = installed["plugins"][plugin_id]
        if not installs:
            continue

        install_path = Path(installs[0]["installPath"])
        skills_dir = install_path / "skills"
        if not skills_dir.exists():
            continue

        for skill_dir in skills_dir.iterdir():
            if not skill_dir.is_dir():
                continue
            skill_md = skill_dir / "SKILL.md"
            if skill_md.exists():
                desc = _parse_skill_description(skill_md)
                # Format: plugin:skill or just skill if plugin matches
                plugin_name = plugin_id.split("@")[0]
                skill_name = skill_dir.name
                if plugin_name == skill_name:
                    skills.append((skill_name, desc))
                else:
                    skills.append((f"{plugin_name}:{skill_name}", desc))

    return skills


async def get_sdk_commands(agent: "Agent | None") -> list[tuple[str, str]]:
    """Get commands from SDK server info."""
    if not agent or not agent.client:
        return []

    try:
        info = await agent.client.get_server_info()
        if not info:
            return []

        return [
            (f"/{cmd['name']}", cmd.get("description", ""))
            for cmd in info.get("commands", [])
        ]
    except Exception:
        return []


async def format_help(agent: "Agent | None") -> str:
    """Format complete help text as markdown."""
    lines = ["# Help\n"]

    # Discover skills first so we can filter them from SDK commands
    skills = discover_skills()
    skill_names = {name.split(":")[0] for name, _ in skills}  # e.g. "frontend-design"

    # SDK commands (filter out skills which may appear here too)
    sdk_cmds = await get_sdk_commands(agent)
    sdk_cmds = [
        (cmd, desc) for cmd, desc in sdk_cmds if cmd.lstrip("/") not in skill_names
    ]
    if sdk_cmds:
        lines.append("## Claude Code Commands\n")
        lines.append("| Command | Description |")
        lines.append("|---------|-------------|")
        for cmd, desc in sdk_cmds:
            lines.append(f"| `{cmd}` | {desc} |")
        lines.append("")

    # Chic commands
    lines.append("## Claudechic Commands\n")
    lines.append("| Command | Description |")
    lines.append("|---------|-------------|")
    for cmd, desc in _get_chic_commands():
        lines.append(f"| `{cmd}` | {desc} |")
    lines.append("")

    # Skills (already discovered above for filtering)
    if skills:
        lines.append("## Skills\n")
        lines.append("| Skill | Description |")
        lines.append("|-------|-------------|")
        for name, desc in skills:
            lines.append(f"| `/{name}` | {desc} |")
        lines.append("")

    # MCP tools
    lines.append("## MCP Tools (chic)\n")
    lines.append("| Tool | Description |")
    lines.append("|------|-------------|")
    for name, desc in MCP_TOOLS:
        lines.append(f"| `{name}` | {desc} |")
    lines.append("")

    # Shortcuts
    lines.append("## Keyboard Shortcuts\n")
    lines.append("| Key | Action |")
    lines.append("|-----|--------|")
    for key, action in SHORTCUTS:
        lines.append(f"| `{key}` | {action} |")
    lines.append("")

    # Vi mode
    lines.append("## Vi Mode (toggle with `/vim`)\n")
    lines.append("| Key | Action |")
    lines.append("|-----|--------|")
    for key, action in VI_MODE_KEYS:
        lines.append(f"| `{key}` | {action} |")

    return "\n".join(lines)
