# Changes from upstream

This fork adds Windows compatibility fixes and quality-of-life improvements.
Branch: `worktree-disable-config` | PR: https://github.com/mrocklin/claudechic/pull/61

---

## Worktree

**`worktree.enabled` config flag** — set `worktree: enabled: false` in `.claudechic.yaml` to disable git worktree operations entirely. Prevents UI hangs caused by `EnterWorktree`/`ExitWorktree` git operations. When disabled, subagents run in the current directory instead of spinning up isolated worktrees.

## Clipboard / image paste

**Raw clipboard image support** — pasting a screenshot (Win+Shift+S, ShareX, etc.) now works via PIL fallback. Previously only file paths were detected; raw image data was silently dropped. Saves clipboard image to a temp file and attaches it as normal.

**ShareX HTTP endpoint** — `POST /attach-image` added to the remote control server. Accepts `{"path": "/abs/path/to/image.png"}`. Lets external tools (ShareX custom actions, scripts) attach images without clipboard involvement.

**`remote_port` config key** — set `remote_port: 9001` in `.claudechic.yaml` to auto-start the HTTP server on that port without needing the `--remote-port` CLI flag every launch.

**Graceful port-in-use handling** — if the configured remote port is already held by a previous session, ClaudeChic now shows a warning notification and continues starting normally instead of crashing silently.

## Windows session fixes

**Session path bug** — `get_project_sessions_dir()` was stripping the drive colon (`C:\foo` → `C-foo`) instead of replacing it with a dash (`C--foo`). Claude Code uses the dash form, so the session directory was never found. Context bar showed zero tokens; session history failed to load.

**UTF-8 encoding** — session JSONL files are UTF-8 but `aiofiles.open()` defaulted to the Windows system encoding (cp1252). Fixed by adding `encoding="utf-8"`. Prevents `UnicodeDecodeError` when loading sessions containing emoji or non-ASCII characters.

## Error handling

**Worker errors show as notifications** — previously, any background worker failure (`exit_on_error=True`) crashed the whole app and closed the window with no explanation. All workers now use `exit_on_error=False`. A central `on_worker_state_changed` handler catches failures and shows a red corner notification with the error message (10s timeout).

## UI

**Context-mode MCP tools collapse by default** — tools whose names start with `mcp__plugin_context-mode` now start collapsed, same as `Read`, `Grep`, etc. Reduces noise from sandbox utility tools that run frequently.
