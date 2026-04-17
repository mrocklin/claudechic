"""Tests for worktree path template expansion."""

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from claudechic.features.worktree.git import _expand_worktree_path, start_worktree


@pytest.fixture
def mock_worktree_deps():
    """Mock all external dependencies for start_worktree tests."""
    with (
        patch("claudechic.features.worktree.git.CONFIG") as mock_config,
        patch("claudechic.features.worktree.git.get_main_worktree") as mock_get_main,
        patch("claudechic.features.worktree.git.get_repo_name") as mock_get_repo,
        patch("claudechic.features.worktree.git.subprocess.run") as mock_run,
    ):
        yield {
            "config": mock_config,
            "get_main": mock_get_main,
            "get_repo": mock_get_repo,
            "run": mock_run,
        }


class TestWorktreePathTemplate:
    """Test worktree path template expansion."""

    @pytest.mark.parametrize(
        "template,expected",
        [
            ("/tmp/worktrees/${repo_name}", Path("/tmp/worktrees/my-repo")),
            ("/tmp/worktrees/${branch_name}", Path("/tmp/worktrees/test-feature")),
            ("$HOME/worktrees/test", Path.home() / "worktrees" / "test"),
            ("~/worktrees/test", Path.home() / "worktrees" / "test"),
        ],
    )
    def test_template_variable_expansion(self, template, expected):
        """Test template variable expansion for various variables."""
        result = _expand_worktree_path(template, "my-repo", "test-feature")
        assert result == expected.resolve()

    def test_expand_template_combined(self):
        """Test combined template with multiple variables."""
        result = _expand_worktree_path(
            "$HOME/code/worktrees/${repo_name}/${branch_name}",
            repo_name="my-repo",
            feature_name="test-feature",
        )
        expected = Path.home() / "code" / "worktrees" / "my-repo" / "test-feature"
        assert result == expected

    def test_expand_template_with_spaces_in_names(self):
        """Test handling of spaces in repo/branch names."""
        result = _expand_worktree_path(
            "/tmp/${repo_name}/${branch_name}",
            repo_name="my repo",
            feature_name="test feature",
        )
        assert result == Path("/tmp/my repo/test feature").resolve()

    def test_rejects_path_traversal_in_feature_name(self):
        """Test that path traversal in feature name is rejected."""
        with pytest.raises(ValueError, match="path traversal"):
            _expand_worktree_path(
                "/tmp/${repo_name}/${branch_name}",
                repo_name="my-repo",
                feature_name="../../etc/passwd",
            )

    def test_rejects_path_traversal_in_repo_name(self):
        """Test that path traversal in repo name is rejected."""
        with pytest.raises(ValueError, match="path traversal"):
            _expand_worktree_path(
                "/tmp/${repo_name}/${branch_name}",
                repo_name="../../../etc",
                feature_name="test-feature",
            )

    def test_rejects_relative_path_template(self):
        """Test that relative path templates are rejected."""
        with pytest.raises(ValueError, match="absolute path"):
            _expand_worktree_path(
                "relative/path/${branch_name}",
                repo_name="my-repo",
                feature_name="test-feature",
            )

    def test_rejects_path_traversal_in_template(self):
        """Test that path traversal in template itself is rejected."""
        with pytest.raises(ValueError, match="path traversal"):
            _expand_worktree_path(
                "/tmp/../../../etc/${branch_name}",
                repo_name="my-repo",
                feature_name="test-feature",
            )

    def test_rejects_empty_repo_name(self):
        """Test that empty repository name is rejected."""
        with pytest.raises(ValueError, match="Repository name cannot be empty"):
            _expand_worktree_path(
                "/tmp/${repo_name}/${branch_name}",
                repo_name="",
                feature_name="test-feature",
            )

    def test_rejects_empty_feature_name(self):
        """Test that empty feature name is rejected."""
        with pytest.raises(ValueError, match="Feature name cannot be empty"):
            _expand_worktree_path(
                "/tmp/${repo_name}/${branch_name}",
                repo_name="my-repo",
                feature_name="",
            )

    def test_rejects_whitespace_only_repo_name(self):
        """Test that whitespace-only repository name is rejected."""
        with pytest.raises(ValueError, match="Repository name cannot be empty"):
            _expand_worktree_path(
                "/tmp/${repo_name}/${branch_name}",
                repo_name="   ",
                feature_name="test-feature",
            )


