from __future__ import annotations

import json
from pathlib import Path

from app.cli import main


def _page(root: Path) -> Path:
    path = root / "wiki" / "note.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntitle: owner@example.com\n---\n\nbody\n", encoding="utf-8")
    return path


def test_cli_exposes_provenance_and_privacy_admin_plan_apply(tmp_path: Path, capsys) -> None:
    page = _page(tmp_path)
    assert main(["repair", "privacy-audit", "plan", "--vault", str(tmp_path)]) == 0
    privacy_plan = json.loads(capsys.readouterr().out)
    assert privacy_plan["kind"] == "privacy_audit"
    assert main(["repair", "privacy-audit", "apply", "--vault", str(tmp_path), "--plan-id", privacy_plan["plan_id"]]) == 0
    privacy_apply = json.loads(capsys.readouterr().out)
    assert privacy_apply["ok"] is True
    assert "owner@example.com" not in page.read_text(encoding="utf-8")

    assert main(["repair", "provenance", "plan", "--vault", str(tmp_path)]) == 0
    provenance_plan = json.loads(capsys.readouterr().out)
    assert provenance_plan["kind"] == "provenance_migration"
