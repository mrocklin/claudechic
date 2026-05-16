"""Tests for /worktree finish parent-tracking.

Regression coverage for: when sibling worktrees share a tip commit, the
commit-topology heuristic in `get_parent_branch` ties and picks whoever
git happens to list first — which can route a merge into the wrong
sibling worktree. The fix records the parent at creation time.
"""

from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from claudechic.features.worktree.git import (
    FinishInfo,
    cleanup_worktrees,
    discard_all_changes,
    get_finish_info,
    get_finish_prompt,
    get_parent_branch,
    has_uncommitted_changes,
    read_parent_branch,
    record_parent_branch,
    start_worktree,
)


def _git(cwd: Path, *args: str) -> str:
    """Run git in cwd; return stdout stripped."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    """Init a real git repo with a single commit on `main`. Yields the path."""
    repo_path = tmp_path / "main-repo"
    repo_path.mkdir()
    _git(repo_path, "init", "-b", "main")
    _git(repo_path, "config", "user.email", "test@test")
    _git(repo_path, "config", "user.name", "test")
    (repo_path / "a").write_text("a")
    _git(repo_path, "add", "a")
    _git(repo_path, "commit", "-m", "first")
    yield repo_path


@pytest.fixture
def patched_main(repo, monkeypatch):
    """Patch get_main_worktree + CONFIG so start_worktree works on `repo`.

    Also chdir into `repo` so `list_worktrees` (which uses process cwd)
    sees the test repo's worktrees.
    """
    monkeypatch.chdir(repo)
    with (
        patch("claudechic.features.worktree.git.CONFIG") as cfg,
        patch(
            "claudechic.features.worktree.git.get_main_worktree",
            return_value=(repo, "main"),
        ),
    ):
        cfg.get.return_value = {}
        yield repo


def test_record_and_read_parent_roundtrip(repo, tmp_path):
    """record_parent_branch persists, read_parent_branch returns it."""
    wt_path = tmp_path / "wts" / "feat"
    _git(repo, "worktree", "add", "-b", "feat", str(wt_path), "main")

    assert read_parent_branch(wt_path) is None
    record_parent_branch(wt_path, "main")
    assert read_parent_branch(wt_path) == "main"


@pytest.mark.usefixtures("patched_main")
def test_start_worktree_records_parent_from_base(tmp_path):
    """When `base` is given, it's recorded as the parent."""
    template = f"{tmp_path}/wts/${{repo_name}}/${{branch_name}}"
    with patch("claudechic.features.worktree.git.CONFIG") as cfg:
        cfg.get.return_value = {"path_template": template}
        ok, _, wt_path = start_worktree("feat-a", base="main")
    assert ok and wt_path is not None
    assert read_parent_branch(wt_path) == "main"


@pytest.mark.usefixtures("patched_main")
def test_start_worktree_records_parent_from_parent_cwd(tmp_path):
    """When `parent_cwd` points into a feature worktree, that branch is
    recorded — this is the multi-agent case where a user is in feature-a
    and creates feature-b from it."""
    # Create feature-a first, with main as parent.
    template = f"{tmp_path}/wts/${{repo_name}}/${{branch_name}}"
    with patch("claudechic.features.worktree.git.CONFIG") as cfg:
        cfg.get.return_value = {"path_template": template}
        ok, _, wt_a = start_worktree("feat-a", base="main")
    assert ok and wt_a is not None

    # Now create feature-b "from" feature-a (no explicit base, just cwd).
    with patch("claudechic.features.worktree.git.CONFIG") as cfg:
        cfg.get.return_value = {"path_template": template}
        ok, _, wt_b = start_worktree("feat-b", parent_cwd=wt_a)
    assert ok and wt_b is not None
    assert read_parent_branch(wt_b) == "feat-a"


