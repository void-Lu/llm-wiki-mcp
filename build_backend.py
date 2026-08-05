from __future__ import annotations

import os
import re
import runpy
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from setuptools import build_meta as _setuptools_build_meta
from setuptools.command.build_py import build_py as _build_py
from setuptools.command.sdist import sdist as _sdist

BUILD_REVISION_ENV = "LLM_WIKI_BUILD_REVISION"
BUILD_DIRTY_ENV = "LLM_WIKI_BUILD_DIRTY"
_EDITABLE_BUILD_ENV = "_LLM_WIKI_EDITABLE_BUILD"
_BUILD_INFO_SCHEMA_VERSION = 1
_FULL_REVISION_PATTERN = re.compile(r"^[0-9a-f]{40,64}$", re.IGNORECASE)
_PROJECT_ROOT = Path(__file__).resolve().parent
_BUILD_INFO_RELATIVE_PATH = Path("src") / "_build_info.py"


@dataclass(frozen=True)
class BuildIdentity:
    revision: str
    dirty: bool


def resolve_build_identity(project_root: Path = _PROJECT_ROOT) -> BuildIdentity:
    revision = os.environ.get(BUILD_REVISION_ENV)
    dirty_text = os.environ.get(BUILD_DIRTY_ENV)
    if revision is not None or dirty_text is not None:
        if not revision:
            raise RuntimeError("build revision is required")
        return _validated_identity(revision, dirty_text)

    generated = _read_generated_identity(project_root)
    if generated is not None:
        return generated

    git_identity = _git_identity(project_root)
    if git_identity is not None:
        return _validated_identity(
            git_identity.revision,
            "true" if git_identity.dirty else "false",
        )

    raise RuntimeError(
        f"build revision is required via {BUILD_REVISION_ENV} or Git metadata"
    )


def _validated_identity(revision: str, dirty_text: str | None) -> BuildIdentity:
    if not _FULL_REVISION_PATTERN.fullmatch(revision):
        raise RuntimeError("invalid build revision")
    if dirty_text is None:
        raise RuntimeError("build dirty flag is required")
    normalized = dirty_text.strip().lower()
    if normalized in {"1", "true"}:
        dirty = True
    elif normalized in {"0", "false"}:
        dirty = False
    else:
        raise RuntimeError("invalid build dirty flag")
    return BuildIdentity(revision=revision.lower(), dirty=dirty)


def _git_identity(project_root: Path) -> BuildIdentity | None:
    # ``git -C <path>`` walks parent directories.  A copied release tree can
    # therefore accidentally inherit the surrounding checkout's revision;
    # only metadata rooted in the project being built is authoritative.
    if not (project_root / ".git").exists():
        return None
    source_path = str(project_root / "src")
    sys.path.insert(0, source_path)
    try:
        from common.git_utils import (
            get_git_dirty,
            get_git_revision,
        )

        revision = get_git_revision(project_root)
        dirty = get_git_dirty(project_root)
    finally:
        sys.path.remove(source_path)
    if not revision or dirty is None:
        return None
    return BuildIdentity(revision=revision, dirty=dirty)


def _read_generated_identity(project_root: Path) -> BuildIdentity | None:
    path = project_root / _BUILD_INFO_RELATIVE_PATH
    if not path.is_file():
        return None
    try:
        values = runpy.run_path(str(path))
        if values["SCHEMA_VERSION"] != _BUILD_INFO_SCHEMA_VERSION:
            raise RuntimeError("unsupported build metadata schema")
        return _validated_identity(
            str(values["REVISION"]),
            "true" if values["DIRTY"] is True else "false"
            if values["DIRTY"] is False
            else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("invalid generated build metadata") from exc


def _render_build_info(identity: BuildIdentity) -> str:
    return (
        '"""Generated at build time; do not edit."""\n\n'
        f"SCHEMA_VERSION = {_BUILD_INFO_SCHEMA_VERSION}\n"
        f'REVISION = "{identity.revision}"\n'
        f"DIRTY = {identity.dirty!r}\n"
    )


def _write_build_info(root: Path, identity: BuildIdentity) -> None:
    target = root / _BUILD_INFO_RELATIVE_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_render_build_info(identity), encoding="utf-8")


class BuildPy(_build_py):
    def run(self) -> None:
        super().run()
        if os.environ.get(_EDITABLE_BUILD_ENV) == "1":
            return
        identity = resolve_build_identity()
        target = Path(self.build_lib) / "_build_info.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_render_build_info(identity), encoding="utf-8")


class Sdist(_sdist):
    def make_release_tree(self, base_dir: str, files: list[str]) -> None:
        super().make_release_tree(base_dir, files)
        _write_build_info(Path(base_dir), resolve_build_identity())


@contextmanager
def _identity_environment(identity: BuildIdentity) -> Iterator[None]:
    previous_revision = os.environ.get(BUILD_REVISION_ENV)
    previous_dirty = os.environ.get(BUILD_DIRTY_ENV)
    os.environ[BUILD_REVISION_ENV] = identity.revision
    os.environ[BUILD_DIRTY_ENV] = "true" if identity.dirty else "false"
    try:
        yield
    finally:
        _restore_environment(BUILD_REVISION_ENV, previous_revision)
        _restore_environment(BUILD_DIRTY_ENV, previous_dirty)


def _restore_environment(name: str, value: str | None) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, str | list[str] | None] | None = None,
    metadata_directory: str | None = None,
) -> str:
    identity = resolve_build_identity()
    with _identity_environment(identity):
        return _setuptools_build_meta.build_wheel(
            wheel_directory,
            config_settings,
            metadata_directory,
        )


def build_sdist(
    sdist_directory: str,
    config_settings: dict[str, str | list[str] | None] | None = None,
) -> str:
    identity = resolve_build_identity()
    with _identity_environment(identity):
        return _setuptools_build_meta.build_sdist(
            sdist_directory,
            config_settings,
        )


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, str | list[str] | None] | None = None,
    metadata_directory: str | None = None,
) -> str:
    previous = os.environ.get(_EDITABLE_BUILD_ENV)
    os.environ[_EDITABLE_BUILD_ENV] = "1"
    try:
        return _setuptools_build_meta.build_editable(
            wheel_directory,
            config_settings,
            metadata_directory,
        )
    finally:
        _restore_environment(_EDITABLE_BUILD_ENV, previous)


get_requires_for_build_wheel = (
    _setuptools_build_meta.get_requires_for_build_wheel
)
get_requires_for_build_sdist = (
    _setuptools_build_meta.get_requires_for_build_sdist
)
prepare_metadata_for_build_wheel = (
    _setuptools_build_meta.prepare_metadata_for_build_wheel
)
get_requires_for_build_editable = (
    _setuptools_build_meta.get_requires_for_build_editable
)
prepare_metadata_for_build_editable = (
    _setuptools_build_meta.prepare_metadata_for_build_editable
)
