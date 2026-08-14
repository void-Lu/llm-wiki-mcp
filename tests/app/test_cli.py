from __future__ import annotations

import json
import sqlite3
import shutil
from pathlib import Path

import pytest
import yaml

from app.cli import main
from archive.archive_service import ArchiveService
from retrieval.query_telemetry import QueryTelemetry
from retrieval.retrieval_index import RetrievalIndexStore


def _make_vault(path: Path) -> Path:
    (path / "rag").mkdir(parents=True)
    (path / "rag" / "sources.yaml").write_text(
        "schema_version: 2\nworkspace_root: .\nsources: []\n",
        encoding="utf-8",
    )
    return path


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        path.relative_to(root).as_posix(): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


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
    assert output["logical_vault"] == "homework"
    assert output["vault"] == "homework"
    assert output["resolution_source"] == "config"
    assert output["config_path"] == str((config_dir / "config.yaml").resolve())
    assert output["storage_paths"] == {
        "state": ".llm-wiki/state.sqlite3",
        "page_state": ".llm-wiki/page-state.sqlite3",
        "retrieval_index": ".llm-wiki/retrieval.sqlite3",
        "vector_index": ".llm-wiki/vector-index",
        "archives": "archives/",
    }
    assert output["storage_id"]
    assert not {"data_root", "user_data_root", "vault_data_root", "vault_storage_dir", "chroma_path", "manifest_path", "embedding_cache_path", "model_cache_path", "vault_storage_id"} & output.keys()
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
    assert output["logical_vault"] == "homework"
    assert output["vault"] == "homework"
    assert output["resolution_source"] == "config"
    assert output["config_path"] == str((config_dir / "config.yaml").resolve())
    assert output["storage_paths"]["retrieval_index"] == ".llm-wiki/retrieval.sqlite3"
    assert output["storage_paths"]["vector_index"] == ".llm-wiki/vector-index"
    assert output["storage_paths"]["archives"] == "archives/"
    assert not {"data_root", "user_data_root", "vault_data_root", "vault_storage_dir", "chroma_path", "manifest_path", "embedding_cache_path", "model_cache_path", "vault_storage_id"} & output.keys()
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
    assert output["logical_vault"] == "homework"
    assert output["vault"] == "homework"
    assert output["resolution_source"] == "config"
    assert output["config_path"] == str((config_dir / "config.yaml").resolve())
    assert output["global_config_path"] == str((config_dir / "config.yaml").resolve())
    assert output["storage_paths"]["state"] == ".llm-wiki/state.sqlite3"
    assert output["storage_paths"]["page_state"] == ".llm-wiki/page-state.sqlite3"
    assert not {"data_root", "user_data_root", "vault_data_root", "vault_storage_dir", "chroma_path", "manifest_path", "embedding_cache_path", "model_cache_path", "vault_storage_id"} & output.keys()
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


def test_archive_status_missing_vault_is_read_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    before = _tree_snapshot(vault)

    exit_code = main(["archive", "status", "--vault", str(vault)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["ok"] is True
    assert output["state"] == "missing"
    assert output["code"] == "archive_state_missing"
    assert _tree_snapshot(vault) == before
    assert not (vault / ".llm-wiki").exists()


def test_archive_status_existing_vault_reports_ready_operations(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    vault = tmp_path / "vault"
    ArchiveService(vault)
    state_path = vault / ".llm-wiki" / "state.sqlite3"
    with sqlite3.connect(state_path) as connection:
        connection.execute(
            "INSERT INTO archive_operations "
            "(operation_id, archive_id, operation_type, state, plan_hash, actor, created_at, updated_at, error_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "operation-1",
                "archive-1",
                "archive",
                "committed",
                "plan-hash",
                "test",
                "2026-08-14T00:00:00+00:00",
                "2026-08-14T00:00:01+00:00",
                None,
            ),
        )

    exit_code = main(["archive", "status", "--vault", str(vault)])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["ok"] is True
    assert output["state"] == "ready"
    assert output["operations"] == [
        {
            "operation_id": "operation-1",
            "archive_id": "archive-1",
            "operation_type": "archive",
            "state": "committed",
            "updated_at": "2026-08-14T00:00:01+00:00",
            "error_code": None,
        }
    ]


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
    telemetry = QueryTelemetry(vault)
    for index in range(4):
        telemetry.record(
            question=f"query {index}",
            scope="knowledge",
            project=None,
            passage_ids=[],
            fallback_level="none",
            token_count=0,
            latency_ms=1.0,
        )
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
