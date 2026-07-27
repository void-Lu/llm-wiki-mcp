from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

FULL_REVISION = "b" * 40
BUILD_REVISION_ENV = "NETSUITE_LLM_WIKI_BUILD_REVISION"
BUILD_DIRTY_ENV = "NETSUITE_LLM_WIKI_BUILD_DIRTY"


def _copy_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    for name in ("pyproject.toml", "README.md", "build_backend.py", "MANIFEST.in"):
        shutil.copy2(name, project / name)
    shutil.copytree("src", project / "src")
    assert not (project / ".git").exists()
    return project


def _build_wheel(
    project: Path,
    output_dir: Path,
    *,
    revision: str | None,
    dirty: str | None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.pop(BUILD_REVISION_ENV, None)
    env.pop(BUILD_DIRTY_ENV, None)
    if revision is not None:
        env[BUILD_REVISION_ENV] = revision
    if dirty is not None:
        env[BUILD_DIRTY_ENV] = dirty
    return subprocess.run(
        [
            shutil.which("uv") or "uv",
            "build",
            "--wheel",
            "--out-dir",
            str(output_dir),
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_build_requires_an_injected_or_git_revision(tmp_path: Path):
    project = _copy_project(tmp_path)
    completed = _build_wheel(
        project,
        tmp_path / "missing-dist",
        revision=None,
        dirty=None,
    )

    assert completed.returncode != 0
    assert "build revision" in (completed.stderr + completed.stdout)


def test_build_validates_explicit_revision(tmp_path: Path):
    project = _copy_project(tmp_path)
    completed = _build_wheel(
        project,
        tmp_path / "invalid-dist",
        revision="not-a-revision",
        dirty="false",
    )

    assert completed.returncode != 0
    assert "invalid build revision" in (completed.stderr + completed.stdout)


def test_wheel_embeds_revision_and_imports_without_git_metadata(
    tmp_path: Path,
):
    project = _copy_project(tmp_path)
    output_dir = tmp_path / "dist"
    completed = _build_wheel(
        project,
        output_dir,
        revision=FULL_REVISION,
        dirty="false",
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert not (
        project / "src" / "netsuite_llm_wiki_mcp" / "_build_info.py"
    ).exists()

    wheel = next(output_dir.glob("*.whl"))
    with zipfile.ZipFile(wheel) as archive:
        build_info = archive.read(
            "netsuite_llm_wiki_mcp/_build_info.py"
        ).decode("utf-8")
    assert f'REVISION = "{FULL_REVISION}"' in build_info
    assert "DIRTY = False" in build_info

    code = (
        "import json,sys;"
        f"sys.path.insert(0,{str(wheel)!r});"
        "from netsuite_llm_wiki_mcp.runtime_provenance import "
        "RUNTIME_PROVENANCE;"
        "print(json.dumps(RUNTIME_PROVENANCE.to_public_dict()))"
    )
    isolated = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert isolated.returncode == 0, isolated.stderr
    runtime = json.loads(isolated.stdout)
    assert runtime["package_version"] == "0.9.0"
    assert runtime["revision"] == FULL_REVISION
    assert runtime["dirty"] is False
    assert runtime["revision_source"] == "build"
    assert runtime["provenance_incomplete"] is False

    restarted = subprocess.run(
        [sys.executable, "-I", "-c", code],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert restarted.returncode == 0, restarted.stderr
    restarted_runtime = json.loads(restarted.stdout)
    assert restarted_runtime["started_at"] != runtime["started_at"]