class TestStartWorktreeWithConfig:
    """Test start_worktree() with path_template config."""

    def test_uses_custom_template_when_configured(self, mock_worktree_deps, tmp_path):
        """Test that custom path template is used when configured."""
        mocks = mock_worktree_deps
        mocks["get_repo"].return_value = "test-repo"
        mocks["get_main"].return_value = (Path("/original/test-repo"), "main")

        template = f"{tmp_path}/worktrees/${{repo_name}}/${{branch_name}}"
        mocks["config"].get.return_value = {"path_template": template}

        success, message, path = start_worktree("test-feature")

        expected_path = (
            tmp_path / "worktrees" / "test-repo" / "test-feature"
        ).resolve()
        assert success
        assert path == expected_path
        assert "Created worktree at" in message
        mocks["run"].assert_called_once()

    @pytest.mark.parametrize(
        "config_return",
        [
            {"path_template": None},
            {},
        ],
    )
    def test_uses_sibling_behavior_when_no_template(
        self, mock_worktree_deps, config_return
    ):
        """Test that sibling behavior is preserved when path_template is null or missing."""
        mocks = mock_worktree_deps
        mocks["get_repo"].return_value = "test-repo"
        main_worktree_path = Path("/original/test-repo")
        mocks["get_main"].return_value = (main_worktree_path, "main")
        mocks["config"].get.return_value = config_return

        success, message, path = start_worktree("test-feature")

        expected_path = Path("/original/test-repo-test-feature")
        assert success, f"Expected success but got failure: {message}"
        assert path == expected_path
        mocks["run"].assert_called_once()

    def test_creates_parent_directories_for_custom_path(
        self, mock_worktree_deps, tmp_path
    ):
        """Test that parent directories are created for custom paths."""
        mocks = mock_worktree_deps
        mocks["get_repo"].return_value = "test-repo"
        mocks["get_main"].return_value = (Path("/original/test-repo"), "main")

        template = f"{tmp_path}/deep/nested/path/${{repo_name}}/${{branch_name}}"
        mocks["config"].get.return_value = {"path_template": template}

        success, message, path = start_worktree("test-feature")

        expected_path = (
            tmp_path / "deep" / "nested" / "path" / "test-repo" / "test-feature"
        ).resolve()
        assert success
        assert path == expected_path
        assert expected_path.parent.exists()


class TestStartWorktreeBase:
    """Test start_worktree()'s `base` parameter for deterministic forking."""

    def test_default_base_uses_head_and_no_explicit_cwd(self, mock_worktree_deps):
        """Without a base, git resolves HEAD from the caller's cwd (legacy)."""
        mocks = mock_worktree_deps
        mocks["get_repo"].return_value = "test-repo"
        mocks["get_main"].return_value = (Path("/original/test-repo"), "main")
        mocks["config"].get.return_value = {}

        success, _, _ = start_worktree("wt-a")

        assert success
        call = mocks["run"].call_args
        args = call.args[0]
        assert args[-1] == "HEAD"
        assert call.kwargs.get("cwd") is None

    def test_explicit_base_is_passed_to_git(self, mock_worktree_deps):
        """With a base, it becomes the final arg to `git worktree add`."""
        mocks = mock_worktree_deps
        mocks["get_repo"].return_value = "test-repo"
        mocks["get_main"].return_value = (Path("/original/test-repo"), "main")
        mocks["config"].get.return_value = {}

        success, _, _ = start_worktree("wt-a", base="main")

        assert success
        args = mocks["run"].call_args.args[0]
        assert args[-1] == "main"

    def test_explicit_base_anchors_cwd_to_main_worktree(self, mock_worktree_deps):
        """With a base, git is run with cwd=main worktree so refs resolve
        deterministically across parallel spawns."""
        mocks = mock_worktree_deps
        mocks["get_repo"].return_value = "test-repo"
        main_path = Path("/original/test-repo")
        mocks["get_main"].return_value = (main_path, "main")
        mocks["config"].get.return_value = {}

        success, _, _ = start_worktree("wt-a", base="main")

        assert success
        assert mocks["run"].call_args.kwargs.get("cwd") == main_path

    def test_parallel_spawns_all_fork_from_same_base(self, mock_worktree_deps):
        """Two sibling worktrees both fork from the given base, not from each
        other — regression test for the ancestor-chain bug."""
        mocks = mock_worktree_deps
        mocks["get_repo"].return_value = "test-repo"
        main_path = Path("/original/test-repo")
        mocks["get_main"].return_value = (main_path, "main")
        mocks["config"].get.return_value = {}

        start_worktree("wt-a", base="main")
        start_worktree("wt-b", base="main")

        assert mocks["run"].call_count == 2
        for call in mocks["run"].call_args_list:
            assert call.args[0][-1] == "main"
            assert call.kwargs.get("cwd") == main_path

    def test_explicit_base_without_main_worktree_fails_loudly(self, mock_worktree_deps):
        """If the caller asks for deterministic resolution but we can't find
        the main worktree, fail rather than silently falling back to cwd."""
        mocks = mock_worktree_deps
        mocks["get_repo"].return_value = "test-repo"
        mocks["get_main"].return_value = None
        mocks["config"].get.return_value = {}

        success, message, path = start_worktree("wt-a", base="main")

        assert not success
        assert path is None
        assert "main worktree not found" in message
        mocks["run"].assert_not_called()


