from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any, Callable

Runner = Callable[[list[str], Path, int], tuple[int, str, str]]

_CODEGRAPH_EXE: str | None = None


def _get_codegraph_exe() -> str:
    global _CODEGRAPH_EXE
    if _CODEGRAPH_EXE is None:
        _CODEGRAPH_EXE = shutil.which("codegraph") or shutil.which("codegraph.cmd") or "codegraph"
    return _CODEGRAPH_EXE


class CodeGraphClient:
    def __init__(self, project_path: str | Path, runner: Runner | None = None, timeout: int = 60):
        self.project_path = Path(project_path).expanduser().resolve()
        self._runner = runner or _subprocess_runner
        self.timeout = timeout

    def status(self) -> dict[str, Any]:
        return self._run([_get_codegraph_exe(), "status", str(self.project_path), "--json"])

    def files(self) -> dict[str, Any]:
        return self._run([_get_codegraph_exe(), "files", "--path", str(self.project_path), "--json"])

    def context(self, query: str) -> dict[str, Any]:
        return self._run([_get_codegraph_exe(), "context", query, "--path", str(self.project_path), "--format", "json"])

    def query(self, query: str) -> dict[str, Any]:
        return self._run([_get_codegraph_exe(), "query", query, "--json"])

    def callers(self, symbol: str) -> dict[str, Any]:
        return self._run([_get_codegraph_exe(), "callers", symbol, "--json"])

    def callees(self, symbol: str) -> dict[str, Any]:
        return self._run([_get_codegraph_exe(), "callees", symbol, "--json"])

    def impact(self, symbol: str) -> dict[str, Any]:
        return self._run([_get_codegraph_exe(), "impact", symbol, "--json"])

    def graph_snapshot(self) -> dict[str, Any]:
        db_path = self.project_path / ".codegraph" / "codegraph.db"
        if not db_path.exists():
            return {"ok": False, "code": "codegraph_db_not_found", "error": f"codegraph database not found: {db_path}"}
        try:
            connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            try:
                files = [_row_dict(row) for row in connection.execute("SELECT * FROM files ORDER BY path")]
                nodes = [_node_row_dict(row) for row in connection.execute("SELECT * FROM nodes ORDER BY file_path, start_line, kind, name")]
                edges = [_edge_row_dict(row) for row in connection.execute("SELECT * FROM edges ORDER BY source, kind, target")]
            finally:
                connection.close()
        except sqlite3.Error as exc:
            return {"ok": False, "code": "codegraph_db_error", "error": str(exc)}
        return {"ok": True, "data": {"files": files, "nodes": nodes, "edges": edges}}

    def _run(self, args: list[str]) -> dict[str, Any]:
        try:
            return_code, stdout, stderr = self._runner(args, self.project_path, self.timeout)
        except (FileNotFoundError, NotADirectoryError):
            return {"ok": False, "code": "codegraph_unavailable", "error": "codegraph executable not found or project path invalid"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "code": "codegraph_timeout", "error": "codegraph command timed out"}
        if return_code != 0:
            text = f"{stdout or ''}\n{stderr or ''}".lower()
            if "not initialized" in text or "run 'codegraph init" in text:
                return {"ok": False, "code": "codegraph_not_initialized", "error": stderr or stdout or "codegraph not initialized"}
            return {"ok": False, "code": "codegraph_failed", "error": stderr or stdout or "unknown error", "returncode": return_code}
        try:
            data = json.loads(stdout) if stdout and stdout.strip() else {}
        except json.JSONDecodeError as exc:
            return {"ok": False, "code": "invalid_codegraph_json", "error": str(exc), "raw": stdout}
        return {"ok": True, "data": data}


def _subprocess_runner(args: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    completed = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False)
    return completed.returncode, completed.stdout, completed.stderr


def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {key: row[key] for key in row.keys()}


def _node_row_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    return {
        "id": data.get("id"),
        "kind": data.get("kind"),
        "name": data.get("name"),
        "qualifiedName": data.get("qualified_name"),
        "filePath": data.get("file_path"),
        "language": data.get("language"),
        "startLine": data.get("start_line"),
        "endLine": data.get("end_line"),
        "startColumn": data.get("start_column"),
        "endColumn": data.get("end_column"),
        "docstring": data.get("docstring"),
        "signature": data.get("signature"),
        "visibility": data.get("visibility"),
        "isExported": bool(data.get("is_exported")),
        "isAsync": bool(data.get("is_async")),
        "isStatic": bool(data.get("is_static")),
        "isAbstract": bool(data.get("is_abstract")),
        "decorators": _json_or_raw(data.get("decorators")),
        "typeParameters": _json_or_raw(data.get("type_parameters")),
        "updatedAt": data.get("updated_at"),
    }


def _edge_row_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = _row_dict(row)
    return {
        "id": data.get("id"),
        "source": data.get("source"),
        "target": data.get("target"),
        "kind": data.get("kind"),
        "metadata": _json_or_raw(data.get("metadata")),
        "line": data.get("line"),
        "column": data.get("col"),
        "provenance": data.get("provenance"),
    }


def _json_or_raw(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value
