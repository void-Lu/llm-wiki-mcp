from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

import yaml

from runtime.platform_paths import global_config_path, user_data_dir

VAULT_ROOT_ENV = "LLM_WIKI_VAULT_ROOT"
CONFIG_SCHEMA_VERSION = 1


class RuntimeConfigError(RuntimeError):
    def __init__(self, message: str, *, code: str = "missing_vault_root", config_path: Path | None = None):
        super().__init__(message)
        self.code = code
        self.config_path = config_path


@dataclass(frozen=True)
class EmbeddingSettings:
    enabled: bool = False
    provider: str = "local_bge_m3"
    model_path: Path | None = None
    index_path: Path | None = None
    device: str = "cpu"
    batch_size: int = 16
    max_sequence_length: int = 256
    candidate_limit: int = 50
    rrf_k: int = 60
    min_vector_score: float = 0.5


@dataclass(frozen=True)
class ContextSettings:
    response_mode: Literal["context_pack", "legacy"] = "context_pack"
    hard_budget_tokens: int = 16_000


@dataclass(frozen=True)
class RetrievalSettings:
    lexical_enabled: bool = True
    query_version: Literal["v2"] = "v2"
    embedding: EmbeddingSettings = EmbeddingSettings()
    context: ContextSettings = ContextSettings()


@dataclass(frozen=True)
class PrivacySettings:
    credential_redaction_enabled: bool = True
    redaction_rule_version: str = "v1"
    pii_policy: Literal["preserve", "redact"] = "preserve"


@dataclass(frozen=True)
class TelemetrySettings:
    enabled: bool = True
    retention_days: int = 90
    store_query_body: bool = False


@dataclass(frozen=True)
class ArchiveSettings:
    archive_index_enabled: bool = True
    index_snapshot_ttl_days: int = 7
    automatic_purge: bool = False
    purge_after_days: int | None = None


@dataclass(frozen=True)
class VaultSettings:
    name: str
    root: Path
    retrieval: RetrievalSettings = RetrievalSettings()
    privacy: PrivacySettings = PrivacySettings()
    telemetry: TelemetrySettings = TelemetrySettings()
    archive: ArchiveSettings = ArchiveSettings()


@dataclass(frozen=True)
class GlobalConfig:
    schema_version: int
    default_vault: str | None
    tool_profile: Literal["core", "worker"]
    vaults: Mapping[str, VaultSettings]


@dataclass(frozen=True)
class ResolvedVault:
    name: str
    root: Path
    settings: VaultSettings
    resolution_source: Literal["config", "default", "env", "legacy"]


@dataclass(frozen=True)
class RuntimeConfig:
    vault_root: Path
    vault_name: str
    vault_storage_id: str
    resolution_source: str
    global_config_path: Path
    sources_config_path: Path
    data_root: Path
    vault_data_root: Path
    chroma_path: Path
    manifest_path: Path
    embedding_cache_path: Path

    @property
    def user_data_root(self) -> Path:
        return self.data_root

    @property
    def vault_storage_dir(self) -> Path:
        return self.vault_data_root


def _slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value.strip().lower())
    return re.sub(r"-+", "-", slug).strip("-") or "vault"


