"""Tests for git_utils: extracting commit, branch, and dirty status via subprocess."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from netsuite_llm_wiki_mcp.git_utils import (
    GitInfo,
    format_git_commit,
    get_git_branch,
    get_git_commit,
    get_git_info,
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
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run("abc1234\n")
            result = get_git_commit(SAMPLE_DIR)
            assert result == "abc1234"
            mock_run.assert_called_once()
            args = mock_run.call_args
            assert "rev-parse" in args[0][0]
            assert "--short" in args[0][0]
            assert "HEAD" in args[0][0]

    def test_returns_empty_on_failure(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(128, "git")
            result = get_git_commit(SAMPLE_DIR)
            assert result == ""

    def test_returns_empty_when_git_not_found(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = get_git_commit(SAMPLE_DIR)
            assert result == ""

    def test_returns_empty_on_subprocess_error(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.SubprocessError("timeout")
            result = get_git_commit(SAMPLE_DIR)
            assert result == ""


# ---------------------------------------------------------------------------
# is_git_dirty
# ---------------------------------------------------------------------------


class TestIsGitDirty:
    def test_returns_true_when_changes_exist(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run(" M src/main.py\n?? new_file.py\n")
            result = is_git_dirty(SAMPLE_DIR)
            assert result is True

    def test_returns_false_when_clean(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run("")
            result = is_git_dirty(SAMPLE_DIR)
            assert result is False

    def test_returns_false_on_failure(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(128, "git")
            result = is_git_dirty(SAMPLE_DIR)
            assert result is False

    def test_returns_false_when_git_not_found(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = is_git_dirty(SAMPLE_DIR)
            assert result is False


# ---------------------------------------------------------------------------
# get_git_branch
# ---------------------------------------------------------------------------


class TestGetGitBranch:
    def test_returns_branch_name_on_success(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run("main\n")
            result = get_git_branch(SAMPLE_DIR)
            assert result == "main"

    def test_returns_empty_on_failure(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(128, "git")
            result = get_git_branch(SAMPLE_DIR)
            assert result == ""

    def test_returns_empty_when_git_not_found(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            result = get_git_branch(SAMPLE_DIR)
            assert result == ""

    def test_returns_detached_head_hash(self) -> None:
        """In detached HEAD, rev-parse --abbrev-ref HEAD returns 'HEAD'."""
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.return_value = _successful_run("HEAD\n")
            result = get_git_branch(SAMPLE_DIR)
            assert result == "HEAD"


# ---------------------------------------------------------------------------
# get_git_info
# ---------------------------------------------------------------------------


class TestGetGitInfo:
    def test_returns_full_info_for_git_repo(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _successful_run("abc1234\n"),      # get_git_commit
                _successful_run("feature/x\n"),     # get_git_branch
                _successful_run(" M file.py\n"),    # is_git_dirty
            ]
            info = get_git_info(SAMPLE_DIR)
            assert info == GitInfo(commit="abc1234", branch="feature/x", dirty=True)

    def test_returns_defaults_for_non_git_dir(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(128, "git")
            info = get_git_info(SAMPLE_DIR)
            assert info == GitInfo(commit="", branch="", dirty=False)

    def test_works_with_file_path(self) -> None:
        """get_git_info should work when given a file path (uses parent dir)."""
        with patch("netsuite_llm_wiki_mcp.git_utils._resolve_git_dir", return_value=SAMPLE_DIR), \
             patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = [
                _successful_run("deadbeef\n"),   # get_git_commit
                _successful_run("main\n"),        # get_git_branch
                _successful_run(""),              # is_git_dirty
            ]
            info = get_git_info(Path("/fake/repo/src/main.py"))
            assert info.commit == "deadbeef"
            assert info.branch == "main"
            assert info.dirty is False

    def test_returns_defaults_when_git_not_available(self) -> None:
        with patch("netsuite_llm_wiki_mcp.git_utils.subprocess.run") as mock_run:
            mock_run.side_effect = FileNotFoundError("git not found")
            info = get_git_info(SAMPLE_DIR)
            assert info == GitInfo(commit="", branch="", dirty=False)


# ---------------------------------------------------------------------------
# format_git_commit
# ---------------------------------------------------------------------------


class TestFormatGitCommit:
    def test_plain_commit(self) -> None:
        assert format_git_commit("abc1234", dirty=False) == "abc1234"

    def test_dirty_commit(self) -> None:
        assert format_git_commit("abc1234", dirty=True) == "abc1234+dirty"

    def test_empty_commit_not_dirty(self) -> None:
        assert format_git_commit("", dirty=False) == ""

    def test_empty_commit_dirty(self) -> None:
        """Even with empty commit, dirty flag is appended (unusual but defined)."""
        assert format_git_commit("", dirty=True) == "+dirty"