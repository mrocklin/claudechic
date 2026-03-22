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

**Graceful port-in-use handling** — if the configured remote port is already held by a previous session, ClaudeChic automatically finds and kills the stale Python process, waits 0.5s, then retries. Only kills Python processes (won't touch unrelated services). Falls back to a warning notification if the retry also fails.

**Clipboard paste no longer freezes window** — `PIL.ImageGrab.grabclipboard()` was called synchronously on the event loop, blocking the entire UI while Windows read and converted the screenshot. Now runs in a thread via `asyncio.to_thread()`. Screenshot pastes are non-blocking.

## Windows session fixes

**Session path bug** — `get_project_sessions_dir()` was stripping the drive colon (`C:\foo` → `C-foo`) instead of replacing it with a dash (`C--foo`). Claude Code uses the dash form, so the session directory was never found. Context bar showed zero tokens; session history failed to load.

**UTF-8 encoding** — session JSONL files are UTF-8 but `aiofiles.open()` defaulted to the Windows system encoding (cp1252). Fixed by adding `encoding="utf-8"`. Prevents `UnicodeDecodeError` when loading sessions containing emoji or non-ASCII characters.

## Error handling

**Worker errors show as notifications** — previously, any background worker failure (`exit_on_error=True`) crashed the whole app and closed the window with no explanation. All workers now use `exit_on_error=False`. A central `on_worker_state_changed` handler catches failures and shows a red corner notification with the error message (10s timeout).

## UI

**Configurable MCP tool collapse prefixes** — any MCP tool whose name starts with a configured prefix starts collapsed. Built-in defaults: `mcp__plugin_context-mode`, `mcp__searxng`. Override or extend in `.claudechic.yaml`:

```yaml
collapse-tool-prefixes:
  - mcp__plugin_context-mode
  - mcp__searxng
  - mcp__your_other_tool
```

If the key is absent, built-in defaults apply. Set to an empty list `[]` to disable prefix collapsing entirely.

**Plan review no longer hangs** — `ExitPlanMode` was rendering plan content as a Textual `Markdown` widget, which spawns hundreds of child widgets for large plans and blocks the layout thread. Also had a double-render path via `_try_update_plan_content`. Fixed by using `Static(markup=False)` (plain text, instantaneous) and applying the same lazy `content_factory` pattern as `Edit` for collapsed history items.

**Startup crash fix** — `config.setdefault("collapse-tool-prefixes", None)` stored `None` in the config dict. `dict.get(key, default)` returns the stored `None` (key exists), causing `tuple(None)` to crash at import time. Fixed with `CONFIG.get("collapse-tool-prefixes") or _DEFAULT_COLLAPSE_PREFIXES`.

**Desktop shortcut launches ClaudeChic** — `Claude.bat` updated to launch `python -m claudechic` with `ANTHROPIC_MODEL` + `ANTHROPIC_SMALL_MODEL` env vars for model selection, instead of the raw `claude.cmd` CLI.
