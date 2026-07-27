from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from netsuite_llm_wiki_mcp.cli import main


def _make_vault(path: Path) -> Path:
    (path / "rag").mkdir(parents=True)
    (path / "rag" / "sources.yaml").write_text(
        "schema_version: 2\nworkspace_root: .\nsources: []\n",
        encoding="utf-8",
    )
    return path


def test_init_writes_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    vault = _make_vault(tmp_path / "Homework Vault")
    config_dir = tmp_path / "config"
    monkeypatch.setenv("NETSUITE_LLM_WIKI_CONFIG_DIR", str(config_dir))

    exit_code = main(["init", "--vault", "homework", "--root", str(vault), "--default"])

    assert exit_code == 0
    raw = yaml.safe_load((config_dir / "config.yaml").read_text(encoding="utf-8"))
    assert raw["default_vault"] == "homework"
    assert raw["vaults"]["homework"]["root"] == str(vault.resolve())
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["vault_root"] == str(vault.resolve())
    assert output["resolution_source"] == "argument"
    assert output["config_path"] == str((config_dir / "config.yaml").resolve())
    # vault-local: data lives inside vault
    assert output["vault_data_root"] == str((vault / ".rag-index").resolve())
    assert output["chroma_path"].endswith("chroma")
    assert output["manifest_path"].endswith("index-manifest.json")
    assert output["embedding_cache_path"] == str((vault / ".models").resolve())
    assert output["sources_config_exists"] is True


def test_status_reads_same_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    vault = _make_vault(tmp_path / "Homework Vault")
    config_dir = tmp_path / "config"
    monkeypatch.delenv("NETSUITE_LLM_WIKI_VAULT_ROOT", raising=False)
    monkeypatch.setenv("NETSUITE_LLM_WIKI_CONFIG_DIR", str(config_dir))

    assert main(["init", "--vault", "homework", "--root", str(vault), "--default"]) == 0
    capsys.readouterr()
    assert main(["status"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["vault_root"] == str(vault.resolve())
    assert output["resolution_source"] == "global_config"
    assert output["config_path"] == str((config_dir / "config.yaml").resolve())
    # vault-local: data lives inside vault
    assert output["vault_data_root"] == str((vault / ".rag-index").resolve())
    assert output["chroma_path"].endswith("chroma")
    assert output["manifest_path"].endswith("index-manifest.json")
    assert output["embedding_cache_path"] == str((vault / ".models").resolve())
    assert output["sources_config_exists"] is True


def test_status_reports_full_diagnostics_when_sources_config_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    vault = tmp_path / "Homework Vault"
    vault.mkdir()
    config_dir = tmp_path / "config"
    monkeypatch.delenv("NETSUITE_LLM_WIKI_VAULT_ROOT", raising=False)
    monkeypatch.setenv("NETSUITE_LLM_WIKI_CONFIG_DIR", str(config_dir))

    assert main(["init", "--vault", "homework", "--root", str(vault), "--default"]) == 0
    init_output = json.loads(capsys.readouterr().out)
    assert init_output["sources_config_exists"] is False
    assert not (vault / "rag" / "sources.yaml").exists()

    exit_code = main(["status"])

    assert exit_code != 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["code"] == "missing_sources_config"
    assert "rag/sources.yaml" in output["error"]
    assert output["vault_root"] == str(vault.resolve())
    assert output["resolution_source"] == "global_config"
    assert output["config_path"] == str((config_dir / "config.yaml").resolve())
    assert output["global_config_path"] == str((config_dir / "config.yaml").resolve())
    # vault-local: data lives inside vault
    assert output["vault_data_root"] == str((vault / ".rag-index").resolve())
    assert output["vault_storage_dir"] == output["vault_data_root"]
    assert output["chroma_path"].endswith("chroma")
    assert output["manifest_path"].endswith("index-manifest.json")
    assert output["embedding_cache_path"] == str((vault / ".models").resolve())
    assert output["model_cache_path"] == str((vault / ".models").resolve())
    assert output["sources_config_path"] == str((vault / "rag" / "sources.yaml").resolve())
    assert output["sources_config_exists"] is False


def test_status_returns_nonzero_with_actionable_message_when_config_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    monkeypatch.delenv("NETSUITE_LLM_WIKI_VAULT_ROOT", raising=False)
    monkeypatch.setenv("NETSUITE_LLM_WIKI_CONFIG_DIR", str(tmp_path / "missing-config"))
    monkeypatch.setenv("NETSUITE_LLM_WIKI_USER_DATA_DIR", str(tmp_path / "user-data"))

    exit_code = main(["status"])

    assert exit_code != 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["code"] == "missing_vault_root"
    assert "netsuite-llm-wiki-mcp init --vault" in output["error"]
    assert "NETSUITE_LLM_WIKI_VAULT_ROOT" in output["error"]


def test_init_returns_nonzero_when_vault_root_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    config_dir = tmp_path / "config"
    missing_root = tmp_path / "missing-vault"
    monkeypatch.setenv("NETSUITE_LLM_WIKI_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("NETSUITE_LLM_WIKI_USER_DATA_DIR", str(tmp_path / "user-data"))

    exit_code = main(["init", "--vault", "homework", "--root", str(missing_root), "--default"])

    assert exit_code != 0
    assert not (config_dir / "config.yaml").exists()
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["code"] == "invalid_vault_root"
    assert str(missing_root) in output["error"]


def test_init_reports_missing_sources_without_creating_starter_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    vault = tmp_path / "Homework Vault"
    vault.mkdir()
    monkeypatch.setenv("NETSUITE_LLM_WIKI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("NETSUITE_LLM_WIKI_USER_DATA_DIR", str(tmp_path / "user-data"))

    exit_code = main(["init", "--vault", "homework", "--root", str(vault), "--default"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["sources_config_exists"] is False
    assert not (vault / "rag" / "sources.yaml").exists()


def test_server_subcommand_delegates_to_server_main(monkeypatch: pytest.MonkeyPatch):
    calls: list[str] = []

    def fake_server_main() -> None:
        calls.append("server")

    monkeypatch.setattr("netsuite_llm_wiki_mcp.server.main", fake_server_main)

    assert main(["server"]) == 0
    assert calls == ["server"]


def test_retrieval_eval_writes_json_and_markdown_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    fixture_root = Path(__file__).parent / "fixtures" / "retrieval"
    vault = tmp_path / "vault"
    shutil.copytree(fixture_root / "vault", vault)
    dataset = tmp_path / "fixture.jsonl"
    shutil.copy2(fixture_root / "fixture.jsonl", dataset)
    shutil.copy2(fixture_root / "fixture.manifest.json", tmp_path / "fixture.manifest.json")
    output_dir = tmp_path / "reports"

    exit_code = main(
        [
            "retrieval-eval",
            "--vault",
            str(vault),
            "--dataset",
            str(dataset),
            "--output-dir",
            str(output_dir),
            "--repeats",
            "2",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["metrics"]["recall_at_k_macro"] == 1.0
    assert Path(output["reports"]["json"]).is_file()
    assert Path(output["reports"]["markdown"]).is_file()


def test_pyproject_exposes_cli_without_preload_script():
    text = Path("pyproject.toml").read_text(encoding="utf-8")

    assert 'netsuite-llm-wiki-mcp = "netsuite_llm_wiki_mcp.cli:main"' in text
    assert 'netsuite-llm-wiki-mcp-server = "netsuite_llm_wiki_mcp.server:main"' in text
    assert "netsuite-llm-wiki-mcp-preload-model" not in text
