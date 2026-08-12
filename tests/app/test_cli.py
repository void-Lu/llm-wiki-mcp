from __future__ import annotations

import json
import sqlite3
import shutil
from pathlib import Path

import pytest
import yaml

from app.cli import main
from retrieval.retrieval_index import RetrievalIndexStore


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
    monkeypatch.setenv("LLM_WIKI_CONFIG_DIR", str(config_dir))

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
    monkeypatch.delenv("LLM_WIKI_VAULT_ROOT", raising=False)
    monkeypatch.setenv("LLM_WIKI_CONFIG_DIR", str(config_dir))

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
    monkeypatch.delenv("LLM_WIKI_VAULT_ROOT", raising=False)
    monkeypatch.setenv("LLM_WIKI_CONFIG_DIR", str(config_dir))

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
    monkeypatch.delenv("LLM_WIKI_VAULT_ROOT", raising=False)
    monkeypatch.setenv("LLM_WIKI_CONFIG_DIR", str(tmp_path / "missing-config"))
    monkeypatch.setenv("LLM_WIKI_USER_DATA_DIR", str(tmp_path / "user-data"))

    exit_code = main(["status"])

    assert exit_code != 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is False
    assert output["code"] == "missing_vault_root"
    assert "llm-wiki-mcp init --vault" in output["error"]
    assert "LLM_WIKI_VAULT_ROOT" in output["error"]


def test_init_returns_nonzero_when_vault_root_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
):
    config_dir = tmp_path / "config"
    missing_root = tmp_path / "missing-vault"
    monkeypatch.setenv("LLM_WIKI_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("LLM_WIKI_USER_DATA_DIR", str(tmp_path / "user-data"))

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
    monkeypatch.setenv("LLM_WIKI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("LLM_WIKI_USER_DATA_DIR", str(tmp_path / "user-data"))

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

    monkeypatch.setattr("app.server.main", fake_server_main)

    assert main(["server"]) == 0
    assert calls == ["server"]


def test_retrieval_eval_writes_json_and_markdown_reports(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    fixture_root = Path(__file__).parents[1] / "fixtures" / "retrieval"
    vault = tmp_path / "vault"
    shutil.copytree(fixture_root / "vault", vault)
    dataset = tmp_path / "fixture.jsonl"
    shutil.copy2(fixture_root / "fixture.jsonl", dataset)
    shutil.copy2(fixture_root / "fixture.manifest.json", tmp_path / "fixture.manifest.json")
    output_dir = tmp_path / "reports"
    store = RetrievalIndexStore(vault)
    store.build(store.iter_vault_pages())

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
            "--query-version",
            "v2",
        ]
    )

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["metrics"]["recall_at_k_macro"] == 0.8
    assert output["metrics"]["precision_at_k_macro"] == pytest.approx(0.1)
    assert output["gate"] is None
    assert Path(output["reports"]["json"]).is_file()
    assert Path(output["reports"]["markdown"]).is_file()


def test_retrieval_gold_sample_cli_writes_redacted_template(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    vault = _make_vault(tmp_path / "vault")
    database = vault / ".llm-wiki" / "state.sqlite3"
    database.parent.mkdir()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE query_telemetry("
            "query_hash TEXT NOT NULL, normalized_query_redacted TEXT NOT NULL, at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, scope TEXT NOT NULL, project TEXT NOT NULL, passage_ids TEXT NOT NULL, "
            "fallback_level TEXT NOT NULL, token_count INTEGER NOT NULL, latency_ms REAL NOT NULL, "
            "outcome TEXT NOT NULL DEFAULT 'completed')"
        )
        for index in range(4):
            connection.execute(
                "INSERT INTO query_telemetry VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (f"hash-{index}", f"query {index}", "2026-08-11", "2026-12-31", "knowledge", "", "", "none", 0, 1.0, "completed"),
            )
        connection.commit()
    output_dir = tmp_path / "gold"

    exit_code = main(["retrieval-gold-sample", "--vault", str(vault), "--output-dir", str(output_dir), "--count", "4"])

    assert exit_code == 0
    output = json.loads(capsys.readouterr().out)
    assert output["ok"] is True
    assert output["count"] == 4
    assert "template" not in output
    assert "manifest" not in output
    assert output["artifacts"] == {
        "template": "gold-template.jsonl",
        "manifest": "gold-template.manifest.json",
    }
    assert (output_dir / "gold-template.jsonl").is_file()
    assert (output_dir / "gold-template.manifest.json").is_file()


def test_vector_status_is_read_only_and_reports_missing_index(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    vault = _make_vault(tmp_path / "vault")

    exit_code = main(["vector", "status", "--vault", str(vault)])

    assert exit_code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["code"] == "index_missing"
    assert output["local_provider"] == {"available": False, "code": "model_missing"}
    assert not (vault / ".llm-wiki" / "vector-index").exists()


def test_pyproject_exposes_cli_without_preload_script():
    text = Path("pyproject.toml").read_text(encoding="utf-8")

    assert 'llm-wiki-mcp = "app.cli:main"' in text
    assert 'llm-wiki-mcp-server = "app.server:main"' in text
    assert "llm-wiki-mcp-preload-model" not in text
