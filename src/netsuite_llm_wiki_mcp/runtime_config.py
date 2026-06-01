from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from netsuite_llm_wiki_mcp.platform_paths import global_config_path, user_data_dir

VAULT_ROOT_ENV = "NETSUITE_LLM_WIKI_VAULT_ROOT"


class RuntimeConfigError(RuntimeError):
    def __init__(self, message: str, *, code: str = "missing_vault_root", config_path: Path | None = None):
        super().__init__(message)
        self.code = code
        self.config_path = config_path


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
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "vault"


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

    if case_sensitive:
        return path_text

    return path_text.replace("/", "\\").casefold()


def vault_storage_id(vault_root: str | Path) -> str:
    root = _resolved_path(vault_root)
    normalized = _normalize_storage_hash_path(str(root))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
    return f"{_slug(root.name)}-{digest}"


def load_global_config(config_path: str | Path | None = None) -> dict[str, Any]:
    path = _resolved_path(config_path) if config_path is not None else global_config_path()
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def write_global_config(
    config_path: str | Path | None,
    *,
    vault_name: str,
    vault_root: str | Path,
    make_default: bool = True,
) -> Path:
    path = _resolved_path(config_path) if config_path is not None else global_config_path()
    raw = load_global_config(path)
    vaults = raw.get("vaults")
    if not isinstance(vaults, dict):
        vaults = {}

    vaults[vault_name] = {"root": str(_resolved_path(vault_root))}
    raw["vaults"] = vaults
    if make_default or not raw.get("default_vault"):
        raw["default_vault"] = vault_name

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def _vault_from_global_config(raw: dict[str, Any], config_path: Path) -> tuple[str, Path] | None:
    vaults = raw.get("vaults")
    if not isinstance(vaults, dict):
        return None

    selected_name = raw.get("default_vault")
    if not isinstance(selected_name, str) or not selected_name:
        return None

    selected = vaults.get(selected_name)
    if not isinstance(selected, dict):
        return None

    root = selected.get("root")
    if not isinstance(root, str) or not root:
        return None

    description = f"global config {config_path} value vaults.{selected_name}.root"
    return selected_name, _resolve_required_absolute_path(root, description=description)


def _missing_config_error(config_path: Path) -> RuntimeConfigError:
    message = (
        "No Obsidian vault root is configured. Run "
        "`netsuite-llm-wiki-mcp init --vault <name> --root <vault-path> --default` "
        f"to write {config_path}, or set {VAULT_ROOT_ENV} for development and automation."
    )
    return RuntimeConfigError(message, code="missing_vault_root", config_path=config_path)


def _missing_sources_error(vault_root: Path, sources_config_path: Path) -> RuntimeConfigError:
    message = (
        f"Vault root {vault_root} does not contain rag/sources.yaml. "
        "Run `netsuite-llm-wiki-mcp init --vault <name> --root <vault-path> --default` "
        f"after creating {sources_config_path}, or set {VAULT_ROOT_ENV} to a vault with rag/sources.yaml."
    )
    return RuntimeConfigError(message, code="missing_sources_config")


def resolve_runtime_config(
    vault_root_arg: str | Path | None = None,
    config_path: str | Path | None = None,
    data_root: str | Path | None = None,
    require_sources_config: bool = True,
) -> RuntimeConfig:
    cfg_path = _resolved_path(config_path) if config_path is not None else global_config_path()
    root_data = _resolved_path(data_root) if data_root is not None else user_data_dir()

    if vault_root_arg is not None:
        vault_root = _resolved_path(vault_root_arg)
        vault_name = vault_root.name
        resolution_source = "argument"
    else:
        env_root = os.environ.get(VAULT_ROOT_ENV)
        if env_root:
            vault_root = _resolve_required_absolute_path(env_root, description=VAULT_ROOT_ENV)
            vault_name = vault_root.name
            resolution_source = "env"
        else:
            selected = _vault_from_global_config(load_global_config(cfg_path), cfg_path)
            if selected is None:
                raise _missing_config_error(cfg_path)
            vault_name, vault_root = selected
            resolution_source = "global_config"

    sources_config_path = vault_root / "rag" / "sources.yaml"
    if require_sources_config and not sources_config_path.exists():
        raise _missing_sources_error(vault_root, sources_config_path)

    storage_id = vault_storage_id(vault_root)
    vault_data_root = vault_root / ".rag-index"
    embedding_cache_path = vault_root / ".models"
    return RuntimeConfig(
        vault_root=vault_root,
        vault_name=vault_name,
        vault_storage_id=storage_id,
        resolution_source=resolution_source,
        global_config_path=cfg_path,
        sources_config_path=sources_config_path,
        data_root=root_data,
        vault_data_root=vault_data_root,
        chroma_path=vault_data_root / "chroma",
        manifest_path=vault_data_root / "index-manifest.json",
        embedding_cache_path=embedding_cache_path,
    )
