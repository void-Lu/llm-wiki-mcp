"""Tests for git_utils: extracting commit, branch, and dirty status via subprocess."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from common.git_utils import (
    get_git_branch,
    get_git_commit,
    get_git_dirty,
    get_git_revision,
    is_git_dirty,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_DIR = Path("/fake/repo")


def _successful_run(stdout: str, returncode: int = 0) -> MagicMock:
    """Create a mock subprocess.CompletedProcess for a successful git command."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = stdout
    cp.stderr = ""
    return cp


def _failed_run(returncode: int = 128, stderr: str = "fatal: not a git repository") -> MagicMock:
    """Create a mock subprocess.CompletedProcess for a failed git command."""
    cp = MagicMock(spec=subprocess.CompletedProcess)
    cp.returncode = returncode
    cp.stdout = ""
    cp.stderr = stderr
    return cp


# ---------------------------------------------------------------------------
# get_git_commit
# ---------------------------------------------------------------------------


class TestGetGitCommit:
    def test_returns_short_sha_on_success(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run("abc1234\n")
            result = get_git_commit(SAMPLE_DIR)
            assert result == "abc1234"
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert "rev-parse" in args[0][0]
            assert "--short" in args[0][0]
            assert "HEAD" in args[0][0]

    def test_returns_empty_on_failure(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(128, "git")
            result = get_git_commit(SAMPLE_DIR)
            assert result == ""

    def test_returns_empty_when_git_not_found(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = get_git_commit(SAMPLE_DIR)
            assert result == ""

    def test_returns_empty_on_subprocess_error(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.SubprocessError("timeout")
            result = get_git_commit(SAMPLE_DIR)
            assert result == ""


# ---------------------------------------------------------------------------
# get_git_revision
# ---------------------------------------------------------------------------


class TestGetGitRevision:
    def test_returns_full_sha_on_success(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            revision = "a" * 40
            mock_run.return_value = _successful_run(f"{revision}\n")

            result = get_git_revision(SAMPLE_DIR)

            assert result == revision
            args = mock_run.call_args[0][0]
            assert args[-2:] == ["rev-parse", "HEAD"]
            assert "--short" not in args

    def test_returns_empty_on_failure(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _failed_run()

            assert get_git_revision(SAMPLE_DIR) == ""


# ---------------------------------------------------------------------------
# is_git_dirty
# ---------------------------------------------------------------------------


class TestIsGitDirty:
    def test_returns_true_when_changes_exist(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run(" M src/main.py\n?? new_file.py\n")
            result = is_git_dirty(SAMPLE_DIR)
            assert result is True

    def test_returns_false_when_clean(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run("")
            result = is_git_dirty(SAMPLE_DIR)
            assert result is False

    def test_returns_false_on_failure(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(128, "git")
            result = is_git_dirty(SAMPLE_DIR)
            assert result is False

    def test_returns_false_when_git_not_found(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = is_git_dirty(SAMPLE_DIR)
            assert result is False


class TestGetGitDirty:
    def test_distinguishes_clean_from_unavailable(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run("")
            assert get_git_dirty(SAMPLE_DIR) is False

            mock_run.side_effect = FileNotFoundError("git not found")
            assert get_git_dirty(SAMPLE_DIR) is None


# ---------------------------------------------------------------------------
# get_git_branch
# ---------------------------------------------------------------------------


class TestGetGitBranch:
    def test_returns_branch_name_on_success(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run("main\n")
            result = get_git_branch(SAMPLE_DIR)
            assert result == "main"

    def test_returns_empty_on_failure(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(128, "git")
            result = get_git_branch(SAMPLE_DIR)
            assert result == ""

    def test_returns_empty_when_git_not_found(self) -> None:
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = get_git_branch(SAMPLE_DIR)
            assert result == ""

    def test_returns_detached_head_hash(self) -> None:
        """In detached HEAD, rev-parse --abbrev-ref HEAD returns 'HEAD'."""
        with patch("common.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run("HEAD\n")
            result = get_git_branch(SAMPLE_DIR)
            assert result == "HEAD"

