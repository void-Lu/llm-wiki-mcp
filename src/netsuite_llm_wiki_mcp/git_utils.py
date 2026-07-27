"""Git utility functions for extracting commit, branch, and dirty status.

Uses subprocess git commands — no external Python dependencies required.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class GitInfo:
    """Git information for a file or directory."""

    commit: str = ""  # Short commit SHA (empty if not in git repo)
    branch: str = ""  # Current branch name (empty if not in git repo)
    dirty: bool = False  # True if working tree has uncommitted changes


def _resolve_git_dir(path: Path) -> Path:
    """Resolve a path to a directory suitable for git -C.

    If path is a file, return its parent directory.
    Otherwise return path itself.
    """
    return path.parent if path.is_file() else path


def get_git_commit(path: Path) -> str:
    """Get short commit SHA for the git repo containing path.

    Args:
        path: Path to a file or directory within a git repo.

    Returns:
        Short commit SHA (~7 chars), or empty string if not in a git repo
        or git is not available.
    """
    return _run_git(path, "rev-parse", "--short", "HEAD") or ""


def get_git_revision(path: Path) -> str:
    """Get the full commit revision for the git repo containing path."""
    return _run_git(path, "rev-parse", "HEAD") or ""


def get_git_dirty(path: Path) -> bool | None:
    """Return dirty state, or None when Git state cannot be determined.

    Args:
        path: Path to a file or directory within a git repo.

    Returns:
        True if there are uncommitted changes, False if clean, or None when
        the path is not in a Git repo or Git is unavailable.
    """
    output = _run_git(path, "status", "--porcelain")
    return None if output is None else bool(output)


def is_git_dirty(path: Path) -> bool:
    """Check if a Git worktree is dirty, preserving the legacy bool API."""
    return bool(get_git_dirty(path))


def get_git_branch(path: Path) -> str:
    """Get current branch name for the git repo containing path.

    Args:
        path: Path to a file or directory within a git repo.

    Returns:
        Branch name, or empty string if not in a git repo or git not available.
    """
    return _run_git(path, "rev-parse", "--abbrev-ref", "HEAD") or ""


def get_git_info(path: Path) -> GitInfo:
    """Get git info for a directory or file.

    Args:
        path: Path to a file or directory within a git repo.

    Returns:
        GitInfo with commit, branch, dirty status.
        Returns defaults (empty strings, False) if not in a git repo or git not available.
    """
    return GitInfo(
        commit=get_git_commit(path),
        branch=get_git_branch(path),
        dirty=is_git_dirty(path),
    )


def format_git_commit(commit: str, dirty: bool) -> str:
    """Format commit+dirty as 'abc1234' or 'abc1234+dirty'.

    Args:
        commit: Short commit SHA.
        dirty: Whether there are uncommitted changes.

    Returns:
        Formatted string like 'abc1234' or 'abc1234+dirty'.
    """
    if dirty:
        return f"{commit}+dirty"
    return commit


def _run_git(path: Path, *arguments: str) -> str | None:
    git_dir = _resolve_git_dir(path)
    try:
        result = subprocess.run(
            ["git", "-C", str(git_dir), *arguments],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        logger.warning("git command not found on this system")
        return None
    except subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()
