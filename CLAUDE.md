# Claude Chic

A stylish terminal UI for Claude Code, built with Textual and wrapping the `claude-agent-sdk`.

## Run

```bash
uv run claudechic
uv run claudechic --resume     # Resume most recent session
uv run claudechic -s <uuid>    # Resume specific session
```

Requires Claude Code to be logged in with a Max/Pro subscription (`claude /login`).

## File Map

```
claudechic/
├── __init__.py        # Package entry, exports ChatApp
├── __main__.py        # CLI entry point
├── agent.py           # Agent class - SDK connection, history, permissions, state
├── analytics.py       # PostHog analytics - fire-and-forget event tracking
├── agent_manager.py   # AgentManager - coordinates multiple concurrent agents
├── app.py             # ChatApp - main application, event handlers
├── commands.py        # Slash command routing (/agent, /shell, /clear, etc.)
├── compact.py         # Session compaction - shrink old tool uses to save context
├── errors.py          # Logging infrastructure, error handling
├── file_index.py      # Fuzzy file search using git ls-files
├── formatting.py      # Tool formatting, diff rendering (pure functions)
├── history.py         # Global history loading from ~/.claude/history.jsonl
├── mcp.py             # In-process MCP server for agent control tools
├── messages.py        # Custom Textual Message types for SDK events
├── remote.py          # HTTP server for remote control (live testing)
├── permissions.py     # PermissionRequest dataclass for tool approval
├── profiling.py       # Lightweight profiling utilities (@profile decorator)
├── sampling.py        # CPU-conditional sampling profiler for high-CPU investigation
├── protocols.py       # Observer protocols (AgentObserver, AgentManagerObserver)
├── sessions.py        # Session file loading and listing (pure functions)
├── styles.tcss        # Textual CSS - visual styling
├── theme.py           # Textual theme definition
├── usage.py           # OAuth usage API fetching (rate limits)
├── features/
│   ├── __init__.py    # Feature module exports
│   └── worktree/
│       ├── __init__.py   # Public API (list_worktrees, handle_worktree_command)
│       ├── commands.py   # /worktree command handlers
│       └── git.py        # Git worktree operations
├── processes.py       # BackgroundProcess dataclass, child process detection
├── screens/           # Full-page screens (navigation)
│   ├── chat.py        # ChatScreen - main chat UI (default screen)
│   ├── diff.py        # DiffScreen - review uncommitted changes
│   └── session.py     # SessionScreen - session browser for /resume
└── widgets/
    ├── __init__.py    # Re-exports all widgets for backward compat
    ├── prompts.py     # All prompt widgets (Selection, Question, Model, Worktree)
    ├── base/          # Protocols and base classes
    │   ├── clickable.py # ClickableLabel base class
    │   ├── tool_base.py # ToolWidgetBase class
    │   └── tool_protocol.py # ToolWidget protocol
    ├── primitives/    # Low-level building blocks
    │   ├── button.py  # Button with click handling
    │   ├── collapsible.py # QuietCollapsible
    │   ├── scroll.py  # AutoHideScroll
    │   └── spinner.py # Animated spinner
    ├── content/       # Content display widgets
    │   ├── message.py # ChatMessage, ChatInput, ThinkingIndicator
    │   ├── tools.py   # ToolUseWidget, TaskWidget, AgentToolWidget
    │   ├── diff.py    # Syntax-highlighted diff widget
    │   └── todo.py    # TodoPanel, TodoWidget
    ├── input/         # User input widgets
    │   ├── autocomplete.py # TextAreaAutoComplete
    │   └── history_search.py # HistorySearch (Ctrl+R)
    ├── layout/        # Structural/container widgets
    │   ├── chat_view.py # ChatView - renders agent messages
    │   ├── sidebar.py # AgentSidebar, AgentItem, WorktreeItem
    │   ├── footer.py  # StatusFooter, AutoEditLabel, ModelLabel
    │   ├── indicators.py # IndicatorWidget, CPUBar, ContextBar, ProcessIndicator
    │   └── processes.py # ProcessPanel, ProcessItem
    ├── reports/       # In-page report widgets
    │   ├── context.py # ContextReport - visual 2D grid
    │   └── usage.py   # UsageReport, UsageBar
    └── modals/        # Modal screen overlays
        ├── profile.py # ProfileModal - profiling stats
        └── process_modal.py # ProcessModal

tests/
├── __init__.py        # Package marker
├── conftest.py        # Shared fixtures (wait_for)
├── test_app.py        # E2E tests with real SDK
├── test_app_ui.py     # App UI tests without SDK
├── test_autocomplete.py # Autocomplete widget tests
├── test_file_index.py # Fuzzy file search tests
└── test_widgets.py    # Pure widget tests
```

