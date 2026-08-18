from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from runtime.runtime_config import (
    ConfigRegistry,
    QualityGateSettings,
    RuntimeConfigError,
    _normalize_storage_hash_path,
    resolve_runtime_config,
    vault_storage_id,
    write_global_config,
)


def _make_vault(path: Path) -> Path:
    (path / "rag").mkdir(parents=True)
    (path / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "schema_version: 2",
                "workspace_root: .",
                "sources:",
                "  - source_name: obsidian",
                "    source_kind: note",
                "    root: .",
                "    include: [knowledge]",
                "    exclude: [.git, .obsidian]",
                "    file_types: [md]",
                "    parser: markdown_frontmatter_h2",
                "    collection: netsuite_knowledge",
                "    authority: curated_note_source",
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_explicit_argument_wins_over_env_and_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    arg_vault = _make_vault(tmp_path / "Arg Vault")
    env_vault = _make_vault(tmp_path / "Env Vault")
    config_vault = _make_vault(tmp_path / "Config Vault")
    config_path = tmp_path / "config" / "config.yaml"
    data_root = tmp_path / "data"

    write_global_config(config_path, vault_name="saved", vault_root=config_vault, make_default=True)
    monkeypatch.setenv("LLM_WIKI_VAULT_ROOT", str(env_vault))

    runtime = resolve_runtime_config(
        vault_root_arg=arg_vault,
        config_path=config_path,
        data_root=data_root,
    )

    assert runtime.vault_root == arg_vault.resolve()
    assert runtime.vault_name == "Arg Vault"
    assert runtime.resolution_source == "argument"
    assert runtime.global_config_path == config_path


def test_env_wins_over_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    env_vault = _make_vault(tmp_path / "Env Vault")
    config_vault = _make_vault(tmp_path / "Config Vault")
    config_path = tmp_path / "config" / "config.yaml"

    write_global_config(config_path, vault_name="saved", vault_root=config_vault, make_default=True)
    monkeypatch.setenv("LLM_WIKI_VAULT_ROOT", str(env_vault))

    runtime = resolve_runtime_config(config_path=config_path, data_root=tmp_path / "data")

    assert runtime.vault_root == env_vault.resolve()
    assert runtime.resolution_source == "env"


def test_relative_env_vault_root_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config_vault = _make_vault(tmp_path / "Config Vault")
    config_path = tmp_path / "config" / "config.yaml"

    write_global_config(config_path, vault_name="saved", vault_root=config_vault, make_default=True)
    monkeypatch.setenv("LLM_WIKI_VAULT_ROOT", "relative-vault")

    with pytest.raises(RuntimeConfigError) as exc_info:
        resolve_runtime_config(config_path=config_path, data_root=tmp_path / "data")

    message = str(exc_info.value)
    assert "LLM_WIKI_VAULT_ROOT" in message
    assert "absolute path" in message


def test_global_config_resolves_default_vault(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    vault = _make_vault(tmp_path / "Homework Vault")
    config_path = tmp_path / "config" / "config.yaml"

    write_global_config(config_path, vault_name="homework", vault_root=vault, make_default=True)
    monkeypatch.delenv("LLM_WIKI_VAULT_ROOT", raising=False)

    runtime = resolve_runtime_config(config_path=config_path, data_root=tmp_path / "data")

    assert runtime.vault_root == vault.resolve()
    assert runtime.vault_name == "homework"
    assert runtime.resolution_source == "global_config"
    assert runtime.sources_config_path == vault / "rag" / "sources.yaml"


def test_relative_global_config_vault_root_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    config_path = tmp_path / "config" / "config.yaml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        yaml.safe_dump(
            {
                "default_vault": "saved",
                "vaults": {
                    "saved": {
                        "root": "relative-vault",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.delenv("LLM_WIKI_VAULT_ROOT", raising=False)

    with pytest.raises(RuntimeConfigError) as exc_info:
        resolve_runtime_config(config_path=config_path, data_root=tmp_path / "data")

    message = str(exc_info.value)
    assert "global config" in message
    assert "vaults.saved.root" in message
    assert "absolute path" in message


def test_relative_config_dir_env_override_is_rejected(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setenv("LLM_WIKI_CONFIG_DIR", "relative-config")
    monkeypatch.delenv("LLM_WIKI_VAULT_ROOT", raising=False)

    with pytest.raises(ValueError) as exc_info:
        resolve_runtime_config(data_root=tmp_path / "data")

    message = str(exc_info.value)
    assert "LLM_WIKI_CONFIG_DIR" in message
    assert "absolute path" in message


def test_missing_config_does_not_use_current_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    cwd_vault = _make_vault(tmp_path / "cwd-vault")
    monkeypatch.chdir(cwd_vault)
    monkeypatch.delenv("LLM_WIKI_VAULT_ROOT", raising=False)

    with pytest.raises(RuntimeConfigError) as exc_info:
        resolve_runtime_config(config_path=tmp_path / "missing" / "config.yaml", data_root=tmp_path / "data")

    message = str(exc_info.value)
    assert "llm-wiki-mcp init --vault" in message
    assert "LLM_WIKI_VAULT_ROOT" in message
    assert str(cwd_vault) not in message


def test_runtime_config_excludes_retired_local_storage_layout(tmp_path: Path):
    vault = _make_vault(tmp_path / "Vault With Spaces")

    runtime = resolve_runtime_config(vault_root_arg=vault)

    retired_fields = {
        "data_root",
        "vault_data_root",
        "chroma_path",
        "manifest_path",
        "embedding_cache_path",
        "user_data_root",
        "vault_storage_dir",
    }
    assert all(not hasattr(runtime, field) for field in retired_fields)
    assert runtime.vault_storage_id == vault_storage_id(vault)


def test_two_vaults_with_same_folder_name_get_different_storage_ids(tmp_path: Path):
    first = _make_vault(tmp_path / "client-a" / "homework")
    second = _make_vault(tmp_path / "client-b" / "homework")

    first_id = vault_storage_id(first)
    second_id = vault_storage_id(second)

    assert first_id != second_id
    assert first_id.startswith("homework-")
    assert second_id.startswith("homework-")


def test_storage_hash_normalization_preserves_posix_case_differences():
    first = _normalize_storage_hash_path("/vaults/client/homework", case_sensitive=True)
    second = _normalize_storage_hash_path("/vaults/client/Homework", case_sensitive=True)

    assert first != second


def test_storage_hash_normalization_folds_windows_case_differences():
    first = _normalize_storage_hash_path(r"C:\Vaults\client\homework", case_sensitive=False)
    second = _normalize_storage_hash_path(r"c:\vaults\client\Homework", case_sensitive=False)

    assert first == second


def test_write_global_config_creates_expected_schema(tmp_path: Path):
    vault = _make_vault(tmp_path / "Homework Vault")
    config_path = tmp_path / "config" / "config.yaml"

    write_global_config(config_path, vault_name="homework", vault_root=vault, make_default=True)

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert raw == {
        "schema_version": 1,
        "default_vault": "homework",
        "vaults": {
            "homework": {
                "root": str(vault.resolve()),
            }
        },
    }


def test_registry_decodes_profiles_and_redacts_public_status(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "vault")
    model = tmp_path / "model"
    model.mkdir()
    config_path = tmp_path / "config" / "config.yaml"
    write_global_config(
        config_path,
        vault_name="primary",
        vault_root=vault,
        retrieval={"embedding": {"enabled": True, "model_path": str(model), "candidate_limit": 80}},
        telemetry={"retention_days": 30},
        archive={"index_snapshot_ttl_days": 14},
    )
    registry = ConfigRegistry.from_file(config_path)
    resolved = registry.resolve_vault()
    status: dict[str, Any] = registry.public_status(resolved)

    assert resolved.settings.retrieval.embedding.candidate_limit == 80
    assert resolved.settings.retrieval.embedding.max_sequence_length == 256
    assert status["telemetry"]["retention_days"] == 30
    assert "model_path" not in status["retrieval"]["embedding"]
    assert resolved.settings.retrieval.execution.max_concurrency == 4
    assert "execution" not in status["retrieval"]
    assert status["archive"]["automatic_purge"] is False
    assert status["quality_gate"] == {
        "mode": "off",
        "policy_version": "query-quality-policy-v0",
    }


def test_registry_decodes_quality_gate_into_immutable_snapshot(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "vault")
    config_path = tmp_path / "config.yaml"
    write_global_config(
        config_path,
        vault_name="primary",
        vault_root=vault,
        quality_gate={"mode": "shadow", "policy_version": "policy-v1"},
    )

    registry = ConfigRegistry.from_file(config_path)
    resolved = registry.resolve_vault()

    assert resolved.settings.quality_gate == QualityGateSettings(mode="shadow", policy_version="policy-v1")
    assert registry.public_status(resolved)["quality_gate"] == {
        "mode": "shadow",
        "policy_version": "policy-v1",
    }
    with pytest.raises((AttributeError, TypeError)):
        resolved.settings.quality_gate.mode = "off"  # type: ignore[misc]
    with pytest.raises(TypeError):
        registry.config.vaults["other"] = resolved.settings  # type: ignore[index]


def test_registry_decodes_trusted_query_execution_bounds_without_public_exposure(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "vault")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "default_vault": "primary",
                "vaults": {
                    "primary": {
                        "root": str(vault),
                        "retrieval": {"execution": {"max_concurrency": 7, "cancel_grace_seconds": 0.5}},
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    registry = ConfigRegistry.from_file(config_path)
    resolved = registry.resolve_vault()
    assert resolved.settings.retrieval.execution.max_concurrency == 7
    assert resolved.settings.retrieval.execution.cancel_grace_seconds == pytest.approx(0.5)
    retrieval_status = registry.public_status(resolved)["retrieval"]
    assert isinstance(retrieval_status, dict)
    assert "execution" not in retrieval_status


@pytest.mark.parametrize(
    ("patch", "code"),
    [
        ({"unknown": True}, "unknown_config_field"),
        ({"retrieval": {"embedding": {"provider": "remote"}}}, "vector_provider_unsupported"),
        ({"retrieval": {"embedding": {"api_key": "secret"}}}, "unknown_config_field"),
        ({"retrieval": {"embedding": {"device": 42}}}, "invalid_config"),
        ({"retrieval": {"query_version": "v1"}}, "invalid_config"),
        ({"retrieval": {"lexical_enabled": False}}, "invalid_config"),
        ({"privacy": {"redaction_rule_version": "token=secret"}}, "invalid_config"),
        ({"telemetry": {"retention_days": 0}}, "invalid_config"),
        ({"telemetry": {"store_query_body": True}}, "invalid_config"),
        ({"quality_gate": {"unknown": True}}, "unknown_config_field"),
        ({"quality_gate": {"mode": "on"}}, "invalid_config"),
        ({"quality_gate": {"policy_version": "token=secret"}}, "invalid_config"),
    ],
)
def test_registry_rejects_unknown_unsafe_and_out_of_range_profile_fields(tmp_path: Path, patch: dict[str, object], code: str) -> None:
    vault = _make_vault(tmp_path / "vault")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "default_vault": "primary", "vaults": {"primary": {"root": str(vault), **patch}}}), encoding="utf-8")
    with pytest.raises(RuntimeConfigError) as exc_info:
        ConfigRegistry.from_file(config_path)
    assert exc_info.value.code == code


def test_registry_rejects_enabled_missing_local_model(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "vault")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"schema_version": 1, "default_vault": "primary", "vaults": {"primary": {"root": str(vault), "retrieval": {"embedding": {"enabled": True, "model_path": str(tmp_path / "missing-model")}}}}}), encoding="utf-8")
    with pytest.raises(RuntimeConfigError) as exc_info:
        ConfigRegistry.from_file(config_path)
    assert exc_info.value.code == "model_missing"


def test_write_global_config_recursively_merges_nested_retrieval_profile(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "vault")
    model = tmp_path / "model"
    model.mkdir()
    config_path = tmp_path / "config.yaml"
    write_global_config(
        config_path,
        vault_name="primary",
        vault_root=vault,
        retrieval={
            "lexical_enabled": False,
            "query_version": "v2",
            "embedding": {"enabled": True, "model_path": str(model), "candidate_limit": 40},
            "context": {"response_mode": "legacy", "hard_budget_tokens": 2048},
        },
    )

    write_global_config(
        config_path,
        vault_name="primary",
        vault_root=vault,
        make_default=False,
        retrieval={"embedding": {"candidate_limit": 80}},
    )

    registry = ConfigRegistry.from_file(config_path)
    settings = registry.resolve_vault().settings.retrieval
    assert settings.lexical_enabled is False
    assert settings.query_version == "v2"
    assert settings.embedding.candidate_limit == 80
    assert settings.context.response_mode == "legacy"
    assert settings.context.hard_budget_tokens == 2048


def test_registry_snapshot_vaults_cannot_be_mutated(tmp_path: Path) -> None:
    vault = _make_vault(tmp_path / "vault")
    config_path = tmp_path / "config.yaml"
    write_global_config(config_path, vault_name="primary", vault_root=vault)

    registry = ConfigRegistry.from_file(config_path)
    with pytest.raises(TypeError):
        registry.config.vaults["other"] = registry.config.vaults["primary"]  # type: ignore[index]
