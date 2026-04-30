"""Worktree command handlers extracted from app.py."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING

from textual.containers import Center
from textual import work

from claudechic.analytics import capture

from claudechic.features.worktree.git import (
    FinishInfo,
    FinishPhase,
    FinishState,
    ResolutionAction,
    WorktreeInfo,
    WorktreeStatus,
    clean_gitignored_files,
    cleanup_worktrees,
    determine_resolution_action,
    diagnose_worktree,
    discard_all_changes,
    fast_forward_merge,
    finish_cleanup,
    get_cleanup_fix_prompt,
    get_finish_info,
    get_finish_prompt,
    is_git_repo,
    list_worktrees,
    remove_worktree,
    start_worktree,
)
from claudechic.widgets.prompts import (
    UncommittedChangesPrompt,
    WorktreePrompt,
)

if TYPE_CHECKING:
    from claudechic.app import ChatApp
    from claudechic.agent import Agent

# Max retries for worktree cleanup before giving up
MAX_CLEANUP_ATTEMPTS = 3


def handle_worktree_command(app: "ChatApp", command: str) -> None:
    """Handle /worktree commands.

    Args:
        app: The ChatApp instance
        command: Full command string (e.g. "/worktree finish")
    """
    if not is_git_repo():
        app.notify("Not in a git repository", severity="error")
        return

    parts = command.split(maxsplit=2)

    if len(parts) == 1:
        _show_worktree_modal(app)
        return

    agent = app._agent
    agent_id = agent.analytics_id if agent else "unknown"

    subcommand = parts[1]
    if subcommand == "finish":
        app.run_worker(capture("worktree_action", action="finish", agent_id=agent_id))
        _handle_finish(app)
    elif subcommand == "cleanup":
        app.run_worker(capture("worktree_action", action="cleanup", agent_id=agent_id))
        branches = parts[2].split() if len(parts) > 2 else None
        _handle_cleanup(app, branches)
    elif subcommand == "discard":
        app.run_worker(capture("worktree_action", action="discard", agent_id=agent_id))
        _handle_discard(app)
    else:
        app.run_worker(capture("worktree_action", action="create", agent_id=agent_id))
        _switch_or_create_worktree(app, subcommand)


@work(group="finish_start", exclusive=True, exit_on_error=False)
async def _handle_finish(app: "ChatApp") -> None:
    """Handle /worktree finish command.

    Phase-based approach:
    1. Pre-flight: diagnose worktree status
    2. Resolution: handle uncommitted, merge/rebase
    3. Cleanup: remove worktree and branch
    """

    agent = app._agent
    if not agent:
        app.notify("No active agent", severity="error")
        return

    success, message, info = await asyncio.to_thread(get_finish_info, app.sdk_cwd)
    if not success or info is None:
        app.notify(message, severity="error")
        return

    # Phase 1: Pre-flight diagnosis
    status = await asyncio.to_thread(diagnose_worktree, info)

    # Store state on agent
    agent.finish_state = FinishState(
        info=info,
        phase=FinishPhase.RESOLUTION,
        status=status,
    )

    # Show status summary
    _show_finish_status(app, status)

    # Phase 2: Resolution
    _run_resolution(app, agent)


def _show_finish_status(app: "ChatApp", status: WorktreeStatus) -> None:
    """Show pre-flight status to user."""
    parts = []
    if status.commits_ahead == 0:
        parts.append("no commits")
    else:
        parts.append(f"{status.commits_ahead} commits")
    if status.is_merged:
        parts.append("already merged")
    if status.has_uncommitted:
        parts.append(f"{len(status.uncommitted_files)} uncommitted")
    if status.untracked_gitignored:
        parts.append(f"{len(status.untracked_gitignored)} gitignored")
    if status.untracked_other:
        parts.append(f"{len(status.untracked_other)} untracked")

    app.notify(f"Status: {', '.join(parts)}")


@work(group="finish", exclusive=True, exit_on_error=False)
async def _run_resolution(app: "ChatApp", agent: "Agent") -> None:
    """Run Phase 2: Resolution. May invoke Claude or prompt user."""
    from claudechic.widgets import ChatInput

    state = agent.finish_state
    if not state or not state.status:
        return

    # Loop to handle actions that can be resolved immediately
    while True:
        action = determine_resolution_action(state.status)

        if action == ResolutionAction.NONE:
            # Nothing to resolve, go to cleanup
            state.phase = FinishPhase.CLEANUP
            _run_cleanup(app, agent)
            return

        if action == ResolutionAction.CLEAN_GITIGNORED:
            # Auto-clean gitignored files (safe)
            app.notify("Cleaning gitignored files...")
            success, error = clean_gitignored_files(state.info.worktree_dir)
            if not success:
                app.notify(f"Failed to clean: {error}", severity="error")
                agent.finish_state = None
                return
            # Re-diagnose and loop
            state.status = diagnose_worktree(state.info)
            continue

        if action == ResolutionAction.PROMPT_UNCOMMITTED:
            # Ask user what to do
            prompt = UncommittedChangesPrompt(
                state.status.uncommitted_files,
                state.status.untracked_other,
            )
            async with app._show_prompt(prompt) as p:
                p.focus()
                choice = await p.wait()

            app.query_one("#input", ChatInput).focus()

            if choice == "abort" or choice is None:
                state.phase = FinishPhase.ABORTED
                agent.finish_state = None
                app.notify("Finish aborted")
                return

            if choice == "discard":
                app.notify("Discarding all changes...")
                success, error = discard_all_changes(state.info.worktree_dir)
                if not success:
                    app.notify(f"Failed to discard: {error}", severity="error")
                    agent.finish_state = None
                    return
                # Re-diagnose and loop
                state.status = diagnose_worktree(state.info)
                continue

            if choice == "commit":
                # Ask Claude to commit
                app._show_thinking(agent.id)
                app._send_to_agent(
                    agent, "Commit all changes with a descriptive message."
                )
                # Resolution will continue in on_response_complete_finish
                return

        if action == ResolutionAction.FAST_FORWARD:
            app.notify("Fast-forward merge...")
            success, error = fast_forward_merge(state.info)
            if success:
                state.phase = FinishPhase.CLEANUP
                _run_cleanup(app, agent)
            else:
                # Unexpected - fall back to Claude
                app._show_thinking(agent.id)
                app._send_to_agent(
                    agent,
                    f"Fast-forward merge failed: {error}\n\n"
                    + get_finish_prompt(state.info),
                    display_as="/worktree finish",
                )
            return

        if action == ResolutionAction.REBASE:
            # Claude handles rebase
            app._show_thinking(agent.id)
            app._send_to_agent(
                agent, get_finish_prompt(state.info), display_as="/worktree finish"
            )
            return

        # Unknown action - shouldn't happen
        return


@work(group="cleanup", exclusive=True, exit_on_error=False)
async def _run_cleanup(app: "ChatApp", agent: "Agent") -> None:
    """Run Phase 3: Cleanup. Bash only, Claude if it fails."""

    state = agent.finish_state
    if not state:
        return

    success, message = await asyncio.to_thread(finish_cleanup, state.info)
    if success:
        _finish_complete(app, agent, message)
        return

    # Cleanup failed - ask Claude for help
    state.cleanup_attempts += 1
    state.last_error = message

    if state.cleanup_attempts >= MAX_CLEANUP_ATTEMPTS:
        app.notify(
            f"Cleanup failed after {MAX_CLEANUP_ATTEMPTS} attempts: {message}",
            severity="error",
        )
        agent.finish_state = None
        return

    app._show_thinking(agent.id)
    app._send_to_agent(
        agent,
        get_cleanup_fix_prompt(message, state.info.worktree_dir),
        display_as=f"[Cleanup attempt {state.cleanup_attempts}/{MAX_CLEANUP_ATTEMPTS} failed]",
    )


def _finish_complete(app: "ChatApp", agent: "Agent", warning: str = "") -> None:
    """Handle successful finish completion."""
    state = agent.finish_state
    if not state:
        return

    branch_name = state.info.branch_name
    agent.finish_state = None

    if warning:
        app.notify(f"Cleaned up {branch_name}{warning}", severity="warning")
    else:
        app.notify(f"Cleaned up {branch_name}")

    _close_agents_for_branches(app, [branch_name])


@work(group="finish_continue", exclusive=True, exit_on_error=False)
async def on_response_complete_finish(app: "ChatApp", agent: "Agent") -> None:
    """Called from on_response_complete when finish_state is set.

    Continues the finish process after Claude completes a task.
    """

    state = agent.finish_state
    if not state:
        return

    if state.phase == FinishPhase.RESOLUTION:
        # Re-diagnose to see if Claude fixed things
        state.status = await asyncio.to_thread(diagnose_worktree, state.info)
        action = determine_resolution_action(state.status)

        if action == ResolutionAction.NONE:
            # Resolution complete, move to cleanup
            state.phase = FinishPhase.CLEANUP
            _run_cleanup(app, agent)
        else:
            # Still needs work - continue resolution
            _run_resolution(app, agent)

    elif state.phase == FinishPhase.CLEANUP:
        # Claude attempted to fix cleanup issue - retry
        _run_cleanup(app, agent)


def _switch_or_create_worktree(app: "ChatApp", feature_name: str) -> None:
    """Switch to existing worktree agent or create new one."""
    # Check if we already have an agent for this worktree
    for agent in app.agents.values():
        if agent.worktree == feature_name:
            if app.agent_mgr:
                app.agent_mgr.switch(agent.id)
            app.notify(f"Switched to {feature_name}")
            return

    # Check if worktree exists on disk
    existing = [wt for wt in list_worktrees() if wt.branch == feature_name]
    if existing:
        wt = existing[0]
        app._create_new_agent(
            feature_name, wt.path, worktree=feature_name, auto_resume=True
        )
    else:
        # Create new worktree. Pass the active agent's cwd so the parent
        # branch recorded on the new worktree reflects where the user was
        # working — not the app process's cwd.
        success, message, new_cwd = start_worktree(feature_name, parent_cwd=app.sdk_cwd)
        if success and new_cwd:
            app._create_new_agent(
                feature_name, new_cwd, worktree=feature_name, auto_resume=False
            )
        else:
            app.notify(message, severity="error")


def _close_agents_for_branches(app: "ChatApp", branches: list[str]) -> None:
    """Close agents associated with removed worktree branches."""
    for branch in branches:
        agent = next((a for a in app.agents.values() if a.worktree == branch), None)
        if agent and len(app.agents) > 1:
            if app.active_agent_id == agent.id:
                main = next(
                    (a for a in app.agents.values() if a.worktree is None), None
                )
                if main and app.agent_mgr:
                    app.agent_mgr.switch(main.id)
            app._do_close_agent(agent.id)


def _handle_discard(app: "ChatApp") -> None:
    """Handle /worktree discard command - discard current worktree entirely."""
    success, message, info = get_finish_info(app.sdk_cwd)
    if not success or info is None:
        app.notify(message, severity="error")
        return

    status = diagnose_worktree(info)

    # Check if there's anything to warn about
    has_commits = status.commits_ahead > 0 and not status.is_merged
    has_changes = status.has_uncommitted or status.untracked_other

    if has_commits or has_changes:
        _run_discard_prompt(app, info, status)
    else:
        _do_discard(app, info)


def _do_discard(app: "ChatApp", info: FinishInfo) -> None:
    """Force remove worktree and branch."""
    wt = WorktreeInfo(path=info.worktree_dir, branch=info.branch_name, is_main=False)
    success, msg = remove_worktree(wt, force=True)
    if success:
        app.notify(f"Discarded {info.branch_name}")
        _close_agents_for_branches(app, [info.branch_name])
    else:
        app.notify(msg, severity="error")


@work(group="discard_prompt", exclusive=True, exit_on_error=False)
async def _run_discard_prompt(
    app: "ChatApp", info: FinishInfo, status: WorktreeStatus
) -> None:
    """Prompt user to confirm discarding a worktree with commits/changes."""
    from claudechic.widgets import SelectionPrompt, ChatInput

    warnings = []
    if status.commits_ahead > 0 and not status.is_merged:
        warnings.append(f"{status.commits_ahead} unmerged commits")
    if status.has_uncommitted:
        warnings.append(f"{len(status.uncommitted_files)} uncommitted changes")
    if status.untracked_other:
        warnings.append(f"{len(status.untracked_other)} untracked files")

    warning_text = ", ".join(warnings)
    options = [
        ("discard", f"Discard anyway ({warning_text})"),
        ("cancel", "Cancel"),
    ]

    async with app._show_prompt(
        SelectionPrompt(f"Discard {info.branch_name}?", options)
    ) as prompt:
        prompt.focus()
        selected = await prompt.wait()

    app.query_one("#input", ChatInput).focus()

    if selected == "discard":
        _do_discard(app, info)
    else:
        app.notify("Discard cancelled")


def _handle_cleanup(app: "ChatApp", branches: list[str] | None) -> None:
    """Handle /worktree cleanup command."""
    results = cleanup_worktrees(branches)

    if not results:
        app.notify("No worktrees to clean up")
        return

    # Check if any need confirmation
    needs_confirm = [(b, msg) for b, _, msg, confirm in results if confirm]
    removed = [b for b, success, _, _ in results if success]
    failed = [
        (b, msg) for b, success, msg, confirm in results if not success and not confirm
    ]

    # Close agents for successfully removed worktrees
    _close_agents_for_branches(app, removed)

    # Remove ghost worktrees from sidebar
    for branch in removed:
        app.agent_section.remove_worktree(branch)

    # Report results
    for branch in removed:
        app.notify(f"Removed: {branch}")
    for branch, msg in failed:
        app.notify(f"Failed: {branch} - {msg}", severity="error")

    # Prompt for confirmation on dirty/unmerged
    if needs_confirm:
        _run_cleanup_prompt(app, needs_confirm)


@work(group="cleanup_prompt", exclusive=True, exit_on_error=False)
async def _run_cleanup_prompt(
    app: "ChatApp", needs_confirm: list[tuple[str, str]]
) -> None:
    """Show prompt for confirming worktree removal."""
    from claudechic.widgets import SelectionPrompt, ChatInput

    branches_to_confirm = [b for b, _ in needs_confirm]
    options = [("all", f"Remove all ({len(needs_confirm)})")]
    options.extend((b, f"Remove {b} ({msg})") for b, msg in needs_confirm)
    options.append(("cancel", "Cancel"))

    async with app._show_prompt(
        SelectionPrompt("Worktrees with changes or unmerged:", options)
    ) as prompt:
        prompt.focus()
        selected = await prompt.wait()

    if selected and selected != "cancel":
        to_remove = branches_to_confirm if selected == "all" else [selected]
        worktrees = list_worktrees()
        removed = []
        for branch in to_remove:
            wt = next((w for w in worktrees if w.branch == branch), None)
            if wt:
                success, msg = remove_worktree(wt, force=True)
                if success:
                    removed.append(branch)
                app.notify(
                    f"Removed: {branch}" if success else msg,
                    severity="error" if not success else "information",
                )
        _close_agents_for_branches(app, removed)
        # Remove ghost worktrees from sidebar
        for branch in removed:
            app.agent_section.remove_worktree(branch)
    else:
        app.notify("Cleanup cancelled")

    app.query_one("#input", ChatInput).focus()


def _show_worktree_modal(app: "ChatApp") -> None:
    """Show worktree selection modal."""
    worktrees = [(str(wt.path), wt.branch) for wt in list_worktrees() if not wt.is_main]
    prompt = WorktreePrompt(worktrees)
    container = Center(prompt, id="worktree-modal")
    app.mount(container)
    _wait_for_worktree_selection(app, prompt, container)


@work(group="worktree", exclusive=True, exit_on_error=False)
async def _wait_for_worktree_selection(
    app: "ChatApp", prompt: WorktreePrompt, container: Center
) -> None:
    """Wait for worktree modal selection and act on it."""
    try:
        result = await prompt.wait()
        container.remove()
        if result is None:
            return  # Cancelled

        action, value = result
        if action == "switch":
            # value is the path; find the branch name from worktrees
            worktrees = {str(wt.path): wt.branch for wt in list_worktrees()}
            branch = worktrees.get(value, Path(value).name)
            _switch_or_create_worktree(app, branch)
        elif action == "new":
            _switch_or_create_worktree(app, value)
    except Exception as e:
        app.show_error("Worktree selection failed", e)