@pytest.mark.usefixtures("patched_main")
def test_get_finish_info_uses_recorded_parent_over_sibling(tmp_path):
    """The bug scenario: feature-b's real parent is feature-a, but a sibling
    feature-c shares feature-a's tip. Without recorded parent, the heuristic
    can pick feature-c. With recorded parent, feature-a always wins.
    """
    template = f"{tmp_path}/wts/${{repo_name}}/${{branch_name}}"

    with patch("claudechic.features.worktree.git.CONFIG") as cfg:
        cfg.get.return_value = {"path_template": template}
        # feat-a forks from main.
        ok, _, wt_a = start_worktree("feat-a", base="main")
        assert ok and wt_a is not None
        # feat-b forks from feat-a.
        ok, _, wt_b = start_worktree("feat-b", parent_cwd=wt_a)
        assert ok and wt_b is not None
        # feat-c is a sibling of feat-b, also forked from feat-a.
        # It hasn't diverged yet — same tip as feat-a.
        ok, _, wt_c = start_worktree("feat-c", parent_cwd=wt_a)
        assert ok and wt_c is not None

    # Add a commit to feat-b so it has something to merge.
    (wt_b / "b").write_text("b")
    _git(wt_b, "add", "b")
    _git(wt_b, "commit", "-m", "feat-b commit")

    # feat-a, feat-c, and main are all at the same commit (the original
    # `first` commit). The heuristic alone is ambiguous here — it ties on
    # commit-distance and picks whoever git lists first. Demonstrate the
    # ambiguity exists so the regression's value is self-evident: any of
    # the three is "valid" by the heuristic's lights.
    assert _git(wt_a, "rev-parse", "HEAD") == _git(wt_c, "rev-parse", "HEAD")
    heuristic_pick = get_parent_branch("feat-b", cwd=wt_b)
    assert heuristic_pick in {"feat-a", "feat-c", "main"}

    # The recorded parent disambiguates and always picks feat-a.
    success, _, info = get_finish_info(wt_b)
    assert success and info is not None
    assert info.base_branch == "feat-a"
    assert info.main_dir == wt_a
    assert info.worktree_dir == wt_b


def test_get_finish_info_falls_back_to_inference_when_no_record(patched_main, tmp_path):
    """Worktrees created before this change have no recorded parent. The
    inference path must still work for them."""
    repo = patched_main
    wt = tmp_path / "wts" / "legacy"
    _git(repo, "worktree", "add", "-b", "legacy", str(wt), "main")
    # Add a divergent commit so it's a clear ancestor relationship.
    (wt / "x").write_text("x")
    _git(wt, "add", "x")
    _git(wt, "commit", "-m", "legacy commit")

    assert read_parent_branch(wt) is None  # no record

    success, _, info = get_finish_info(wt)
    assert success and info is not None
    assert info.base_branch == "main"
    assert info.main_dir == repo


def test_get_finish_info_ignores_stale_record_for_deleted_branch(
    patched_main, tmp_path
):
    """If the recorded parent branch was deleted, fall back to inference
    rather than blindly trusting the stale value."""
    repo = patched_main
    wt = tmp_path / "wts" / "feat"
    _git(repo, "worktree", "add", "-b", "feat", str(wt), "main")
    record_parent_branch(wt, "branch-that-does-not-exist")

    # Add a divergent commit so inference can find `main` as the parent.
    (wt / "x").write_text("x")
    _git(wt, "add", "x")
    _git(wt, "commit", "-m", "feat commit")

    success, _, info = get_finish_info(wt)
    assert success and info is not None
    assert info.base_branch == "main"


def test_get_finish_info_ignores_stale_record_when_parent_worktree_gone(
    patched_main, tmp_path
):
    """If the recorded parent branch still exists but its worktree was
    removed, fall back to inference. Without this check, `parent_dir`
    would point at the main worktree (which has `main` checked out) and
    `git merge` would silently land on main instead of the recorded
    parent.
    """
    repo = patched_main
    # Create feat-a with main as parent, then feat-b with feat-a as parent.
    template = f"{tmp_path}/wts/${{repo_name}}/${{branch_name}}"
    with patch("claudechic.features.worktree.git.CONFIG") as cfg:
        cfg.get.return_value = {"path_template": template}
        ok, _, wt_a = start_worktree("feat-a", base="main")
        assert ok and wt_a is not None
        ok, _, wt_b = start_worktree("feat-b", parent_cwd=wt_a)
        assert ok and wt_b is not None

    # Add a commit to feat-b so inference has something to work with.
    (wt_b / "x").write_text("x")
    _git(wt_b, "add", "x")
    _git(wt_b, "commit", "-m", "feat-b commit")

    # Remove feat-a's worktree, but keep its branch ref.
    _git(repo, "worktree", "remove", str(wt_a))
    assert read_parent_branch(wt_b) == "feat-a"  # record still says feat-a

    success, _, info = get_finish_info(wt_b)
    assert success and info is not None
    # Without the fix, base_branch would stay "feat-a" while main_dir
    # silently became `repo`, and the merge would land on main.
    assert info.base_branch == "main"
    assert info.main_dir == repo


# --- discard_all_changes ---
# Must clobber both staged AND unstaged changes plus untracked files.
# The earlier `git checkout .` only restored unstaged tracked files, so a
# staged change survived. `repo` fixture provides committed file `a`.


def test_discard_reverts_unstaged_modification(repo):
    (repo / "a").write_text("dirty")
    ok, err = discard_all_changes(repo)
    assert ok, err
    assert (repo / "a").read_text() == "a"
    assert _git(repo, "status", "--porcelain") == ""