## Architecture

### Module Boundaries

**Pure functions (no UI dependencies):**
- `formatting.py` - Tool header formatting, diff rendering, language detection
- `sessions.py` - Session file I/O, listing, filtering
- `file_index.py` - Fuzzy file search, git ls-files integration
- `compact.py` - Session compaction to reduce context window usage
- `usage.py` - OAuth API for rate limit info

**Agent layer (no UI dependencies):**
- `agent.py` - `Agent` class owns SDK client, message history, permissions, state
- `agent_manager.py` - Coordinates multiple agents, switching, lifecycle
- `protocols.py` - Observer protocols (`AgentObserver`, `AgentManagerObserver`, `PermissionHandler`)

**Internal protocol:**
- `messages.py` - Custom `Message` subclasses for async event communication
- `permissions.py` - `PermissionRequest` dataclass bridging SDK callbacks to UI
- `mcp.py` - MCP server exposing agent control tools to Claude

**Features:**
- `features/worktree/` - Git worktree management for isolated development

**UI components:**
- `widgets/` - Textual widgets with associated styles
- `widgets/chat_view.py` - `ChatView` renders agent messages, handles streaming
- `app.py` - Main app orchestrating widgets and agents via observer pattern

### Widget Hierarchy

```
ChatApp
└── ChatScreen (default screen, owns chat-specific bindings)
    ├── Horizontal #main
    │   ├── Vertical #chat-column
    │   │   ├── ChatView (one per agent, only active visible)
    │   │   │   ├── ChatMessage (user/assistant)
    │   │   │   ├── ToolUseWidget (collapsible tool display)
    │   │   │   ├── TaskWidget (for Task tool - nested content)
    │   │   │   └── ThinkingIndicator (animated spinner)
    │   │   └── Vertical #input-container
    │   │       ├── ImageAttachments (hidden by default)
    │   │       ├── ChatInput (or SelectionPrompt/QuestionPrompt)
    │   │       └── TextAreaAutoComplete
    │   └── Vertical #right-sidebar (hidden when narrow)
    │       ├── AgentSection
    │       ├── TodoPanel
    │       └── ProcessPanel
    └── StatusFooter
```

### Observer Pattern

Agent and AgentManager emit events via protocol-based observers:

```
Agent events (AgentObserver)         ChatApp handlers
────────────────────────────         ────────────────
on_text_chunk()                  ->  ChatView.append_text()
on_tool_use()                    ->  ChatView.append_tool_use()
on_tool_result()                 ->  ChatView.update_tool_result()
on_complete()                    ->  end response, update UI
on_status_changed()              ->  update AgentSidebar indicator
on_prompt_added()                ->  show SelectionPrompt/QuestionPrompt

AgentManager events                  ChatApp handlers
───────────────────                  ────────────────
on_agent_created()               ->  create ChatView, update sidebar
on_agent_switched()              ->  show/hide ChatViews
on_agent_closed()                ->  remove ChatView, update sidebar
```

This decouples Agent (pure async) from UI (Textual widgets).

### Permission Flow

