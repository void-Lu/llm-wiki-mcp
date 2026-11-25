from __future__ import annotations

import ast
import asyncio
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession, StdioServerParameters, stdio_client


ROOT = Path(__file__).resolve().parents[1]
BUILDER_PATH = ROOT / "scripts" / "build_wiki_plugins.py"
HOSTS = ("codex", "claude", "copilot", "antigravity", "dsh")
HOOK_HOSTS = ("codex", "claude", "copilot", "antigravity")
MCP_FILENAMES = {
    "codex": "mcp.json",
    "claude": ".mcp.json",
    "copilot": "mcp.json",
    "antigravity": "mcp_config.json",
    "dsh": "mcp.json",
}
MANIFEST_PATHS = {
    "codex": Path("plugin.json"),
    "claude": Path(".claude-plugin") / "plugin.json",
    "copilot": Path("plugin.json"),
    "antigravity": Path("plugin.json"),
    "dsh": Path("package.json"),
}
HOST_ROOT_VARS = {
    "codex": "${PLUGIN_ROOT}",
    "claude": "${CLAUDE_PLUGIN_ROOT}",
    "copilot": "${PLUGIN_ROOT}",
    "antigravity": "${PLUGIN_ROOT}",
    "dsh": "${PLUGIN_ROOT}",
}
HOOK_EVENTS = {
    "codex": "SessionStart",
    "claude": "SessionStart",
    "copilot": "sessionStart",
    "antigravity": "SessionStart",
}
SYNTHETIC_VERSION = "2.3.4"
SYNTHETIC_WHEEL = f"llm_wiki_mcp-{SYNTHETIC_VERSION}-py3-none-any.whl"
SYNTHETIC_PACKAGES = ("app", "archive", "common", "retrieval", "runtime", "wiki")


def _load_builder() -> Any:
    spec = importlib.util.spec_from_file_location("build_wiki_plugins", BUILDER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


BUILDER = _load_builder()


def _create_synthetic_wheel(
    dest_dir: Path,
    *,
    version: str = SYNTHETIC_VERSION,
    packages: tuple[str, ...] = SYNTHETIC_PACKAGES,
    extra_entries: dict[str, str] | None = None,
) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    wheel = dest_dir / f"llm_wiki_mcp-{version}-py3-none-any.whl"
    dist_info = f"llm_wiki_mcp-{version}.dist-info"
    with zipfile.ZipFile(wheel, "w") as archive:
        for pkg in packages:
            archive.writestr(f"{pkg}/__init__.py", "")
        archive.writestr("_build_info.py", 'SCHEMA_VERSION = 1\nREVISION = "a"*40\nDIRTY = False\n')
        archive.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: llm-wiki-mcp\nVersion: {version}\n",
        )
        archive.writestr(
            f"{dist_info}/entry_points.txt",
            "[console_scripts]\nllm-wiki-mcp = app.cli:main\nllm-wiki-mcp-server = app.server:main\n",
        )
        for name, content in (extra_entries or {}).items():
            archive.writestr(name, content)
    return wheel


