"""Git worktree management for isolated feature work."""

import subprocess
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

from claudechic.config import CONFIG
from claudechic.errors import log


class FinishPhase(Enum):
    """Phases of the /worktree finish process."""

    RESOLUTION = auto()  # Handling uncommitted changes, merging
    CLEANUP = auto()  # Removing worktree and branch
    ABORTED = auto()  # User cancelled


class ResolutionAction(Enum):
    """What action is needed in resolution phase."""

    NONE = auto()  # Nothing to do, go to cleanup
    CLEAN_GITIGNORED = auto()  # Run git clean -fdX
    PROMPT_UNCOMMITTED = auto()  # Ask user: commit/discard/abort
    FAST_FORWARD = auto()  # git merge --ff-only in main_dir
    REBASE = auto()  # Claude does rebase


@dataclass
class WorktreeInfo:
    """Info about an existing worktree."""

    path: Path
    branch: str
    is_main: bool


@dataclass
class FinishInfo:
    """Info needed to finish a worktree."""

    branch_name: str
    base_branch: str
    worktree_dir: Path
    main_dir: Path


@dataclass
class WorktreeStatus:
    """Pre-flight status of a worktree before finishing.

    All fields are gathered via git commands - no Claude involvement.
    """

    # Commit status
    commits_ahead: int  # Number of commits beyond base branch
    is_merged: bool  # Branch already merged into base
    can_fast_forward: bool  # Base is ancestor of branch (no rebase needed)

    # Working directory status
    uncommitted_files: list[str] = field(default_factory=list)  # Modified/staged files

    # Untracked files (categorized)
    untracked_gitignored: list[str] = field(default_factory=list)  # Safe to delete
    untracked_other: list[str] = field(default_factory=list)  # Need user decision

    @property
    def has_uncommitted(self) -> bool:
        return bool(self.uncommitted_files)

    @property
    def has_untracked(self) -> bool:
        return bool(self.untracked_gitignored or self.untracked_other)

    @property
    def is_clean(self) -> bool:
        """True if working directory is clean."""
        return not self.has_uncommitted and not self.has_untracked

    @property
    def only_gitignored_untracked(self) -> bool:
        """True if only untracked files are gitignored (safe to auto-clean)."""
        return (
            not self.has_uncommitted
            and not self.untracked_other
            and bool(self.untracked_gitignored)
        )


@dataclass
class FinishState:
    """Tracks state of an in-progress /worktree finish."""

    info: FinishInfo
    phase: FinishPhase
    status: WorktreeStatus | None = None
    cleanup_attempts: int = 0
    last_error: str | None = None


def is_git_repo() -> bool:
    """Check if the current directory is inside a git repository."""
    result = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def get_repo_name() -> str:
    """Get the current repository name."""
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip()).name


def _is_main_worktree(worktree_path: Path) -> bool:
    """Check if a worktree is the main one (not a linked worktree).

    Main worktrees have .git as a directory; linked worktrees have .git as a file
    pointing to the main repo's .git/worktrees/<name>.
    """
    git_path = worktree_path / ".git"
    return git_path.is_dir()


