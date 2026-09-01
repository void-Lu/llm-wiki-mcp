from __future__ import annotations

import json
from pathlib import Path

from app.cli import main

TARGET_ROOT = "raw/sources/file/example-docs"
TARGET_PREFIX = TARGET_ROOT + "/"


def _write(root: Path, relative: str, text: str) -> None:
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_raw_replace_plan_is_exposed_as_cli_admin_command(tmp_path: Path, capsys) -> None:
    vault = tmp_path / "vault"
    source = tmp_path / "source"
    locator = f"{TARGET_PREFIX}one.md"
    _write(vault, "wiki/log.md", "# Log\n")
    _write(vault, "wiki/concepts/example.md", f"---\nsources: [{locator}]\n---\n\n# Example\n")
    _write(vault, f"{TARGET_ROOT}/one.md", "old\n")
    _write(source, "one.md", "new\n")

    assert main(
        [
            "raw-replace",
            "plan",
            "--vault",
            str(vault),
            "--source-root",
            str(source),
            "--target-path",
            TARGET_ROOT,
        ]
    ) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["dry_run"] is True
    assert payload["state"] == "ready"
    assert payload["summary"]["mapped_locator_count"] == 1