def _run_git(cwd: Path, *args: str) -> str:
    """Run git in cwd; return stdout stripped."""
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


class TestStartWorktreeIntegration:
    """Real-git end-to-end tests that actually run `git worktree add`.

    Complements the mock-based tests: mocks prove the invocation shape;
    these prove the invocation actually produces the topology we want.
    """

    @pytest.fixture
    def repo(self, tmp_path):
        """Init a real git repo at tmp_path/main-repo with two commits.

        The second commit advances `main` past the detached tip of an older
        ref so we can tell "forked from main" apart from "forked from HEAD".
        Yields (repo_path, main_sha).
        """
        repo_path = tmp_path / "main-repo"
        repo_path.mkdir()
        _run_git(repo_path, "init", "-b", "main")
        _run_git(repo_path, "config", "user.email", "test@test")
        _run_git(repo_path, "config", "user.name", "test")
        (repo_path / "a").write_text("a")
        _run_git(repo_path, "add", "a")
        _run_git(repo_path, "commit", "-m", "first")
        (repo_path / "b").write_text("b")
        _run_git(repo_path, "add", "b")
        _run_git(repo_path, "commit", "-m", "second")
        main_sha = _run_git(repo_path, "rev-parse", "main")
        yield repo_path, main_sha

    def test_parallel_spawns_produce_siblings_off_main(self, repo, tmp_path):
        """Regression test for the ancestor-chain bug: two spawns with
        base='main' must both have main's tip as their parent, not each
        other's tip."""
        repo_path, main_sha = repo
        template = f"{tmp_path}/wts/${{repo_name}}/${{branch_name}}"

        with (
            patch("claudechic.features.worktree.git.CONFIG") as cfg,
            patch(
                "claudechic.features.worktree.git.get_main_worktree",
                return_value=(repo_path, "main"),
            ),
        ):
            cfg.get.return_value = {"path_template": template}
            ok_a, _, _ = start_worktree("wt-a", base="main")
            ok_b, _, _ = start_worktree("wt-b", base="main")

        assert ok_a and ok_b
        sha_a = _run_git(repo_path, "rev-parse", "wt-a")
        sha_b = _run_git(repo_path, "rev-parse", "wt-b")
        assert sha_a == main_sha
        assert sha_b == main_sha

    def test_spawn_ignores_caller_process_cwd(self, repo, tmp_path, monkeypatch):
        """With base='HEAD', the ref must resolve against the main worktree,
        not the caller's cwd. Uses a cwd-dependent ref so a regression (e.g.
        dropping the `cwd=` kwarg) would actually change the resolved sha."""
        repo_path, main_sha = repo
        # Create a sibling worktree at an older sha and chdir into it.
        other = tmp_path / "other"
        _run_git(repo_path, "worktree", "add", "-b", "other", str(other), "HEAD~1")
        monkeypatch.chdir(other)
        other_sha = _run_git(other, "rev-parse", "HEAD")
        assert other_sha != main_sha

        template = f"{tmp_path}/wts/${{repo_name}}/${{branch_name}}"
        with (
            patch("claudechic.features.worktree.git.CONFIG") as cfg,
            patch(
                "claudechic.features.worktree.git.get_main_worktree",
                return_value=(repo_path, "main"),
            ),
        ):
            cfg.get.return_value = {"path_template": template}
            ok, _, _ = start_worktree("wt-a", base="HEAD")

        assert ok
        # HEAD resolved from the main worktree → main_sha, not other_sha.
        assert _run_git(repo_path, "rev-parse", "wt-a") == main_sha
