from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "llm-wiki-mcp"
CONFIG_DIR_ENV = "LLM_WIKI_CONFIG_DIR"
USER_DATA_DIR_ENV = "LLM_WIKI_USER_DATA_DIR"
DEFAULT_VAULT_ROOT_ENV = "LLM_WIKI_DEFAULT_VAULT_ROOT"
DEFAULT_VAULT_NAME = "llm-wiki-vault"


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


def default_vault_root() -> Path:
    override = _env_override_path(DEFAULT_VAULT_ROOT_ENV)
    if override is not None:
        return override
    return (Path.home() / "Documents" / DEFAULT_VAULT_NAME).resolve()


def _windows_user_env_enabled() -> bool:
    # Avoid mutating or reading the real Windows HKCU\Environment when running
    # inside an isolated config directory (tests and sandboxed probes).
    return os.name == "nt" and not os.environ.get(CONFIG_DIR_ENV)


def read_user_env_var(name: str) -> str | None:
    """Read a persisted Windows User environment variable (HKCU\\Environment)."""

    if not _windows_user_env_enabled():
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_READ) as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def persist_user_env_var(name: str, value: str) -> bool:
    """Persist a Windows User environment variable and broadcast the change."""

    if not _windows_user_env_enabled():
        return False
    import ctypes
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)

    try:
        hwnd_broadcast = 0xFFFF
        wm_settingchange = 0x001A
        smto_abortifhung = 0x0002
        ctypes.windll.user32.SendMessageTimeoutW(
            hwnd_broadcast,
            wm_settingchange,
            0,
            "Environment",
            smto_abortifhung,
            1000,
            None,
        )
    except Exception:
        pass
    return True
