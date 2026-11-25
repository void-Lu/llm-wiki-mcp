#!/usr/bin/env python3
"""Assemble self-contained local Wiki plugin bundles with a bundled MCP server wheel."""

from __future__ import annotations

import argparse
from email.parser import Parser
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any, Sequence

HOSTS = ("codex", "claude", "copilot", "antigravity", "dsh")
HOOK_HOSTS = frozenset({"codex", "claude", "copilot", "antigravity"})
GUIDANCE_FILE_HOSTS = frozenset({"dsh"})
OPENAI_AGENT_META_HOSTS = frozenset({"codex"})
SHARED_README_HOSTS = frozenset({"codex", "claude", "copilot", "antigravity"})
HOST_PLUGIN_ROOT_VARS = {
    "codex": "${PLUGIN_ROOT}",
    "claude": "${CLAUDE_PLUGIN_ROOT}",
    "copilot": "${PLUGIN_ROOT}",
    "antigravity": "${PLUGIN_ROOT}",
    "dsh": "${PLUGIN_ROOT}",
}
HOST_MANIFEST_PATHS = {
    "codex": Path("plugin.json"),
    "claude": Path(".claude-plugin") / "plugin.json",
    "copilot": Path("plugin.json"),
    "antigravity": Path("plugin.json"),
    "dsh": Path("package.json"),
}
DEFAULT_SERVER_COMMAND = "llm-wiki-mcp-server"
DIST_NAME = "llm_wiki_mcp"
MCP_CONFIG_NAMES = {".mcp.json", "mcp.json", "mcp_config.json"}
HOOK_GUIDANCE_MARKER = "SESSION_GUIDANCE: str | None = None  # build-time embed"
SERVER_DIR_NAME = "server"
CONSTRAINTS_FILENAME = "constraints.txt"
PYTHON_SPEC = ">=3.11"
UVX_COMMAND = "uvx"
BUILD_DIRTY_ENV = "LLM_WIKI_BUILD_DIRTY"
CACHE_IGNORE_PATTERNS = (
    "__pycache__",
    "*.pyc",
    "*.pyo",
    "node_modules",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cache",
    ".parcel-cache",
    ".vite",
    ".turbo",
    ".venv",
    "venv",
    "coverage",
    ".coverage",
    ".npm",
    ".pnpm-store",
)
PACKAGE_IGNORE_PATTERNS = (
    *CACHE_IGNORE_PATTERNS,
    ".git",
    ".hg",
    ".svn",
    "test",
    "tests",
    "__tests__",
    "test_*.py",
    "*.test.*",
    "*.spec.*",
)


class BuildError(ValueError):
    """A bundle cannot be built cleanly."""


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _require_uv() -> str:
    uv_exe = shutil.which("uv")
    if not uv_exe:
        raise BuildError("uv executable not found on PATH")
    return uv_exe


def _check_working_tree(root: Path, *, allow_dirty: bool) -> None:
    if allow_dirty:
        return

    env_dirty = os.environ.get(BUILD_DIRTY_ENV)
    if env_dirty is not None:
        normalized = env_dirty.strip().lower()
        if normalized in {"1", "true"}:
            raise BuildError(
                f"{BUILD_DIRTY_ENV} is set to dirty; build from a clean commit or pass --allow-dirty"
            )
        if normalized in {"0", "false"}:
            return
        raise BuildError(f"invalid {BUILD_DIRTY_ENV} value: {env_dirty!r}")

    if not (root / ".git").exists():
        return

    try:
        result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BuildError(f"failed to check git working tree status: {exc}") from exc

    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BuildError(f"failed to check git working tree status: {detail}")

    if result.stdout.strip():
        raise BuildError(
            "working tree has uncommitted changes; commit or stash them, or pass --allow-dirty"
        )


def _expected_src_packages(root: Path) -> set[str]:
    src_dir = root / "src"
    if not src_dir.is_dir():
        raise BuildError(f"missing src/ directory: {src_dir}")
    packages = {
        child.name
        for child in src_dir.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    }
    if not packages:
        raise BuildError(f"no Python packages found under {src_dir}")
    return packages