When SDK needs tool approval:
1. `can_use_tool` callback creates `PermissionRequest`
2. Request queued to `app.interactions` (for testing)
3. `SelectionPrompt` mounted, replacing input
4. User selects allow/deny/allow-all
5. Callback returns `PermissionResultAllow` or `PermissionResultDeny`

For `AskUserQuestion` tool: `QuestionPrompt` handles multi-question flow.

### Styling

Visual language uses left border bars to indicate content type:
- **Orange** (`#cc7700`) - User messages
- **Blue** (`#334455`) - Assistant messages
- **Gray** (`#333333`) - Tool uses (brightens on hover)
- **Blue-gray** (`#445566`) - Task widgets

Context/CPU bars color-code by threshold (dim → yellow → red).

Copy buttons appear on hover. Collapsibles auto-collapse older tool uses.

## Key SDK Usage

```python
from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

client = ClaudeSDKClient(ClaudeAgentOptions(
    permission_mode="default",
    env={"ANTHROPIC_API_KEY": ""},
    can_use_tool=permission_callback,
    resume=session_id,
))
await client.connect()
await client.query("prompt")
async for message in client.receive_response():
    # Handle AssistantMessage, TextBlock, ToolUseBlock, ToolResultBlock, ResultMessage
```

## Keybindings

- Enter: Send message
- Ctrl+C (x2): Quit
- Ctrl+L: Clear chat (UI only)
- Ctrl+R: Reverse history search
- Shift+Tab: Cycle permission mode (default → auto-edit → plan)
- Ctrl+N: New agent (hint)
- Ctrl+1-9: Switch to agent by position

## Commands

### Multi-Agent
- `/agent` - List all agents
- `/agent <name>` - Create new agent in current directory
- `/agent <name> <path>` - Create new agent in specified directory
- `/agent close` - Close current agent
- `/agent close <name>` - Close agent by name

Agent status indicators: ○ (idle), ● gray (busy), ● orange (needs input)

### Session Management
- `/resume` - Show session picker
- `/resume <id>` - Resume specific session
- `/usage` - Show API rate limit usage
- `/clear` - Clear chat UI
- `/shell <cmd>` - Suspend TUI and run shell command
- `/exit` - Quit

## Configuration

Configuration is stored in `~/.claude/.claudechic.yaml`.

### Worktree Path Templates

Customize worktree locations in `~/.claude/.claudechic.yaml` using template variables:

```yaml
worktree:
  path_template: "$HOME/code/worktrees/${repo_name}/${branch_name}"
```

**Variables:** `${repo_name}`, `${branch_name}`, `$HOME`, `~`

**Common patterns:**
```yaml
path_template: "$HOME/code/worktrees/${repo_name}/${branch_name}"  # By repo/branch
path_template: "$HOME/worktrees/${repo_name}-${branch_name}"       # Flat structure
path_template: null                                                # Sibling dirs (default)
```

## Testing

```bash
uv run python -m pytest tests/ -n auto -q  # Parallel (fast, ~3s)
uv run python -m pytest tests/ -v          # Sequential with verbose output
```

Use parallel testing by default.

## Remote Testing

For live testing by AI agents, run with remote control enabled:

```bash
./scripts/claudechic-remote 9999
```

This starts an HTTP server on port 9999 with endpoints for sending messages, taking screenshots, and checking state. See [.ai-docs/remote-testing.md](.ai-docs/remote-testing.md) for full API documentation.

## Pre-commit Hooks

```bash
uv run pre-commit install  # Install hooks (one-time)
uv run pre-commit run --all-files  # Run manually
```

Hooks: ruff (lint + fix), ruff-format, pyright. Run automatically on commit.

## GitHub

- **Repo:** https://github.com/mrocklin/claudechic
- **CLI:** `gh` is installed and authenticated as `mrocklin-ai`
- Use `gh issue list/view`, `gh pr list/view/create`, etc. for GitHub operations
