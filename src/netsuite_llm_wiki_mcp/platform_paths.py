from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "netsuite-llm-wiki-mcp"
CONFIG_DIR_ENV = "NETSUITE_LLM_WIKI_CONFIG_DIR"
USER_DATA_DIR_ENV = "NETSUITE_LLM_WIKI_USER_DATA_DIR"


def _env_override_path(env_var: str) -> Path | None:
    override = os.environ.get(env_var)
    if not override:
        return None

    path = Path(override).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{env_var} must be an absolute path; got {override}.")
    return path.resolve()


def user_config_dir(app_name: str = APP_NAME) -> Path:
    override = _env_override_path(CONFIG_DIR_ENV)
    if override is not None:
        return override

    if os.name == "nt":
        base = os.environ.get("APPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Roaming"
        return (root / app_name).resolve()

    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / app_name).resolve()

    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    root = Path(xdg_config) if xdg_config else Path.home() / ".config"
    return (root / app_name).resolve()


def user_data_dir(app_name: str = APP_NAME) -> Path:
    override = _env_override_path(USER_DATA_DIR_ENV)
    if override is not None:
        return override

    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        root = Path(base) if base else Path.home() / "AppData" / "Local"
        return (root / app_name).resolve()

    if sys.platform == "darwin":
        return (Path.home() / "Library" / "Application Support" / app_name).resolve()

    xdg_data = os.environ.get("XDG_DATA_HOME")
    root = Path(xdg_data) if xdg_data else Path.home() / ".local" / "share"
    return (root / app_name).resolve()


def global_config_path(app_name: str = APP_NAME) -> Path:
    return user_config_dir(app_name) / "config.yaml"
