from pathlib import Path


def test_readme_documents_llm_wiki_workflow():
    text = Path("README.md").read_text(encoding="utf-8")

    assert "NetSuite LLM Wiki MCP" in text
    assert "wiki_init" in text
    assert "wiki_ingest" in text
    assert "wiki_query" in text
    assert "wiki_lint" in text
    assert "raw/" in text
    assert "sources/" in text
    assert "wiki/projects/" in text
    assert "wiki/concepts/" in text
    assert "CodeGraph" in text
    assert "不引入 Chroma、sentence-transformers 或 embedding 模型" in text
    assert "不创建" in text
    assert "NETSUITE_LLM_WIKI_VAULT_ROOT" in text


def test_repository_mcp_config_has_no_absolute_paths():
    mcp_json = Path(".vscode/mcp.json")
    if not mcp_json.exists():
        return

    text = mcp_json.read_text(encoding="utf-8")
    assert "C:\\" not in text and "D:\\" not in text and "F:\\" not in text
    # vault root 与 server 安装目录都通过环境变量引用传入，不硬编码绝对路径
    assert "${env:NETSUITE_LLM_WIKI_VAULT_ROOT}" in text
    assert "${env:NETSUITE_LLM_WIKI_MCP_DIR}" in text


def test_repository_does_not_ship_vault_sources_yaml():
    assert not Path("rag/sources.yaml").exists()


def test_local_artifact_and_planning_paths_are_ignored():
    text = Path(".gitignore").read_text(encoding="utf-8")

    for pattern in [
        "rag/",
        "docs/plan/",
        "docs/superpowers/",
        ".vscode/",
        ".pytest_cache/",
        ".venv/",
        "*.egg-info/",
    ]:
        assert pattern in text


def test_readme_avoids_machine_specific_python_paths_and_history_notes():
    text = Path("README.md").read_text(encoding="utf-8")

    assert "C:\\Python" not in text
    assert "python.exe -m pip install" not in text
    assert "python -m pip install -e" not in text
    assert "字段名变更" not in text
    assert "related_script_ids" not in text
    assert "related_records" not in text


def test_installation_docs_prefer_uv_commands():
    readme = Path("README.md").read_text(encoding="utf-8")
    claude = Path("CLAUDE.md").read_text(encoding="utf-8")

    combined = readme + "\n" + claude

    assert "uv sync --extra dev" in readme
    assert "uv run pytest" in readme
    assert '"command": "uv"' in readme
    assert '"command": "netsuite-llm-wiki-mcp-server"' not in readme

    assert "uv sync --extra dev" in claude
    assert "命令为 `uv run pytest tests/test_<module>.py`" in claude
    assert "python -m pip install -e" not in combined