def list_worktrees() -> list[WorktreeInfo]:
    """List all git worktrees for this repo."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )

    worktrees = []
    current_path = None
    current_branch = None

    for line in result.stdout.strip().split("\n"):
        if line.startswith("worktree "):
            current_path = Path(line[9:])
        elif line.startswith("branch refs/heads/"):
            current_branch = line[18:]
        elif line == "":
            if current_path and current_branch:
                is_main = _is_main_worktree(current_path)
                worktrees.append(WorktreeInfo(current_path, current_branch, is_main))
            current_path = None
            current_branch = None

    # Handle last entry if no trailing newline
    if current_path and current_branch:
        is_main = _is_main_worktree(current_path)
        worktrees.append(WorktreeInfo(current_path, current_branch, is_main))

    return worktrees


def get_main_worktree() -> tuple[Path, str] | None:
    """Find the main worktree (non-feature) path and its branch."""
    for wt in list_worktrees():
        if wt.is_main:
            return wt.path, wt.branch
    return None


_PARENT_BRANCH_FILE = "claudechic-parent-branch"


def _worktree_git_dir(worktree_path: Path) -> Path | None:
    """Resolve the per-worktree git dir (e.g. <main>/.git/worktrees/<name>).

    For a linked worktree the worktree's `.git` is a file containing
    `gitdir: <abs path>` pointing into the main repo's `.git/worktrees/`.
    For the main worktree, `.git` is itself a directory. We parse the
    pointer file directly rather than shelling out to `git rev-parse` so
    this is cheap and doesn't muddy subprocess-based tests.
    """
    git_marker = worktree_path / ".git"
    if git_marker.is_dir():
        return git_marker
    if not git_marker.is_file():
        return None
    try:
        content = git_marker.read_text().strip()
    except OSError:
        return None
    prefix = "gitdir: "
    if not content.startswith(prefix):
        return None
    git_dir = Path(content[len(prefix) :])
    if not git_dir.is_absolute():
        git_dir = (worktree_path / git_dir).resolve()
    return git_dir


def record_parent_branch(worktree_path: Path, parent_branch: str) -> None:
    """Persist the parent branch for a worktree in its private git dir.

    Best-effort: failures are swallowed so worktree creation isn't blocked
    by metadata IO. Stored alongside git's per-worktree state so removal of
    the worktree cleans it up automatically.

    No-op for the main worktree: its `.git` is the shared repo dir, not a
    per-worktree dir, so a file written there wouldn't be auto-cleaned and
    would leak across worktrees.
    """
    if _is_main_worktree(worktree_path):
        return
    try:
        git_dir = _worktree_git_dir(worktree_path)
        if git_dir is None:
            return
        (git_dir / _PARENT_BRANCH_FILE).write_text(parent_branch + "\n")
    except OSError as e:
        log.debug("Failed to record parent branch for %s: %s", worktree_path, e)


def read_parent_branch(worktree_path: Path) -> str | None:
    """Read the recorded parent branch for a worktree, if any."""
    try:
        git_dir = _worktree_git_dir(worktree_path)
        if git_dir is None:
            return None
        f = git_dir / _PARENT_BRANCH_FILE
        if not f.exists():
            return None
        value = f.read_text().strip()
        return value or None
    except OSError:
        return None


def _current_branch(cwd: Path) -> str | None:
    """Return the current branch name at cwd, or None if detached/error."""
    result = subprocess.run(
        ["git", "symbolic-ref", "--short", "HEAD"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    name = result.stdout.strip()
    return name or None


def get_parent_branch(branch: str, cwd: Path | None = None) -> str | None:
    """Find the branch that the given branch was forked from.

    Returns the branch whose tip is an ancestor of our branch and is closest
    to our branch tip. This handles nested worktrees correctly.

    Note: this is a best-effort heuristic used as a fallback. Worktrees
    created via `start_worktree` record their parent explicitly — see
    `record_parent_branch` / `read_parent_branch`. The heuristic is
    ambiguous when sibling worktrees share a tip commit (e.g. a fresh
    sibling that hasn't diverged yet looks identical to the real parent),
    so prefer the recorded value when available.
    """
    worktrees = list_worktrees()
    other_branches = [wt.branch for wt in worktrees if wt.branch != branch]

    if not other_branches:
        return None

    # Find which branch is an ancestor and closest to our tip
    best_branch = None
    best_distance = float("inf")

    for candidate in other_branches:
        # Check if candidate is ancestor of our branch
        result = subprocess.run(
            ["git", "merge-base", "--is-ancestor", candidate, branch],
            cwd=cwd,
            capture_output=True,
        )
        if result.returncode != 0:
            continue  # Not an ancestor

        # Count commits from candidate to our branch
        result = subprocess.run(
            ["git", "rev-list", "--count", f"{candidate}..{branch}"],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            continue

        distance = int(result.stdout.strip())
        if distance < best_distance:
            best_distance = distance
            best_branch = candidate

    return best_branch


def _expand_worktree_path(template: str, repo_name: str, feature_name: str) -> Path:
    """Expand template variables in worktree path.

    Supports:
    - ${repo_name}: Repository name
    - ${branch_name}: Feature/branch name
    - $HOME: Home directory
    - ~: Home directory (via expanduser)

    Args:
        template: Path template string with variables
        repo_name: Name of the repository
        feature_name: Name of the feature/branch

    Returns:
        Expanded Path object

    Raises:
        ValueError: If expanded path is not absolute or contains path traversal patterns
    """
    if not repo_name or not repo_name.strip():
        raise ValueError("Repository name cannot be empty")
    if not feature_name or not feature_name.strip():
        raise ValueError("Feature name cannot be empty")

    expanded = (
        template.replace("${repo_name}", repo_name)
        .replace("${branch_name}", feature_name)
        .replace("$HOME", str(Path.home()))
    )

    path = Path(expanded).expanduser()

    if ".." in path.parts:
        raise ValueError(f"Worktree path contains path traversal component: {path}")

    if not path.is_absolute():
        raise ValueError(
            f"Worktree path template must expand to an absolute path, got: {path}"
        )

    return path.resolve()


def start_worktree(
    feature_name: str,
    base: str | None = None,
    parent_cwd: Path | None = None,
) -> tuple[bool, str, Path | None]:
    """Create a worktree for the given feature.

    When `base` is given, git runs with cwd=main worktree so the ref resolves
    deterministically regardless of caller cwd. When `base` is None, HEAD is
    resolved from the process cwd (legacy `/worktree` behavior).

    `parent_cwd`, if given, is used to determine the parent branch to record
    on the new worktree (used by `/worktree finish` to pick the merge target
    unambiguously). When `base` is given it's recorded directly. When neither
    is given we fall back to the current process cwd's branch.

    Returns (success, message, worktree_path).
    """
    if feature_name.startswith("-"):
        return False, "Invalid feature name: must not start with '-'", None

    try:
        main_wt = get_main_worktree()

        # Prefer the main worktree's dir name over `git rev-parse --show-toplevel`
        # so we don't inherit "repo-feature-a" when the caller happens to be in
        # a feature worktree (same cwd-drift family as the base-ref bug).
        repo_name = main_wt[0].name if main_wt else get_repo_name()
        path_template = CONFIG.get("worktree", {}).get("path_template")

        if path_template:
            try:
                worktree_dir = _expand_worktree_path(
                    path_template, repo_name, feature_name
                )
                worktree_dir.parent.mkdir(parents=True, exist_ok=True)
            except ValueError as e:
                return False, str(e), None
        else:
            parent_dir = main_wt[0].parent if main_wt else Path.cwd().parent
            worktree_dir = parent_dir / f"{repo_name}-{feature_name}"

        if worktree_dir.exists():
            return False, f"Directory {worktree_dir} already exists", None

        # When a base is given, the caller wants deterministic ref resolution;
        # silently falling back to cwd-of-process would re-introduce the bug.
        if base and not main_wt:
            return (
                False,
                "Cannot resolve base ref: main worktree not found",
                None,
            )
        base_ref = base or "HEAD"
        git_cwd: Path | None = main_wt[0] if base and main_wt else None

        subprocess.run(
            [
                "git",
                "worktree",
                "add",
                "-b",
                feature_name,
                str(worktree_dir),
                base_ref,
            ],
            cwd=git_cwd,
            check=True,
            capture_output=True,
            text=True,
        )

        # Symlink .claude/ from main worktree so hooks, skills, and
        # local settings carry over (they're typically gitignored).
        if main_wt:
            source_claude_dir = main_wt[0] / ".claude"
            if source_claude_dir.is_dir():
                target = worktree_dir / ".claude"
                if not target.exists():
                    target.symlink_to(source_claude_dir.resolve())

        # Record the parent branch so /worktree finish can pick the right
        # merge target without guessing from commit topology. Prefer an
        # explicit `base` (already a branch name); otherwise read the
        # current branch from `parent_cwd`, falling back to process cwd.
        # `base="HEAD"` is treated as "no explicit base" since it's just
        # a placeholder for cwd resolution.
        recorded_parent: str | None = None
        if base and base.upper() != "HEAD":
            recorded_parent = base
        else:
            recorded_parent = _current_branch(parent_cwd or Path.cwd())
        if recorded_parent:
            record_parent_branch(worktree_dir, recorded_parent)

        return True, f"Created worktree at {worktree_dir}", worktree_dir

    except subprocess.CalledProcessError as e:
        return False, f"Git error: {e.stderr}", None
    except Exception as e:
        return False, f"Error: {e}", None


def get_finish_info(cwd: Path | None = None) -> tuple[bool, str, FinishInfo | None]:
    """Get info needed to finish a worktree.

    Args:
        cwd: Current working directory (SDK's cwd). If None, uses Path.cwd().

    Returns (success, message, FinishInfo or None).

    The base_branch is determined by finding which branch this worktree was
    forked from, allowing nested worktrees to merge back to their parent
    rather than always going to main.
    """
    if cwd is None:
        cwd = Path.cwd()
    cwd = cwd.resolve()
    worktrees = list_worktrees()
    current_wt = next((wt for wt in worktrees if wt.path.resolve() == cwd), None)

    if current_wt is None or current_wt.is_main:
        return False, "Not in a feature worktree. Switch to a worktree first.", None

    main_wt = get_main_worktree()
    if main_wt is None:
        return False, "Cannot find main worktree.", None

    main_dir = main_wt[0]

    # Prefer the parent branch recorded at worktree-creation time. The
    # commit-topology heuristic (get_parent_branch) is ambiguous when a
    # sibling worktree shares a tip with the real parent, and can route
    # the merge into the wrong worktree.
    #
    # Validation: the recorded branch must currently have a checked-out
    # worktree. If only the branch ref exists (worktree was removed), we
    # have nowhere safe to merge — `parent_dir` would fall back to the
    # main worktree, which has *main* checked out, and the merge would
    # silently land on main instead of the recorded parent.
    parent_branch = read_parent_branch(current_wt.path)
    if parent_branch:
        has_worktree = any(wt.branch == parent_branch for wt in worktrees)
        if not has_worktree:
            log.debug(
                "Recorded parent branch %r for worktree %s has no checked-out "
                "worktree; falling back to topology heuristic.",
                parent_branch,
                current_wt.path,
            )
            parent_branch = None
    if parent_branch is None:
        parent_branch = get_parent_branch(current_wt.branch, cwd=cwd)
        if parent_branch is not None:
            log.debug(
                "No recorded parent for worktree %s; topology heuristic "
                "selected %r (may be ambiguous if siblings share a tip).",
                current_wt.path,
                parent_branch,
            )
    if parent_branch is None:
        # Fallback to main branch
        parent_branch = main_wt[1]

    # Find the directory for the parent branch
    parent_wt = next((wt for wt in worktrees if wt.branch == parent_branch), None)
    parent_dir = parent_wt.path if parent_wt else main_dir

    return (
        True,
        "Ready to finish worktree",
        FinishInfo(
            branch_name=current_wt.branch,
            base_branch=parent_branch,
            worktree_dir=current_wt.path,
            main_dir=parent_dir,
        ),
    )


def diagnose_worktree(info: FinishInfo) -> WorktreeStatus:
    """Gather complete pre-flight status for a worktree.

    All operations are git commands - no Claude involvement.
    """
    cwd = info.worktree_dir

    # Commits ahead of base
    result = subprocess.run(
        ["git", "rev-list", "--count", f"{info.base_branch}..{info.branch_name}"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    commits_ahead = int(result.stdout.strip()) if result.returncode == 0 else 0

    # Already merged?
    is_merged = is_branch_merged(info.branch_name, info.base_branch, cwd=info.main_dir)

    # Can fast-forward? (only relevant if there are commits)
    can_ff = not needs_rebase(info) if commits_ahead > 0 else True

    # Uncommitted changes (staged + unstaged)
    result = subprocess.run(
        ["git", "status", "--porcelain"], cwd=cwd, capture_output=True, text=True
    )
    uncommitted = []
    for line in result.stdout.strip().split("\n"):
        if line and not line.startswith("??"):  # Exclude untracked
            uncommitted.append(line[3:])  # Strip status prefix

    # Categorize untracked files
    untracked_gitignored, untracked_other = get_untracked_files(cwd)

    return WorktreeStatus(
        commits_ahead=commits_ahead,
        is_merged=is_merged,
        can_fast_forward=can_ff,
        uncommitted_files=uncommitted,
        untracked_gitignored=untracked_gitignored,
        untracked_other=untracked_other,
    )


def get_untracked_files(worktree_dir: Path) -> tuple[list[str], list[str]]:
    """Categorize untracked files into gitignored and non-ignored.

    Returns (gitignored, non_ignored).
    Uses git clean -n which respects .gitignore.
    """
    # Files that would be removed by git clean -fdX (ignored only)
    result = subprocess.run(
        ["git", "clean", "-fdXn"], cwd=worktree_dir, capture_output=True, text=True
    )
    ignored_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
    # Parse "Would remove X" format
    ignored = [line.replace("Would remove ", "") for line in ignored_lines if line]

    # Files that would be removed by git clean -fd (all untracked)
    result = subprocess.run(
        ["git", "clean", "-fdn"], cwd=worktree_dir, capture_output=True, text=True
    )
    all_lines = result.stdout.strip().split("\n") if result.stdout.strip() else []
    all_untracked = [line.replace("Would remove ", "") for line in all_lines if line]

    # Non-ignored = all untracked minus ignored
    ignored_set = set(ignored)
    non_ignored = [f for f in all_untracked if f not in ignored_set]

    return ignored, non_ignored


def determine_resolution_action(status: WorktreeStatus) -> ResolutionAction:
    """Determine what resolution action is needed based on status."""
    # No commits and clean? Nothing to do
    if status.commits_ahead == 0 and status.is_clean:
        return ResolutionAction.NONE

    # Already merged and clean? Just cleanup
    if status.is_merged and status.is_clean:
        return ResolutionAction.NONE

    # Only gitignored untracked files? Clean them first
    if status.only_gitignored_untracked:
        return ResolutionAction.CLEAN_GITIGNORED

    # Has uncommitted changes or non-ignored untracked? Ask user
    if status.has_uncommitted or status.untracked_other:
        return ResolutionAction.PROMPT_UNCOMMITTED

    # Already merged (other issues handled above)
    if status.is_merged:
        return ResolutionAction.NONE

    # Can fast-forward merge?
    if status.can_fast_forward:
        return ResolutionAction.FAST_FORWARD

    # Need rebase (Claude handles this)
    return ResolutionAction.REBASE


def clean_gitignored_files(worktree_dir: Path) -> tuple[bool, str]:
    """Remove gitignored untracked files. Returns (success, error)."""
    result = subprocess.run(
        ["git", "clean", "-fdX"], cwd=worktree_dir, capture_output=True, text=True
    )
    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, ""


def discard_all_changes(worktree_dir: Path) -> tuple[bool, str]:
    """Discard all uncommitted changes and untracked files.

    Returns (success, error).
    """
    # Reset staged and unstaged changes
    result = subprocess.run(
        ["git", "checkout", "."], cwd=worktree_dir, capture_output=True, text=True
    )
    if result.returncode != 0:
        return False, f"checkout failed: {result.stderr.strip()}"

    # Remove all untracked files
    result = subprocess.run(
        ["git", "clean", "-fd"], cwd=worktree_dir, capture_output=True, text=True
    )
    if result.returncode != 0:
        return False, f"clean failed: {result.stderr.strip()}"

    return True, ""


def needs_rebase(info: FinishInfo) -> bool:
    """Check if the feature branch needs rebasing onto the base branch.

    Returns False if the base branch is an ancestor of the feature branch
    (fast-forward merge possible). Returns True if rebase is needed.
    """
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", info.base_branch, info.branch_name],
        cwd=info.worktree_dir,
        capture_output=True,
    )
    # Exit 0 means base_branch IS an ancestor of branch_name (no rebase needed)
    # Exit 1 means it's NOT an ancestor (rebase needed)
    return result.returncode != 0


def fast_forward_merge(info: FinishInfo) -> tuple[bool, str]:
    """Perform a fast-forward merge when no rebase is needed.

    Returns (success, error_message).
    """
    # Check for uncommitted changes first
    if has_uncommitted_changes(info.worktree_dir):
        return False, "Uncommitted changes in worktree"

    # Do the merge in main dir
    result = subprocess.run(
        ["git", "merge", "--ff-only", info.branch_name],
        cwd=info.main_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip()

    return True, ""


def get_finish_prompt(info: FinishInfo) -> str:
    """Generate the prompt for Claude to rebase and merge a feature branch."""
    return f"""Rebase and merge this feature branch:

Branch: {info.branch_name}
Base branch: {info.base_branch}
Worktree dir: {info.worktree_dir}
Main dir: {info.main_dir}

Steps:
1. Check for uncommitted changes in the worktree (fail if any)
2. Rebase {info.branch_name} onto the LOCAL {info.base_branch} branch (do NOT fetch from remote):
   git rebase {info.base_branch}
3. In the main dir ({info.main_dir}), merge {info.branch_name}:
   cd {info.main_dir} && git merge {info.branch_name}

Do NOT remove the worktree or delete the branch - the app will handle cleanup.
Do NOT interact with remotes (no fetch, no pull, no push)."""


def get_cleanup_fix_prompt(error: str, worktree_dir: Path) -> str:
    """Generate prompt for Claude to fix a cleanup failure."""
    # Get list of files in the worktree for context
    file_list = ""
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            file_list = f"\n\nGit status:\n{result.stdout}"

        # Also list untracked files not in .gitignore
        result = subprocess.run(
            ["git", "clean", "-n", "-d"],
            cwd=worktree_dir,
            capture_output=True,
            text=True,
        )
        if result.stdout.strip():
            file_list += f"\n\nUntracked files that would be removed by git clean:\n{result.stdout}"
    except Exception:
        pass

    return f"""The worktree cleanup failed with this error:

{error}

Worktree dir: {worktree_dir}{file_list}

You MUST take action to fix this. The cleanup will be retried after you respond.

If the error mentions untracked files or "contains modified or untracked files":
- List the files with `ls {worktree_dir}` or `git status`
- Determine if they are important (user work) or disposable (build artifacts, __pycache__, etc.)
- For disposable files: `rm -rf {worktree_dir}/<file>` or `git clean -fd` in the worktree
- For important files: commit them first

If the error mentions branch not merged:
- Merge the branch: `git merge <branch>` in the main worktree

Do NOT just describe what should be done - actually do it."""


def finish_cleanup(info: FinishInfo) -> tuple[bool, str]:
    """Attempt to clean up a finished worktree.

    Returns (success, error_message). On success, error_message is empty.
    Only succeeds if branch is fully merged - never destroys unmerged work.
    """
    # Check branch is merged BEFORE removing anything (run from main_dir for correct refs)
    if not is_branch_merged(info.branch_name, info.base_branch, cwd=info.main_dir):
        return (
            False,
            f"Branch '{info.branch_name}' is not merged into '{info.base_branch}'",
        )

    # Try worktree removal
    result = subprocess.run(
        ["git", "worktree", "remove", str(info.worktree_dir)],
        cwd=info.main_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, result.stderr.strip()

    # Delete branch (should succeed since we verified it's merged)
    result = subprocess.run(
        ["git", "branch", "-d", info.branch_name],
        cwd=info.main_dir,
        capture_output=True,
        text=True,
    )
    branch_warning = (
        ""
        if result.returncode == 0
        else f" (branch not deleted: {result.stderr.strip()})"
    )

    return True, branch_warning


def has_uncommitted_changes(worktree_path: Path) -> bool:
    """Check if a worktree has uncommitted changes.

    Returns False if the worktree path is missing (prunable/stale ref).
    """
    if not worktree_path.exists():
        return False
    result = subprocess.run(
        ["git", "-C", str(worktree_path), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    )
    return bool(result.stdout.strip())


def is_branch_merged(
    branch: str, into_branch: str = "main", cwd: Path | None = None
) -> bool:
    """Check if branch is merged into another branch."""
    result = subprocess.run(
        ["git", "branch", "--merged", into_branch],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    merged = [b.strip().lstrip("*+ ") for b in result.stdout.strip().split("\n")]
    return branch in merged


def remove_worktree(worktree: WorktreeInfo, force: bool = False) -> tuple[bool, str]:
    """Remove a worktree and its branch. Returns (success, message)."""
    try:
        if not worktree.path.exists():
            # Stale worktree (path deleted externally) - prune to clear the ref
            subprocess.run(
                ["git", "worktree", "prune"],
                check=True,
                capture_output=True,
                text=True,
            )
        else:
            cmd = ["git", "worktree", "remove", str(worktree.path)]
            if force:
                cmd.append("--force")
            subprocess.run(cmd, check=True, capture_output=True, text=True)

        delete_flag = "-D" if force else "-d"
        subprocess.run(
            ["git", "branch", delete_flag, worktree.branch],
            check=True,
            capture_output=True,
            text=True,
        )
        return True, f"Removed {worktree.branch}"
    except subprocess.CalledProcessError as e:
        return False, f"Failed to remove {worktree.branch}: {e.stderr}"


def cleanup_worktrees(
    branches: list[str] | None = None,
) -> list[tuple[str, bool, str, bool]]:
    """Clean up worktrees.

    Args:
        branches: Specific branches to remove. If None, removes all safe worktrees.

    Returns:
        List of (branch_name, success, message, needs_confirmation).
        needs_confirmation=True means the branch has changes or is unmerged.
    """
    worktrees = list_worktrees()
    main_wt = get_main_worktree()
    main_dir = main_wt[0] if main_wt else None
    main_branch = main_wt[1] if main_wt else "main"

    if branches is None:
        branches = [wt.branch for wt in worktrees if not wt.is_main]

    results = []
    for branch in branches:
        wt = next((w for w in worktrees if w.branch == branch), None)
        if wt is None:
            results.append((branch, False, f"No worktree for branch '{branch}'", False))
            continue
        if wt.is_main:
            results.append((branch, False, "Cannot remove main worktree", False))
            continue

        merged = is_branch_merged(branch, main_branch, cwd=main_dir)
        dirty = has_uncommitted_changes(wt.path)

        if dirty or not merged:
            reason = []
            if dirty:
                reason.append("has uncommitted changes")
            if not merged:
                reason.append("not merged")
            results.append((branch, False, ", ".join(reason), True))
        else:
            success, msg = remove_worktree(wt)
            results.append((branch, success, msg, False))

    return results
