from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_SPEC = spec_from_file_location("trellis_spec_lint", Path(".trellis/scripts/spec_lint.py"))
assert _SPEC is not None and _SPEC.loader is not None
_MODULE = module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
EXPECTED_CORE_TOOLS = _MODULE.EXPECTED_CORE_TOOLS
lint_specs = _MODULE.lint_specs


def test_current_specs_have_live_anchors_and_exact_core_registry() -> None:
    result = lint_specs(Path.cwd())

    assert result["ok"] is True, result
    assert set(result["core_tools"]) == EXPECTED_CORE_TOOLS


def test_spec_lint_rejects_missing_anchor(tmp_path: Path) -> None:
    spec = tmp_path / ".trellis/spec/backend/error-handling.md"
    spec.parent.mkdir(parents=True)
    spec.write_text("- `src/missing.py:Nope`\n", encoding="utf-8")
    for relative in (
        Path(".trellis/spec/backend/logging-guidelines.md"),
        Path(".trellis/spec/backend/database-guidelines.md"),
    ):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("current\n", encoding="utf-8")
    server = tmp_path / "src/app/server.py"
    server.parent.mkdir(parents=True)
    server.write_text("def wiki_status(): pass\n", encoding="utf-8")

    result = lint_specs(tmp_path)

    assert result["ok"] is False
    assert any(error["code"] == "anchor_file_missing" for error in result["errors"])