def test_discard_drops_staged_new_file(repo):
    # The bug: `git checkout .` leaves this in the index.
    (repo / "staged.txt").write_text("new")
    _git(repo, "add", "staged.txt")
    assert _git(repo, "status", "--porcelain") == "A  staged.txt"

    ok, err = discard_all_changes(repo)
    assert ok, err
    assert not (repo / "staged.txt").exists()
    assert _git(repo, "status", "--porcelain") == ""


def test_discard_clears_mixed_staged_unstaged_and_untracked(repo):
    (repo / "a").write_text("dirty")  # unstaged modification
    (repo / "staged.txt").write_text("new")
    _git(repo, "add", "staged.txt")  # staged new file
    (repo / "untracked.txt").write_text("u")  # untracked

    ok, err = discard_all_changes(repo)
    assert ok, err
    assert (repo / "a").read_text() == "a"
    assert not (repo / "staged.txt").exists()
    assert not (repo / "untracked.txt").exists()
    assert _git(repo, "status", "--porcelain") == ""


# --- get_finish_prompt injection guard ---
# Every interpolated value must round-trip through `shlex.split` as a single
# token. Otherwise a hostile branch name injects when Claude pastes the
# rebase/merge command into a shell.

HOSTILE_STRINGS = [
    "; rm -rf $HOME",
    "$(touch /tmp/pwned)",
    "`id`",
    "a && echo bad",
    "a' '; echo bad",
    "branch with spaces",
]


@pytest.mark.parametrize("hostile", HOSTILE_STRINGS)
@pytest.mark.parametrize("field", ["branch_name", "base_branch", "main_dir"])
def test_finish_prompt_injection_stays_single_token(tmp_path, field, hostile):
    kwargs: dict = {
        "branch_name": "feat",
        "base_branch": "main",
        "worktree_dir": tmp_path / "wt",
        "main_dir": tmp_path / "main",
    }
    kwargs[field] = Path(hostile) if field.endswith("_dir") else hostile
    info = FinishInfo(**kwargs)
    tokens = shlex.split(get_finish_prompt(info))
    # `&&` and other template metacharacters legitimately appear; we only
    # care that the injected value survives as one token.
    assert hostile in tokens, f"{hostile!r} not preserved; tokens={tokens!r}"


# --- start_worktree input validation ---
# Both `feature_name` and `base` flow straight onto a `git worktree add`
# command line. Empty or dash-prefixed values must be rejected.


@pytest.mark.parametrize(
    "kwargs, expected",
    [
        ({"feature_name": "--evil"}, "must not start with"),
        ({"feature_name": "safe", "base": "--evil"}, "must not start with"),
        ({"feature_name": "   "}, "must not be empty"),
        ({"feature_name": "safe", "base": "   "}, "must not be empty"),
    ],
)
def test_start_worktree_rejects_bad_input(patched_main, kwargs, expected):
    ok, msg, path = start_worktree(parent_cwd=patched_main, **kwargs)
    assert not ok
    assert path is None
    assert expected in msg


def test_cleanup_handles_stale_worktree_path(patched_main, tmp_path):
    """If a worktree's path has been deleted externally (e.g. pytest tmp
    cleanup), `/worktree cleanup` should prune the stale ref instead of
    crashing with CalledProcessError from `git status`.
    """
    import shutil

    repo = patched_main
    template = f"{tmp_path}/wts/${{repo_name}}/${{branch_name}}"
    with patch("claudechic.features.worktree.git.CONFIG") as cfg:
        cfg.get.return_value = {"path_template": template}
        ok, _, wt_path = start_worktree("feat-stale", base="main")
        assert ok and wt_path is not None

    # Simulate external deletion of the worktree directory.
    shutil.rmtree(wt_path)
    assert not wt_path.exists()

    # Probe: should not raise on missing path.
    assert has_uncommitted_changes(wt_path) is False

    # Cleanup should succeed via prune (branch is merged since no commits added).
    results = cleanup_worktrees(["feat-stale"])
    assert len(results) == 1
    branch, success, msg, _ = results[0]
    assert branch == "feat-stale"
    assert success, msg
    # Stale ref is gone.
    assert "feat-stale" not in _git(repo, "worktree", "list")


@pytest.mark.usefixtures("patched_main")
class TestStartWorktreeInjectionGuard:
    def test_feature_name_dash_prefix_returns_error(self, repo):
        ok, msg, _ = start_worktree("--evil", parent_cwd=repo)
        assert not ok
        assert "must not start with" in msg.lower() or "invalid" in msg.lower()
