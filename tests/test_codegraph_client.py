from __future__ import annotations

import json
from pathlib import Path

from netsuite_llm_wiki_mcp.codegraph_client import CodeGraphClient


def test_status_returns_parsed_json(tmp_path: Path):
    calls: list[list[str]] = []

    def runner(args: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
        calls.append(args)
        return 0, '{"indexed": true, "files": 3}', ""

    result = CodeGraphClient(tmp_path, runner=runner).status()

    assert result["ok"] is True
    assert result["data"] == {"indexed": True, "files": 3}
    assert calls[0][1:] == ["status", str(tmp_path), "--json"]


def test_unavailable_returns_clear_error(tmp_path: Path):
    def runner(args: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
        raise FileNotFoundError("codegraph")

    result = CodeGraphClient(tmp_path, runner=runner).status()

    assert result["ok"] is False
    assert result["code"] == "codegraph_unavailable"


def test_not_initialized_returns_clear_error(tmp_path: Path):
    def runner(args: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
        return 1, "", "CodeGraph not initialized"

    result = CodeGraphClient(tmp_path, runner=runner).status()

    assert result["ok"] is False
    assert result["code"] == "codegraph_not_initialized"


def test_context_query_and_impact_use_json_commands(tmp_path: Path):
    calls: list[list[str]] = []

    def runner(args: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
        calls.append(args)
        return 0, json.dumps({"tool": args[1], "result": []}), ""

    client = CodeGraphClient(tmp_path, runner=runner)

    assert client.files()["ok"] is True
    assert client.context("map request flow")["ok"] is True
    assert client.query("Suitelet")["ok"] is True
    assert client.callers("onRequest")["ok"] is True
    assert client.callees("onRequest")["ok"] is True
    assert client.impact("onRequest")["ok"] is True
    assert [call[1:] for call in calls] == [
        ["files", "--path", str(tmp_path), "--json"],
        ["context", "map request flow", "--path", str(tmp_path), "--format", "json"],
        ["query", "Suitelet", "--json"],
        ["callers", "onRequest", "--json"],
        ["callees", "onRequest", "--json"],
        ["impact", "onRequest", "--json"],
    ]