def _resolved_path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def _resolve_required_absolute_path(value: str | Path, *, description: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise RuntimeConfigError(f"{description} must be an absolute path; got {value}.", code="invalid_path")
    return path.resolve()


def _normalize_storage_hash_path(path_text: str, *, case_sensitive: bool | None = None) -> str:
    if case_sensitive is None:
        case_sensitive = os.name != "nt"
    return path_text if case_sensitive else path_text.replace("/", "\\").casefold()


def vault_storage_id(vault_root: str | Path) -> str:
    root = _resolved_path(vault_root)
    digest = hashlib.sha256(_normalize_storage_hash_path(str(root)).encode("utf-8")).hexdigest()[:10]
    return f"{_slug(root.name)}-{digest}"


def _mapping(value: object, name: str, path: Path) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise RuntimeConfigError(f"{name} must be an object", code="invalid_config", config_path=path)
    return value


def _merge_mapping(existing: object, updates: Mapping[str, Any]) -> dict[str, Any]:
    """Merge nested profile updates without discarding sibling settings."""
    merged = dict(existing) if isinstance(existing, Mapping) else {}
    for key, value in updates.items():
        merged[key] = _merge_mapping(merged.get(key), value) if isinstance(value, Mapping) else value
    return merged


def _unknown_keys(values: Mapping[str, Any], allowed: set[str], name: str, path: Path) -> None:
    unknown = sorted(set(values) - allowed)
    if unknown:
        raise RuntimeConfigError(f"unknown {name} field(s): {', '.join(unknown)}", code="unknown_config_field", config_path=path)


def _bool(value: object, default: bool, name: str, path: Path) -> bool:
    if value is None:
        return default
    if type(value) is not bool:
        raise RuntimeConfigError(f"{name} must be a boolean", code="invalid_config", config_path=path)
    return value


def _integer(value: object, default: int, minimum: int, maximum: int, name: str, path: Path) -> int:
    if value is None:
        return default
    if type(value) is not int or not minimum <= value <= maximum:
        raise RuntimeConfigError(f"{name} must be an integer between {minimum} and {maximum}", code="invalid_config", config_path=path)
    return value


def _number(value: object, default: float, minimum: float, maximum: float, name: str, path: Path) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not minimum <= float(value) <= maximum:
        raise RuntimeConfigError(f"{name} must be a number between {minimum} and {maximum}", code="invalid_config", config_path=path)
    return float(value)


def _decode_embedding(value: object, path: Path) -> EmbeddingSettings:
    raw = _mapping(value or {}, "embedding", path)
    _unknown_keys(raw, {"enabled", "provider", "model_path", "device", "batch_size", "max_sequence_length", "candidate_limit", "rrf_k", "min_vector_score"}, "embedding", path)
    provider = raw.get("provider", "local_bge_m3")
    if provider != "local_bge_m3":
        raise RuntimeConfigError("only local_bge_m3 embedding is supported", code="vector_provider_unsupported", config_path=path)
    model_value = raw.get("model_path")
    if model_value is not None and (not isinstance(model_value, str) or not model_value):
        raise RuntimeConfigError("embedding.model_path must be a non-empty absolute path", code="invalid_config", config_path=path)
    model_path = _resolve_required_absolute_path(model_value, description="embedding.model_path") if model_value else None
    enabled = _bool(raw.get("enabled"), model_path is not None, "embedding.enabled", path)
    if enabled and (model_path is None or not model_path.is_dir()):
        raise RuntimeConfigError("configured local embedding model is missing", code="model_missing", config_path=path)
    device = raw.get("device", "cpu")
    if not isinstance(device, str) or not device:
        raise RuntimeConfigError("embedding.device must be a non-empty string", code="invalid_config", config_path=path)
    return EmbeddingSettings(
        enabled=enabled,
        provider=provider,
        model_path=model_path,
        device=device,
        batch_size=_integer(raw.get("batch_size"), 16, 1, 256, "embedding.batch_size", path),
        max_sequence_length=_integer(raw.get("max_sequence_length"), 256, 64, 8192, "embedding.max_sequence_length", path),
        candidate_limit=_integer(raw.get("candidate_limit"), 50, 1, 500, "embedding.candidate_limit", path),
        rrf_k=_integer(raw.get("rrf_k"), 60, 1, 10_000, "embedding.rrf_k", path),
        min_vector_score=_number(raw.get("min_vector_score"), 0.5, -1.0, 1.0, "embedding.min_vector_score", path),
    )


def _decode_vault(name: str, value: object, path: Path) -> VaultSettings:
    raw = _mapping(value, f"vaults.{name}", path)
    _unknown_keys(raw, {"root", "retrieval", "privacy", "telemetry", "archive"}, f"vaults.{name}", path)
    root = raw.get("root")
    if not isinstance(root, str) or not root:
        raise RuntimeConfigError(f"vaults.{name}.root is required", code="invalid_config", config_path=path)
    retrieval_raw = _mapping(raw.get("retrieval", {}), "retrieval", path)
    _unknown_keys(retrieval_raw, {"lexical_enabled", "query_version", "embedding", "context"}, "retrieval", path)
    query_version = retrieval_raw.get("query_version", "v2")
    if query_version != "v2":
        raise RuntimeConfigError("retrieval.query_version only supports v2", code="invalid_config", config_path=path)
    lexical_enabled = _bool(retrieval_raw.get("lexical_enabled"), True, "retrieval.lexical_enabled", path)
    embedding = _decode_embedding(retrieval_raw.get("embedding", {}), path)
    if not lexical_enabled and not embedding.enabled:
        raise RuntimeConfigError("retrieval.lexical_enabled=false requires local embedding to be enabled", code="invalid_config", config_path=path)
    context_raw = _mapping(retrieval_raw.get("context", {}), "retrieval.context", path)
    _unknown_keys(context_raw, {"response_mode", "hard_budget_tokens"}, "retrieval.context", path)
    response_mode = context_raw.get("response_mode", "context_pack")
    if response_mode not in {"context_pack", "legacy"}:
        raise RuntimeConfigError("retrieval.context.response_mode is invalid", code="invalid_config", config_path=path)
    privacy_raw = _mapping(raw.get("privacy", {}), "privacy", path)
    _unknown_keys(privacy_raw, {"credential_redaction_enabled", "redaction_rule_version", "pii_policy"}, "privacy", path)
    pii_policy = privacy_raw.get("pii_policy", "preserve")
    if pii_policy not in {"preserve", "redact"}:
        raise RuntimeConfigError("privacy.pii_policy is invalid", code="invalid_config", config_path=path)
    redaction_rule_version = privacy_raw.get("redaction_rule_version", "v1")
    if not isinstance(redaction_rule_version, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", redaction_rule_version):
        raise RuntimeConfigError("privacy.redaction_rule_version must be a safe version identifier", code="invalid_config", config_path=path)
    telemetry_raw = _mapping(raw.get("telemetry", {}), "telemetry", path)
    _unknown_keys(telemetry_raw, {"enabled", "retention_days", "store_query_body"}, "telemetry", path)
    archive_raw = _mapping(raw.get("archive", {}), "archive", path)
    _unknown_keys(archive_raw, {"archive_index_enabled", "index_snapshot_ttl_days", "automatic_purge", "purge_after_days"}, "archive", path)
    purge_after = archive_raw.get("purge_after_days")
    if purge_after is not None:
        purge_after = _integer(purge_after, 1, 1, 36_500, "archive.purge_after_days", path)
    store_query_body = _bool(telemetry_raw.get("store_query_body"), False, "telemetry.store_query_body", path)
    if store_query_body:
        raise RuntimeConfigError("telemetry.store_query_body must remain false", code="invalid_config", config_path=path)
    return VaultSettings(
        name=name,
        root=_resolve_required_absolute_path(root, description=f"global config {path} value vaults.{name}.root"),
        retrieval=RetrievalSettings(
            lexical_enabled=lexical_enabled,
            query_version=query_version,
            embedding=embedding,
            context=ContextSettings(response_mode=response_mode, hard_budget_tokens=_integer(context_raw.get("hard_budget_tokens"), 16_000, 512, 200_000, "retrieval.context.hard_budget_tokens", path)),
        ),
        privacy=PrivacySettings(
            credential_redaction_enabled=_bool(privacy_raw.get("credential_redaction_enabled"), True, "privacy.credential_redaction_enabled", path),
            redaction_rule_version=redaction_rule_version,
            pii_policy=pii_policy,
        ),
        telemetry=TelemetrySettings(
            enabled=_bool(telemetry_raw.get("enabled"), True, "telemetry.enabled", path),
            retention_days=_integer(telemetry_raw.get("retention_days"), 90, 1, 3_650, "telemetry.retention_days", path),
            store_query_body=store_query_body,
        ),
        archive=ArchiveSettings(
            archive_index_enabled=_bool(archive_raw.get("archive_index_enabled"), True, "archive.archive_index_enabled", path),
            index_snapshot_ttl_days=_integer(archive_raw.get("index_snapshot_ttl_days"), 7, 0, 365, "archive.index_snapshot_ttl_days", path),
            automatic_purge=_bool(archive_raw.get("automatic_purge"), False, "archive.automatic_purge", path),
            purge_after_days=purge_after,
        ),
    )


def decode_global_config(raw: object, config_path: str | Path | None = None) -> GlobalConfig:
    path = _resolved_path(config_path) if config_path is not None else global_config_path()
    if raw is None:
        raw = {}
    root = _mapping(raw, "config", path)
    _unknown_keys(root, {"schema_version", "default_vault", "tool_profile", "vaults"}, "config", path)
    schema_version = root.get("schema_version", CONFIG_SCHEMA_VERSION)
    if schema_version != CONFIG_SCHEMA_VERSION:
        raise RuntimeConfigError("unsupported config schema_version", code="unsupported_config_schema", config_path=path)
    default_vault = root.get("default_vault")
    if default_vault is not None and (not isinstance(default_vault, str) or not default_vault):
        raise RuntimeConfigError("default_vault must be a non-empty string", code="invalid_config", config_path=path)
    profile = root.get("tool_profile", "core")
    if profile not in {"core", "worker"}:
        raise RuntimeConfigError("tool_profile must be core or worker", code="invalid_config", config_path=path)
    vaults_raw = _mapping(root.get("vaults", {}), "vaults", path)
    vaults = {name: _decode_vault(name, value, path) for name, value in vaults_raw.items() if isinstance(name, str) and name}
    if len(vaults) != len(vaults_raw):
        raise RuntimeConfigError("vault names must be non-empty strings", code="invalid_config", config_path=path)
    if default_vault is not None and default_vault not in vaults:
        raise RuntimeConfigError("default_vault is not configured", code="missing_default_vault", config_path=path)
    return GlobalConfig(CONFIG_SCHEMA_VERSION, default_vault, profile, MappingProxyType(vaults))


def load_global_config(config_path: str | Path | None = None) -> dict[str, Any]:
    path = _resolved_path(config_path) if config_path is not None else global_config_path()
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RuntimeConfigError("unable to read config.yaml", code="invalid_config", config_path=path) from exc
    if loaded is not None and not isinstance(loaded, dict):
        raise RuntimeConfigError("config.yaml must contain an object", code="invalid_config", config_path=path)
    return loaded or {}


@dataclass(frozen=True)
class ConfigRegistry:
    """Immutable process snapshot. Raw YAML is decoded only in this boundary."""

    config: GlobalConfig
    config_path: Path

    @classmethod
    def from_file(cls, config_path: str | Path | None = None) -> "ConfigRegistry":
        path = _resolved_path(config_path) if config_path is not None else global_config_path()
        return cls(decode_global_config(load_global_config(path), path), path)

    def resolve_vault(self, name: str | None = None) -> ResolvedVault:
        selected_name = name or self.config.default_vault
        if name is not None and name not in self.config.vaults:
            raise RuntimeConfigError(f"unknown vault: {name}", code="unknown_vault", config_path=self.config_path)
        if selected_name is not None:
            settings = self.config.vaults[selected_name]
            return ResolvedVault(selected_name, settings.root, settings, "config" if name else "default")
        env_root = os.environ.get(VAULT_ROOT_ENV)
        if env_root:
            root = _resolve_required_absolute_path(env_root, description=VAULT_ROOT_ENV)
            return ResolvedVault(root.name, root, VaultSettings(root.name, root), "env")
        raise RuntimeConfigError(
            "No Obsidian vault root is configured. Run `llm-wiki-mcp init --vault <name> --root <vault-path> --default` "
            f"to write {self.config_path}, or set {VAULT_ROOT_ENV} for development and automation.",
            code="missing_default_vault",
            config_path=self.config_path,
        )

    def public_status(self, vault: ResolvedVault) -> dict[str, object]:
        settings = vault.settings
        return {
            "schema_version": self.config.schema_version,
            "logical_vault": vault.name,
            "resolution_source": vault.resolution_source,
            "tool_profile": self.config.tool_profile,
            "retrieval": {
                "lexical_enabled": settings.retrieval.lexical_enabled,
                "query_version": settings.retrieval.query_version,
                "embedding": {
                    "enabled": settings.retrieval.embedding.enabled,
                    "provider": settings.retrieval.embedding.provider,
                    "model_configured": settings.retrieval.embedding.model_path is not None,
                    "model_available": bool(settings.retrieval.embedding.model_path and settings.retrieval.embedding.model_path.exists()),
                    "device": settings.retrieval.embedding.device,
                },
                "context": {"response_mode": settings.retrieval.context.response_mode, "hard_budget_tokens": settings.retrieval.context.hard_budget_tokens},
            },
            "privacy": {"credential_redaction_enabled": settings.privacy.credential_redaction_enabled, "redaction_rule_version": settings.privacy.redaction_rule_version, "pii_policy": settings.privacy.pii_policy},
            "telemetry": {"enabled": settings.telemetry.enabled, "retention_days": settings.telemetry.retention_days, "store_query_body": settings.telemetry.store_query_body},
            "archive": {"archive_index_enabled": settings.archive.archive_index_enabled, "index_snapshot_ttl_days": settings.archive.index_snapshot_ttl_days, "automatic_purge": settings.archive.automatic_purge, "purge_configured": settings.archive.purge_after_days is not None},
            "restart_required_for_changes": True,
        }


def write_global_config(config_path: str | Path | None, *, vault_name: str, vault_root: str | Path, make_default: bool = True, retrieval: Mapping[str, Any] | None = None, privacy: Mapping[str, Any] | None = None, telemetry: Mapping[str, Any] | None = None, archive: Mapping[str, Any] | None = None, tool_profile: Literal["core", "worker"] | None = None) -> Path:
    path = _resolved_path(config_path) if config_path is not None else global_config_path()
    raw = load_global_config(path)
    # Validate existing state before extending it; this prevents a CLI write from preserving invalid YAML.
    if raw:
        decode_global_config(raw, path)
    vaults = raw.setdefault("vaults", {})
    if not isinstance(vaults, dict):
        raise RuntimeConfigError("vaults must be an object", code="invalid_config", config_path=path)
    entry: dict[str, Any] = dict(vaults.get(vault_name) or {})
    entry["root"] = str(_resolve_required_absolute_path(vault_root, description="vault root"))
    for key, value in (("retrieval", retrieval), ("privacy", privacy), ("telemetry", telemetry), ("archive", archive)):
        if value is not None:
            entry[key] = _merge_mapping(entry.get(key), value)
    vaults[vault_name] = entry
    raw["schema_version"] = CONFIG_SCHEMA_VERSION
    if tool_profile is not None:
        raw["tool_profile"] = tool_profile
    if make_default or not raw.get("default_vault"):
        raw["default_vault"] = vault_name
    decode_global_config(raw, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _missing_sources_error(vault_root: Path, sources_config_path: Path) -> RuntimeConfigError:
    return RuntimeConfigError(f"Vault root {vault_root} does not contain rag/sources.yaml.", code="missing_sources_config")


def resolve_runtime_config(vault_root_arg: str | Path | None = None, config_path: str | Path | None = None, data_root: str | Path | None = None, require_sources_config: bool = True) -> RuntimeConfig:
    registry = ConfigRegistry.from_file(config_path)
    if vault_root_arg is not None:
        vault_root = _resolve_required_absolute_path(vault_root_arg, description="vault_root")
        vault_name, source = vault_root.name, "argument"
    else:
        # Keep the legacy CLI/runtime precedence stable.  The MCP resolver has
        # the newer logical-vault-first ordering in ``resolve_tool_vault``.
        env_root = os.environ.get(VAULT_ROOT_ENV)
        if env_root:
            vault_root = _resolve_required_absolute_path(env_root, description=VAULT_ROOT_ENV)
            vault_name, source = vault_root.name, "env"
        else:
            try:
                resolved = registry.resolve_vault()
            except RuntimeConfigError as exc:
                if exc.code != "missing_default_vault":
                    raise
                raise RuntimeConfigError(str(exc), code="missing_vault_root", config_path=exc.config_path) from exc
            vault_root, vault_name, source = resolved.root, resolved.name, "global_config"
    sources_config_path = vault_root / "rag" / "sources.yaml"
    if require_sources_config and not sources_config_path.exists():
        raise _missing_sources_error(vault_root, sources_config_path)
    root_data = _resolved_path(data_root) if data_root is not None else user_data_dir()
    vault_data_root = vault_root / ".rag-index"
    return RuntimeConfig(vault_root, vault_name, vault_storage_id(vault_root), source, registry.config_path, sources_config_path, root_data, vault_data_root, vault_data_root / "chroma", vault_data_root / "index-manifest.json", vault_root / ".models")