@pytest.fixture
def fixture_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    for pkg in SYNTHETIC_PACKAGES:
        pkg_dir = repo / "src" / pkg
        pkg_dir.mkdir(parents=True)
        (pkg_dir / "__init__.py").write_text("", encoding="utf-8")

    skill = repo / "skills" / "shared-spec-vault"
    (skill / "references").mkdir(parents=True)
    (skill / "agents").mkdir(parents=True)
    (skill / "__pycache__").mkdir()
    (skill / "__pycache__" / "stale.pyc").write_bytes(b"cache")
    (skill / "SKILL.md").write_text("# Shared skill\n", encoding="utf-8")
    (skill / "references" / "guide.md").write_text("reference\n", encoding="utf-8")
    (skill / "agents" / "openai.yaml").write_text(
        "policy:\n  allow_implicit_invocation: false\n", encoding="utf-8"
    )

    plugins = repo / "plugins"
    plugins.mkdir()
    for name in ("session-guidance.md", "session_hook.py", "README.md"):
        (plugins / name).write_bytes((ROOT / "plugins" / name).read_bytes())

    for host in HOSTS:
        host_root = repo / "plugins" / "hosts" / host
        host_root.mkdir(parents=True)
        (host_root / "node_modules" / "local-package").mkdir(parents=True)
        (host_root / "node_modules" / "local-package" / "index.js").write_text(
            "local dependency cache", encoding="utf-8"
        )
        (host_root / "__pycache__").mkdir()
        (host_root / "__pycache__" / "stale.pyc").write_bytes(b"cache")
        (host_root / ".ruff_cache").mkdir()
        (host_root / ".ruff_cache" / "stale").write_text("cache", encoding="utf-8")
        (host_root / ".cache").mkdir()
        (host_root / ".cache" / "stale").write_text("cache", encoding="utf-8")
        (host_root / "tests").mkdir()
        (host_root / "tests" / "adapter.test.mjs").write_text("test only", encoding="utf-8")

        manifest_path = host_root / MANIFEST_PATHS[host]
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "name": "dsh-llm-wiki" if host == "dsh" else "llm-wiki",
                    "version": "0.0.1",
                    "command": "llm-wiki-mcp-server",
                }
            ),
            encoding="utf-8",
        )
        if host == "dsh":
            (host_root / "README.md").write_text(
                "# DSH v{{VERSION}}\nwheel: {{WHEEL_NAME}}\n", encoding="utf-8"
            )
        (host_root / "unrelated.json").write_text(
            '{"command":"llm-wiki-mcp-server","args":["leave-me"]}\n',
            encoding="utf-8",
        )
        stdio_server: dict[str, Any] = {"command": "llm-wiki-mcp-server", "args": []}
        if host in {"codex", "copilot"}:
            stdio_server["type"] = "stdio"
        (host_root / MCP_FILENAMES[host]).write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "llm-wiki": stdio_server,
                        "remote": {
                            "type": "http",
                            "command": "llm-wiki-mcp-server",
                            "url": "https://example.invalid/mcp",
                        },
                    }
                }
            ),
            encoding="utf-8",
        )

    def _fake_build_wheel(root: Path, work_dir: Path, *, uv_exe: str) -> Path:
        return _create_synthetic_wheel(work_dir)

    def _fake_export_constraints(root: Path, destination: Path, *, uv_exe: str) -> None:
        destination.write_text("mcp==2.0.0\npyyaml==6.0.3\n", encoding="utf-8")

    monkeypatch.setattr(BUILDER, "_build_server_wheel", _fake_build_wheel)
    monkeypatch.setattr(BUILDER, "_export_constraints", _fake_export_constraints)
    return repo


def _build(fixture_repo: Path, output: Path, **kwargs: Any) -> Path:
    return BUILDER.build_plugins(fixture_repo, output, **kwargs)


