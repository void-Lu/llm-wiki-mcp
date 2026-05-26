from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Callable

Runner = Callable[[list[str], Path, int], tuple[int, str, str]]


class CodeGraphClient:
    def __init__(self, project_path: str | Path, runner: Runner | None = None, timeout: int = 60):
        self.project_path = Path(project_path).expanduser().resolve()
        self._runner = runner or _subprocess_runner
        self.timeout = timeout

    def status(self) -> dict[str, Any]:
        return self._run(["codegraph", "status", str(self.project_path), "--json"])

    def files(self) -> dict[str, Any]:
        return self._run(["codegraph", "files", str(self.project_path), "--json"])

    def context(self, query: str) -> dict[str, Any]:
        return self._run(["codegraph", "context", query, "--path", str(self.project_path), "--format", "json"])

    def query(self, query: str) -> dict[str, Any]:
        return self._run(["codegraph", "query", query, "--json"])

    def callers(self, symbol: str) -> dict[str, Any]:
        return self._run(["codegraph", "callers", symbol, "--json"])

    def callees(self, symbol: str) -> dict[str, Any]:
        return self._run(["codegraph", "callees", symbol, "--json"])

    def impact(self, symbol: str) -> dict[str, Any]:
        return self._run(["codegraph", "impact", symbol, "--json"])

    def _run(self, args: list[str]) -> dict[str, Any]:
        try:
            return_code, stdout, stderr = self._runner(args, self.project_path, self.timeout)
        except FileNotFoundError:
            return {"ok": False, "code": "codegraph_unavailable", "error": "codegraph executable not found"}
        except subprocess.TimeoutExpired:
            return {"ok": False, "code": "codegraph_timeout", "error": "codegraph command timed out"}
        if return_code != 0:
            text = f"{stdout}\n{stderr}".lower()
            if "not initialized" in text or "run 'codegraph init" in text:
                return {"ok": False, "code": "codegraph_not_initialized", "error": stderr or stdout}
            return {"ok": False, "code": "codegraph_failed", "error": stderr or stdout, "returncode": return_code}
        try:
            data = json.loads(stdout) if stdout.strip() else {}
        except json.JSONDecodeError as exc:
            return {"ok": False, "code": "invalid_codegraph_json", "error": str(exc), "raw": stdout}
        return {"ok": True, "data": data}


def _subprocess_runner(args: list[str], cwd: Path, timeout: int) -> tuple[int, str, str]:
    completed = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=timeout, check=False)
    return completed.returncode, completed.stdout, completed.stderr
