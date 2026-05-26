from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("NETSUITE_LLM_WIKI_VAULT_ROOT", raising=False)
    monkeypatch.setenv("NETSUITE_LLM_WIKI_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("NETSUITE_LLM_WIKI_USER_DATA_DIR", str(tmp_path / "user-data"))