def test_assembles_five_self_contained_roots_with_bundled_wheel_and_matrix(
    fixture_repo: Path, tmp_path: Path
) -> None:
    output = _build(fixture_repo, tmp_path / "bundle")
    guidance = (ROOT / "plugins" / "session-guidance.md").read_text(encoding="utf-8")

    assert {child.name for child in output.iterdir() if child.is_dir()} == {
        *HOSTS,
        ".agents",
        ".claude-plugin",
    }

    for host in HOSTS:
        host_root = output / host
        assert (host_root / "skills" / "shared-spec-vault" / "SKILL.md").read_text(
            encoding="utf-8"
        ) == "# Shared skill\n"
        assert not (host_root / "skills" / "shared-spec-vault" / "__pycache__").exists()
        assert (
            host_root / "skills" / "shared-spec-vault" / "references" / "guide.md"
        ).read_text(encoding="utf-8") == "reference\n"

        openai_yaml = host_root / "skills" / "shared-spec-vault" / "agents" / "openai.yaml"
        if host == "codex":
            assert openai_yaml.is_file()
        else:
            assert not openai_yaml.exists()
            assert not (host_root / "skills" / "shared-spec-vault" / "agents").exists()

        assert not (host_root / "node_modules").exists()
        assert not (host_root / "__pycache__").exists()
        assert not (host_root / ".ruff_cache").exists()
        assert not (host_root / ".cache").exists()
        assert not (host_root / "tests").exists()
        assert not (host_root / "CHANGELOG.md").exists()

        if host == "dsh":
            assert (host_root / "session-guidance.md").read_text(encoding="utf-8") == guidance
            assert not (host_root / "session_hook.py").exists()
        else:
            assert not (host_root / "session-guidance.md").exists()
            packaged_hook = (host_root / "session_hook.py").read_text(encoding="utf-8")
            assert repr(guidance) in packaged_hook
            assert "SESSION_GUIDANCE: str | None = None" not in packaged_hook

        readme = (host_root / "README.md").read_text(encoding="utf-8")
        assert SYNTHETIC_VERSION in readme
        assert SYNTHETIC_WHEEL in readme
        assert "{{VERSION}}" not in readme
        assert "{{WHEEL_NAME}}" not in readme

        assert (host_root / "server" / SYNTHETIC_WHEEL).is_file()
        constraints_text = (host_root / "server" / "constraints.txt").read_text(encoding="utf-8")
        assert "mcp==2.0.0" in constraints_text

        mcp_raw = (host_root / MCP_FILENAMES[host]).read_text(encoding="utf-8")
        assert "C:\\" not in mcp_raw and "C:/" not in mcp_raw
        mcp_config = json.loads(mcp_raw)
        root_var = HOST_ROOT_VARS[host]
        expected_args = [
            "--python",
            ">=3.11",
            "--with-requirements",
            f"{root_var}/server/constraints.txt",
            "--from",
            f"{root_var}/server/{SYNTHETIC_WHEEL}",
            "llm-wiki-mcp-server",
        ]
        server = mcp_config["mcpServers"]["llm-wiki"]
        assert server["command"] == "uvx"
        assert server["args"] == expected_args
        assert "--no-python-downloads" not in server["args"]
        if host in {"codex", "copilot"}:
            assert server["type"] == "stdio"

        assert mcp_config["mcpServers"]["remote"]["command"] == "llm-wiki-mcp-server"
        assert (host_root / "unrelated.json").read_text(encoding="utf-8") == (
            '{"command":"llm-wiki-mcp-server","args":["leave-me"]}\n'
        )
        manifest = json.loads((host_root / MANIFEST_PATHS[host]).read_text(encoding="utf-8"))
        assert manifest["version"] == SYNTHETIC_VERSION
        assert manifest["command"] == "llm-wiki-mcp-server"

    codex_marketplace = json.loads(
        (output / ".agents" / "plugins" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert codex_marketplace == {
        "name": "llm-wiki-local",
        "plugins": [
            {
                "name": "llm-wiki",
                "version": SYNTHETIC_VERSION,
                "source": {"source": "local", "path": "./codex"},
            }
        ],
    }
    claude_marketplace = json.loads(
        (output / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8")
    )
    assert claude_marketplace == {
        "name": "llm-wiki-local",
        "owner": {"name": "LLM Wiki MCP"},
        "plugins": [
            {
                "name": "llm-wiki",
                "version": SYNTHETIC_VERSION,
                "source": "./claude",
            }
        ],
    }


def test_wheel_whitelist_rejects_unexpected_top_level_package(
    fixture_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _dirty_wheel(root: Path, work_dir: Path, *, uv_exe: str) -> Path:
        return _create_synthetic_wheel(
            work_dir,
            extra_entries={"codegraph/__init__.py": "# stale deleted package\n"},
        )

    monkeypatch.setattr(BUILDER, "_build_server_wheel", _dirty_wheel)
    with pytest.raises(BUILDER.BuildError, match="codegraph"):
        _build(fixture_repo, tmp_path / "bad-wheel")
    assert not (tmp_path / "bad-wheel").exists()


def test_builder_rejects_dirty_git_tree_by_default_and_allows_override(
    fixture_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("LLM_WIKI_BUILD_DIRTY", raising=False)
    subprocess.run(["git", "init", "-q"], cwd=fixture_repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=fixture_repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=fixture_repo, check=True)
    subprocess.run(["git", "add", "."], cwd=fixture_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=fixture_repo, check=True)

    # Clean tree succeeds by default
    clean_out = _build(fixture_repo, tmp_path / "clean")
    assert clean_out.is_dir()

    # Dirty tree is rejected by default
    (fixture_repo / "plugins" / "README.md").write_text("# modified {{VERSION}} {{WHEEL_NAME}}\n", encoding="utf-8")
    with pytest.raises(BUILDER.BuildError, match="uncommitted changes"):
        _build(fixture_repo, tmp_path / "dirty-blocked")
    assert not (tmp_path / "dirty-blocked").exists()

    # Explicit --allow-dirty succeeds
    dirty_out = _build(fixture_repo, tmp_path / "dirty-allowed", allow_dirty=True)
    assert dirty_out.is_dir()

    # Environment dirty flag is also honored
    monkeypatch.setenv("LLM_WIKI_BUILD_DIRTY", "true")
    with pytest.raises(BUILDER.BuildError, match="LLM_WIKI_BUILD_DIRTY"):
        _build(fixture_repo, tmp_path / "env-dirty-blocked")


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        (
            "codex",
            lambda text: {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": text,
                }
            },
        ),
        (
            "claude",
            lambda text: {
                "hookSpecificOutput": {
                    "hookEventName": "SessionStart",
                    "additionalContext": text,
                }
            },
        ),
        ("copilot", lambda text: {"additionalContext": text}),
        (
            "antigravity",
            lambda text: {"injectSteps": [{"systemMessage": {"systemMessage": text}}]},
        ),
    ],
)
def test_packaged_hooks_emit_host_session_start_json(
    fixture_repo: Path,
    tmp_path: Path,
    host: str,
    expected: Any,
) -> None:
    output = _build(fixture_repo, tmp_path / "bundle")
    hook = output / host / "session_hook.py"
    guidance = (ROOT / "plugins" / "session-guidance.md").read_text(encoding="utf-8")
    event = {"hook_event_name": HOOK_EVENTS[host], "cwd": "C:/丁/验收工作区"}
    result = subprocess.run(
        [sys.executable, str(hook), host],
        input=json.dumps(event, ensure_ascii=False).encode("utf-8"),
        env=os.environ | {"PYTHONIOENCODING": "cp1252"},
        capture_output=True,
        check=True,
        timeout=10,
    )
    assert result.stderr == b""
    assert json.loads(result.stdout) == expected(guidance)


def test_hook_ignores_non_start_and_malformed_events(
    fixture_repo: Path, tmp_path: Path
) -> None:
    output = _build(fixture_repo, tmp_path / "bundle")
    hook = output / "codex" / "session_hook.py"
    for payload in ('{"hook_event_name":"Stop"}', "not-json"):
        result = subprocess.run(
            [sys.executable, str(hook), "codex"],
            input=payload,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
        assert result.stdout == "{}\n"
        assert result.stderr == ""


def test_guidance_locks_tool_names_and_task_scoped_approval_boundaries() -> None:
    guidance = (ROOT / "plugins" / "session-guidance.md").read_text(encoding="utf-8")
    for required in (
        'wiki_list(store_scope="active"|"raw"|"archive")',
        "Only the current user's request in this host session grants permission",
        'wiki_update(page_path, incoming_body, action="preview")',
        "plan_id",
        "current_hash",
        "expected_hash",
        "complete, role-marked snapshot",
        "A selected excerpt is a selected source, not a complete chat",
        "For an authorized target",
        'wiki_archive(target, action="apply", plan_id=...)',
        'wiki_restore(archive_id, action="apply", plan_id=...)',
        "Choose an allowed reason (`superseded`, `deprecated`, `retention`, `migration`, or `manual`)",
        "wiki_write_note",
        "wiki_ingest",
        "wiki_archive",
        "wiki_restore",
    ):
        assert required in guidance
    assert 'wiki_list(scope=' not in guidance


def test_python_hook_has_no_direct_file_or_network_access() -> None:
    tree = ast.parse((ROOT / "plugins" / "session_hook.py").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".", maxsplit=1)[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert imported <= {"__future__", "json", "sys", "typing"}
    assert not any(
        isinstance(node, ast.Name) and node.id in {"open", "urlopen"}
        for node in ast.walk(tree)
    )


@pytest.mark.parametrize("output_exists", [False, True])
def test_builder_preserves_existing_output_directory(
    fixture_repo: Path, tmp_path: Path, output_exists: bool
) -> None:
    output = tmp_path / "existing"
    if output_exists:
        output.mkdir()
        sentinel = output / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
    else:
        output.mkdir()

    with pytest.raises(BUILDER.BuildError, match="output directory already exists"):
        _build(fixture_repo, output)
    if output_exists:
        assert (output / "keep.txt").read_text(encoding="utf-8") == "keep"
        assert sorted(path.name for path in output.iterdir()) == ["keep.txt"]
    else:
        assert list(output.iterdir()) == []


def test_builder_removes_only_its_staging_tree_after_failure(
    fixture_repo: Path, tmp_path: Path
) -> None:
    bad_config = fixture_repo / "plugins" / "hosts" / "dsh" / MCP_FILENAMES["dsh"]
    bad_config.write_text("not json", encoding="utf-8")
    output = tmp_path / "failed-bundle"

    with pytest.raises(BUILDER.BuildError, match="invalid MCP config"):
        _build(fixture_repo, output)

    assert not output.exists()
    assert list(tmp_path.glob(".failed-bundle.stage-*")) == []


def test_builder_force_replaces_existing_directory_atomically_and_preserves_on_failure(
    fixture_repo: Path, tmp_path: Path
) -> None:
    output = tmp_path / "fixed-bundle"
    output.mkdir()
    (output / "stale.txt").write_text("old", encoding="utf-8")

    # --force replaces existing directory cleanly
    rebuilt = _build(fixture_repo, output, force=True)
    assert rebuilt == output
    assert not (output / "stale.txt").exists()
    assert (output / "codex" / "plugin.json").is_file()
    assert list(tmp_path.glob(".fixed-bundle.old-*")) == []

    # Failed build with --force leaves previous output intact
    (fixture_repo / "plugins" / "hosts" / "dsh" / MCP_FILENAMES["dsh"]).write_text(
        "not json", encoding="utf-8"
    )
    with pytest.raises(BUILDER.BuildError, match="invalid MCP config"):
        _build(fixture_repo, output, force=True)
    assert (output / "codex" / "plugin.json").is_file()
    assert list(tmp_path.glob(".fixed-bundle.stage-*")) == []


def test_cli_accepts_output_dir_allow_dirty_and_force_only() -> None:
    default = BUILDER.parse_args(["--output-dir", "out"])
    assert default.output_dir == Path("out")
    assert default.allow_dirty is False
    assert default.force is False

    allowed = BUILDER.parse_args(["--output-dir", "out", "--allow-dirty", "--force"])
    assert allowed.allow_dirty is True
    assert allowed.force is True

    with pytest.raises(SystemExit):
        BUILDER.parse_args(["--output-dir", "out", "--server-command", "custom-server"])
    with pytest.raises(SystemExit):
        BUILDER.parse_args(["--output-dir", "out", "--server-arg", "--flag"])


@pytest.fixture(scope="module")
def real_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    base = tmp_path_factory.mktemp("real plugin bundle with spaces")
    return BUILDER.build_plugins(ROOT, base / "wiki plugins", allow_dirty=True)


def test_real_repo_bundle_wheel_and_constraints_are_clean(real_bundle: Path) -> None:
    project_version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["version"]
    expected_wheel = f"llm_wiki_mcp-{project_version}-py3-none-any.whl"
    expected_packages = {
        child.name
        for child in (ROOT / "src").iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    }

    for host in HOSTS:
        host_root = real_bundle / host
        wheel = host_root / "server" / expected_wheel
        assert wheel.is_file()
        constraints = (host_root / "server" / "constraints.txt").read_text(encoding="utf-8")
        assert "mcp==" in constraints
        assert "pyyaml==" in constraints
        assert "sentence-transformers" not in constraints
        assert "pytest==" not in constraints
        assert "ruff==" not in constraints

        manifest = json.loads((host_root / MANIFEST_PATHS[host]).read_text(encoding="utf-8"))
        assert manifest["version"] == project_version

    wheel_path = real_bundle / "codex" / "server" / expected_wheel
    with zipfile.ZipFile(wheel_path) as archive:
        top_level = {name.split("/", 1)[0] for name in archive.namelist() if name}
    assert top_level == expected_packages | {
        "_build_info.py",
        f"llm_wiki_mcp-{project_version}.dist-info",
    }
    assert "codegraph" not in top_level
    assert "tests" not in top_level


def _expand_host_mcp(host_root: Path, host: str) -> tuple[str, list[str]]:
    mcp_config = json.loads((host_root / MCP_FILENAMES[host]).read_text(encoding="utf-8"))
    server = mcp_config["mcpServers"]["llm-wiki"]
    root_var = HOST_ROOT_VARS[host]
    root_str = str(host_root)
    command = server["command"].replace(root_var, root_str)
    args = [arg.replace(root_var, root_str) for arg in server["args"]]
    return command, args


def test_bundled_stdio_server_handshake_and_concurrent_writes(
    real_bundle: Path, tmp_path: Path
) -> None:
    vault = tmp_path / "vault with spaces"
    vault.mkdir()
    isolated_env = os.environ | {
        "LLM_WIKI_CONFIG_DIR": str(tmp_path / "config dir"),
        "LLM_WIKI_USER_DATA_DIR": str(tmp_path / "data dir"),
    }

    # Initialize vault via bundled wheel CLI (also warms uvx cache)
    codex_root = real_bundle / "codex"
    wheel = next((codex_root / "server").glob("*.whl"))
    constraints = codex_root / "server" / "constraints.txt"
    uvx = shutil.which("uvx") or "uvx"
    init_res = subprocess.run(
        [
            uvx,
            "--python",
            ">=3.11",
            "--with-requirements",
            str(constraints),
            "--from",
            str(wheel),
            "llm-wiki-mcp",
            "init",
            "--vault",
            "bundled-smoke",
            "--root",
            str(vault),
            "--default",
        ],
        cwd=tmp_path,
        env=isolated_env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert init_res.returncode == 0, init_res.stderr or init_res.stdout

    async def _exercise() -> None:
        # 1. Verify every host's expanded MCP command completes stdio handshake & wiki_status
        for host in HOSTS:
            command, args = _expand_host_mcp(real_bundle / host, host)
            params = StdioServerParameters(
                command=command,
                args=args,
                env=isolated_env,
                cwd=tmp_path,
            )
            async with stdio_client(params) as (read, write), ClientSession(
                read, write
            ) as session:
                init = await session.initialize()
                assert init.server_info.name
                res = await session.call_tool("wiki_status", {"vault": "bundled-smoke"})
                assert not res.is_error, res.content
                data = res.structured_content or json.loads(res.content[0].text)
                assert data["ok"] is True
                assert data["vault"] == "bundled-smoke"

        # 2. Verify two concurrent bundled server processes can write to the same vault
        cmd_a, args_a = _expand_host_mcp(real_bundle / "claude", "claude")
        cmd_b, args_b = _expand_host_mcp(real_bundle / "copilot", "copilot")
        params_a = StdioServerParameters(command=cmd_a, args=args_a, env=isolated_env, cwd=tmp_path)
        params_b = StdioServerParameters(command=cmd_b, args=args_b, env=isolated_env, cwd=tmp_path)

        async with (
            stdio_client(params_a) as (ra, wa),
            ClientSession(ra, wa) as session_a,
            stdio_client(params_b) as (rb, wb),
            ClientSession(rb, wb) as session_b,
        ):
            await asyncio.gather(session_a.initialize(), session_b.initialize())
            res_a, res_b = await asyncio.gather(
                session_a.call_tool(
                    "wiki_write_note",
                    {
                        "vault": "bundled-smoke",
                        "note_type": "knowledge",
                        "domain": "common-errors",
                        "filename": "proc-a-note",
                        "title": "Concurrent Write A",
                        "content": "Written by host process A.",
                    },
                ),
                session_b.call_tool(
                    "wiki_write_note",
                    {
                        "vault": "bundled-smoke",
                        "note_type": "knowledge",
                        "domain": "common-errors",
                        "filename": "proc-b-note",
                        "title": "Concurrent Write B",
                        "content": "Written by host process B.",
                    },
                ),
            )
            assert not res_a.is_error, res_a.content
            assert not res_b.is_error, res_b.content
            data_a = res_a.structured_content or json.loads(res_a.content[0].text)
            data_b = res_b.structured_content or json.loads(res_b.content[0].text)
            assert data_a["ok"] is True, data_a
            assert data_b["ok"] is True, data_b
            assert (vault / data_a["path"]).is_file()
            assert (vault / data_b["path"]).is_file()

    asyncio.run(asyncio.wait_for(_exercise(), timeout=180))


def test_bundled_server_zero_config_auto_creates_default_vault(
    real_bundle: Path, tmp_path: Path
) -> None:
    """Fresh environment with no init and no LLM_WIKI_VAULT_ROOT auto-bootstraps default vault."""

    default_vault = tmp_path / "Documents" / "llm-wiki-vault"
    config_dir = tmp_path / "fresh config"
    isolated_env = {
        k: v for k, v in os.environ.items() if k != "LLM_WIKI_VAULT_ROOT"
    } | {
        "LLM_WIKI_CONFIG_DIR": str(config_dir),
        "LLM_WIKI_DEFAULT_VAULT_ROOT": str(default_vault),
    }
    command, args = _expand_host_mcp(real_bundle / "codex", "codex")
    params = StdioServerParameters(command=command, args=args, env=isolated_env, cwd=tmp_path)

    async def _exercise() -> None:
        async with stdio_client(params) as (read, write), ClientSession(
            read, write
        ) as session:
            await session.initialize()
            res = await session.call_tool("wiki_status", {})
            assert not res.is_error, res.content
            data = res.structured_content or json.loads(res.content[0].text)
            assert data["ok"] is True
            assert data["vault"] == "llm-wiki-vault"

            write_res = await session.call_tool(
                "wiki_write_note",
                {
                    "note_type": "knowledge",
                    "domain": "common-errors",
                    "filename": "zero-config-check",
                    "title": "Zero Config Check",
                    "content": "Verifies zero-config vault stores runtime state inside vault.",
                },
            )
            assert not write_res.is_error, write_res.content
            write_data = write_res.structured_content or json.loads(write_res.content[0].text)
            assert write_data["ok"] is True, write_data

    asyncio.run(asyncio.wait_for(_exercise(), timeout=120))
    assert default_vault.is_dir()
    assert (default_vault / ".llm-wiki").is_dir()
    assert (config_dir / "config.yaml").is_file()
    assert not (real_bundle / "codex" / ".llm-wiki").exists()