def _build_server_wheel(root: Path, work_dir: Path, *, uv_exe: str) -> Path:
    """Build sdist then wheel from sdist so stale build/lib artifacts never leak in."""

    result = subprocess.run(
        [uv_exe, "build", "--out-dir", str(work_dir)],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BuildError(f"uv build failed: {detail}")

    wheels = sorted(work_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise BuildError(
            f"expected exactly 1 built wheel in {work_dir}, found {len(wheels)}"
        )
    return wheels[0]


def _validate_and_inspect_wheel(root: Path, wheel_path: Path) -> str:
    """Verify wheel top-level entries against src/ whitelist and return package version."""

    expected_packages = _expected_src_packages(root)
    try:
        with zipfile.ZipFile(wheel_path) as archive:
            names = [name for name in archive.namelist() if name]
            metadata_candidates = [
                name for name in names if name.endswith(".dist-info/METADATA")
            ]
            if len(metadata_candidates) != 1:
                raise BuildError(
                    f"wheel {wheel_path.name} must contain exactly one .dist-info/METADATA"
                )
            metadata_text = archive.read(metadata_candidates[0]).decode("utf-8")
            headers = Parser().parsestr(metadata_text)
            version = (headers.get("Version") or "").strip()
            if not version:
                raise BuildError(f"wheel {wheel_path.name} metadata missing Version")

            expected_prefix = f"{DIST_NAME}-{version}-"
            if not wheel_path.name.startswith(expected_prefix):
                raise BuildError(
                    f"wheel filename {wheel_path.name!r} does not match {expected_prefix!r}"
                )

            dist_info_dir = f"{DIST_NAME}-{version}.dist-info"
            expected_top_level = expected_packages | {"_build_info.py", dist_info_dir}
            actual_top_level = {name.split("/", 1)[0] for name in names}
            if actual_top_level != expected_top_level:
                unexpected = sorted(actual_top_level - expected_top_level)
                missing = sorted(expected_top_level - actual_top_level)
                raise BuildError(
                    f"wheel {wheel_path.name} top-level mismatch "
                    f"(unexpected={unexpected}, missing={missing})"
                )

            entry_points_path = f"{dist_info_dir}/entry_points.txt"
            if entry_points_path not in names:
                raise BuildError(f"wheel {wheel_path.name} missing {entry_points_path}")
            entry_points = archive.read(entry_points_path).decode("utf-8")
            if f"{DEFAULT_SERVER_COMMAND} = app.server:main" not in entry_points:
                raise BuildError(
                    f"wheel {wheel_path.name} missing {DEFAULT_SERVER_COMMAND} entry point"
                )
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise BuildError(f"invalid wheel {wheel_path.name}: {exc}") from exc

    return version


def _export_constraints(root: Path, destination: Path, *, uv_exe: str) -> None:
    result = subprocess.run(
        [
            uv_exe,
            "export",
            "--frozen",
            "--no-dev",
            "--no-emit-project",
            "--no-hashes",
            "--format",
            "requirements-txt",
            "-o",
            str(destination),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise BuildError(f"uv export failed: {detail}")
    if not destination.is_file() or not destination.read_text(encoding="utf-8").strip():
        raise BuildError("uv export produced an empty constraints.txt")


def _bundled_server_invocation(host: str, wheel_name: str) -> tuple[str, list[str]]:
    root_var = HOST_PLUGIN_ROOT_VARS[host]
    args = [
        "--python",
        PYTHON_SPEC,
        "--with-requirements",
        f"{root_var}/{SERVER_DIR_NAME}/{CONSTRAINTS_FILENAME}",
        "--from",
        f"{root_var}/{SERVER_DIR_NAME}/{wheel_name}",
        DEFAULT_SERVER_COMMAND,
    ]
    return UVX_COMMAND, args


def _replace_declared_server(
    value: Any, *, command: str, args: Sequence[str]
) -> int:
    """Replace matching stdio commands in an MCP configuration JSON tree."""

    if isinstance(value, dict):
        count = 0
        declared_command = value.get("command")
        transport = value.get("type")
        if declared_command == DEFAULT_SERVER_COMMAND and transport in (None, "stdio"):
            value["command"] = command
            value["args"] = list(args)
            count += 1
        for key, child in value.items():
            if key not in {"command", "args"}:
                count += _replace_declared_server(child, command=command, args=args)
        return count
    if isinstance(value, list):
        return sum(
            _replace_declared_server(item, command=command, args=args) for item in value
        )
    return 0


def _rewrite_mcp_configs(host_root: Path, *, command: str, args: Sequence[str]) -> int:
    rewritten = 0
    for path in host_root.rglob("*"):
        if not path.is_file() or path.name.lower() not in MCP_CONFIG_NAMES:
            continue
        try:
            raw = path.read_text(encoding="utf-8")
            config = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BuildError(
                f"invalid MCP config {path.relative_to(host_root)}: {exc}"
            ) from exc
        replacements = _replace_declared_server(config, command=command, args=args)
        if replacements:
            path.write_text(
                json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            rewritten += replacements
    return rewritten


def _stamp_manifest_version(host_root: Path, host: str, version: str) -> None:
    manifest_rel = HOST_MANIFEST_PATHS[host]
    manifest_path = host_root / manifest_rel
    if not manifest_path.is_file():
        raise BuildError(f"{host} template missing manifest {manifest_rel}")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise BuildError(f"invalid {host} manifest {manifest_rel}: {exc}") from exc
    if not isinstance(manifest, dict):
        raise BuildError(f"{host} manifest {manifest_rel} must be a JSON object")
    manifest["version"] = version
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _render_readme(template: str, *, version: str, wheel_name: str) -> str:
    return (
        template.replace("{{VERSION}}", version)
        .replace("{{WHEEL_NAME}}", wheel_name)
    )


def _embed_guidance(hook_source: str, guidance: str) -> str:
    if hook_source.count(HOOK_GUIDANCE_MARKER) != 1:
        raise BuildError("session_hook.py must contain exactly one guidance embed marker")
    return hook_source.replace(
        HOOK_GUIDANCE_MARKER,
        f"SESSION_GUIDANCE: str | None = {guidance!r}",
        1,
    )


def _prune_non_codex_skill_metadata(skill_dest: Path) -> None:
    openai_meta = skill_dest / "agents" / "openai.yaml"
    if openai_meta.exists():
        openai_meta.unlink()
    agents_dir = skill_dest / "agents"
    if agents_dir.is_dir() and not any(agents_dir.iterdir()):
        agents_dir.rmdir()


def _write_marketplaces(output_root: Path, *, version: str) -> None:
    codex_path = output_root / ".agents" / "plugins" / "marketplace.json"
    codex_path.parent.mkdir(parents=True)
    codex_path.write_text(
        json.dumps(
            {
                "name": "llm-wiki-local",
                "plugins": [
                    {
                        "name": "llm-wiki",
                        "version": version,
                        "source": {"source": "local", "path": "./codex"},
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    claude_path = output_root / ".claude-plugin" / "marketplace.json"
    claude_path.parent.mkdir(parents=True)
    claude_path.write_text(
        json.dumps(
            {
                "name": "llm-wiki-local",
                "owner": {"name": "LLM Wiki MCP"},
                "plugins": [
                    {
                        "name": "llm-wiki",
                        "version": version,
                        "source": "./claude",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def build_plugins(
    repo_root: str | Path,
    output_dir: str | Path,
    *,
    allow_dirty: bool = False,
) -> Path:
    """Build self-contained host plugin bundles into a new output directory atomically."""

    root = Path(repo_root).expanduser().resolve()
    output = Path(output_dir).expanduser().resolve()
    if output.exists():
        raise BuildError(f"output directory already exists; choose a new path: {output}")

    templates_root = (root / "plugins" / "hosts").resolve()
    reserved_roots = ((root / "plugins").resolve(), (root / "skills").resolve())
    if any(_is_within(output, reserved) for reserved in reserved_roots):
        raise BuildError("output directory must be outside plugins/ and skills/")

    templates = {host: templates_root / host for host in HOSTS}
    missing = [host for host, path in templates.items() if not path.is_dir()]
    if missing:
        raise BuildError(f"missing host templates: {', '.join(missing)}")

    skill_source = root / "skills" / "shared-spec-vault"
    guidance_source = root / "plugins" / "session-guidance.md"
    hook_source_path = root / "plugins" / "session_hook.py"
    shared_readme_source = root / "plugins" / "README.md"
    for source in (skill_source, guidance_source, hook_source_path, shared_readme_source):
        if not source.exists():
            raise BuildError(f"missing shared package source: {source.relative_to(root)}")

    for host, template in templates.items():
        reserved_entries = [
            template / "session-guidance.md",
            template / "session_hook.py",
            template / "skills" / "shared-spec-vault",
            template / SERVER_DIR_NAME,
        ]
        if host in SHARED_README_HOSTS:
            reserved_entries.append(template / "README.md")
        for reserved in reserved_entries:
            if reserved.exists():
                raise BuildError(
                    f"host template {host} contains reserved shared output: "
                    f"{reserved.relative_to(template)}"
                )

    uv_exe = _require_uv()
    _check_working_tree(root, allow_dirty=allow_dirty)

    guidance = guidance_source.read_text(encoding="utf-8")
    hook_source = hook_source_path.read_text(encoding="utf-8")
    packaged_hook = _embed_guidance(hook_source, guidance)
    shared_readme_template = shared_readme_source.read_text(encoding="utf-8")

    with tempfile.TemporaryDirectory(prefix="llm-wiki-plugin-build-") as build_tmp:
        tmp_path = Path(build_tmp)
        wheel_path = _build_server_wheel(root, tmp_path / "dist", uv_exe=uv_exe)
        version = _validate_and_inspect_wheel(root, wheel_path)
        constraints_path = tmp_path / CONSTRAINTS_FILENAME
        _export_constraints(root, constraints_path, uv_exe=uv_exe)
        wheel_name = wheel_path.name
        wheel_bytes = wheel_path.read_bytes()
        constraints_bytes = constraints_path.read_bytes()

    output.parent.mkdir(parents=True, exist_ok=True)
    staged_root = output.parent / f".{output.name}.stage-{uuid.uuid4().hex}"
    staged_root.mkdir()
    try:
        for host in HOSTS:
            host_output = staged_root / host
            shutil.copytree(
                templates[host],
                host_output,
                ignore=shutil.ignore_patterns(*PACKAGE_IGNORE_PATTERNS),
            )

            skill_dest = host_output / "skills" / "shared-spec-vault"
            shutil.copytree(
                skill_source,
                skill_dest,
                ignore=shutil.ignore_patterns(*CACHE_IGNORE_PATTERNS),
            )
            if host not in OPENAI_AGENT_META_HOSTS:
                _prune_non_codex_skill_metadata(skill_dest)

            if host in GUIDANCE_FILE_HOSTS:
                (host_output / "session-guidance.md").write_text(
                    guidance, encoding="utf-8"
                )
            if host in HOOK_HOSTS:
                (host_output / "session_hook.py").write_text(
                    packaged_hook, encoding="utf-8"
                )

            readme_dest = host_output / "README.md"
            if host in SHARED_README_HOSTS:
                readme_dest.write_text(
                    _render_readme(
                        shared_readme_template,
                        version=version,
                        wheel_name=wheel_name,
                    ),
                    encoding="utf-8",
                )
            elif readme_dest.is_file():
                readme_dest.write_text(
                    _render_readme(
                        readme_dest.read_text(encoding="utf-8"),
                        version=version,
                        wheel_name=wheel_name,
                    ),
                    encoding="utf-8",
                )

            server_dir = host_output / SERVER_DIR_NAME
            server_dir.mkdir()
            (server_dir / wheel_name).write_bytes(wheel_bytes)
            (server_dir / CONSTRAINTS_FILENAME).write_bytes(constraints_bytes)

            command, args = _bundled_server_invocation(host, wheel_name)
            if not _rewrite_mcp_configs(host_output, command=command, args=args):
                raise BuildError(
                    f"{host} template has no MCP stdio command "
                    f"{DEFAULT_SERVER_COMMAND!r} to package"
                )
            _stamp_manifest_version(host_output, host, version)

        _write_marketplaces(staged_root, version=version)
        staged_root.rename(output)
    except BaseException:
        if staged_root.exists():
            shutil.rmtree(staged_root, ignore_errors=True)
        raise
    return output


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="new output root; must not already exist",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        default=False,
        help="allow building when the Git working tree has uncommitted changes",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    options = parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]
    try:
        output = build_plugins(
            repo_root,
            options.output_dir,
            allow_dirty=options.allow_dirty,
        )
    except (BuildError, OSError) as exc:
        print(f"build failed: {exc}", file=sys.stderr)
        return 2
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
