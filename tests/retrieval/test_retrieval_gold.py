from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from retrieval.retrieval_eval import load_retrieval_dataset
from retrieval.retrieval_gold import RetrievalGoldError, finalize_retrieval_gold, sample_retrieval_gold


def _telemetry_vault(tmp_path: Path, count: int = 12) -> Path:
    root = tmp_path / "vault"
    (root / "wiki" / "concepts").mkdir(parents=True)
    (root / "wiki" / "concepts" / "invoice.md").write_text("# Invoice\n", encoding="utf-8")
    database = root / ".llm-wiki" / "state.sqlite3"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE query_telemetry("
            "query_hash TEXT NOT NULL, normalized_query_redacted TEXT NOT NULL, at TEXT NOT NULL, "
            "expires_at TEXT NOT NULL, scope TEXT NOT NULL, project TEXT NOT NULL, passage_ids TEXT NOT NULL, "
            "fallback_level TEXT NOT NULL, token_count INTEGER NOT NULL, latency_ms REAL NOT NULL, "
            "outcome TEXT NOT NULL DEFAULT 'completed')"
        )
        for index in range(count):
            query = f"问题 {index}" if index % 2 else f"API N/record {index}"
            connection.execute(
                "INSERT INTO query_telemetry VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"hash-{index}",
                    query,
                    f"2026-08-11T00:00:{index:02d}Z",
                    "2026-12-31T00:00:00Z",
                    "knowledge" if index < count - 2 else "auto",
                    "alpha" if index < count // 2 else "",
                    "" if index % 3 == 0 else "p1",
                    "none",
                    0,
                    1.0,
                    "completed",
                ),
            )
        connection.commit()
    return root


def test_sampling_is_deterministic_and_does_not_write_telemetry(tmp_path: Path) -> None:
    vault = _telemetry_vault(tmp_path)
    database = vault / ".llm-wiki" / "state.sqlite3"
    before = database.read_bytes()
    first = sample_retrieval_gold(vault, tmp_path / "one", count=6)
    second = sample_retrieval_gold(vault, tmp_path / "two", count=6)

    assert Path(first["template"]).read_bytes() == Path(second["template"]).read_bytes()
    assert Path(first["manifest"]).read_bytes() == Path(second["manifest"]).read_bytes()
    assert database.read_bytes() == before
    records = [json.loads(line) for line in Path(first["template"]).read_text(encoding="utf-8").splitlines()]
    assert len(records) == 6
    assert all(record["answerable"] is None and record["needs_review"] is True for record in records)
    assert all("passage_ids" not in record and "historical_paths" not in record for record in records)


def test_sampling_reports_coverage_gap_without_faking_it(tmp_path: Path) -> None:
    vault = _telemetry_vault(tmp_path, count=4)
    result = sample_retrieval_gold(vault, tmp_path / "gold", count=4)

    assert result["coverage"]["gaps"]
    manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert manifest["sampling"]["coverage_gaps"]


def test_finalize_rejects_incomplete_or_review_required_annotations(tmp_path: Path) -> None:
    vault = _telemetry_vault(tmp_path)
    sampled = sample_retrieval_gold(vault, tmp_path / "gold", count=6)

    with pytest.raises(RetrievalGoldError) as error:
        finalize_retrieval_gold(sampled["template"], sampled["manifest"], vault, tmp_path / "final")
    assert error.value.code == "annotation_incomplete"

    template = Path(sampled["template"])
    records = [json.loads(line) for line in template.read_text(encoding="utf-8").splitlines()]
    for record in records:
        record.update(
            {
                "answerable": True,
                "relevant": [{"path": "wiki/concepts/invoice.md", "grade": 3}],
                "needs_review": False,
            }
        )
    template.write_text("\n".join(json.dumps(record, ensure_ascii=False) for record in records) + "\n", encoding="utf-8")
    result = finalize_retrieval_gold(sampled["template"], sampled["manifest"], vault, tmp_path / "final")

    dataset = load_retrieval_dataset(result["dataset"], result["manifest"])
    assert len(dataset.cases) == 6
    assert all(case.scope in {"knowledge", "auto"} for case in dataset.cases)
    final_manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
    assert final_manifest["status"] == "reviewed"


def test_finalize_rejects_absolute_or_missing_relevant_path(tmp_path: Path) -> None:
    vault = _telemetry_vault(tmp_path)
    sampled = sample_retrieval_gold(vault, tmp_path / "gold", count=4)
    template = Path(sampled["template"])
    records = [json.loads(line) for line in template.read_text(encoding="utf-8").splitlines()]
    for record in records:
        record.update({"answerable": True, "relevant": [{"path": "C:/secret.md", "grade": 3}], "needs_review": False})
    template.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    with pytest.raises(RetrievalGoldError) as error:
        finalize_retrieval_gold(sampled["template"], sampled["manifest"], vault, tmp_path / "final")
    assert error.value.code == "invalid_relevant_path"


def test_finalize_rejects_unsupported_filters(tmp_path: Path) -> None:
    vault = _telemetry_vault(tmp_path)
    sampled = sample_retrieval_gold(vault, tmp_path / "gold", count=4)
    template = Path(sampled["template"])
    records = [json.loads(line) for line in template.read_text(encoding="utf-8").splitlines()]
    for record in records:
        record.update(
            {
                "answerable": True,
                "relevant": [{"path": "wiki/concepts/invoice.md", "grade": 3}],
                "needs_review": False,
                "filters": {"unsupported": "value"},
            }
        )
    template.write_text("\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8")

    with pytest.raises(RetrievalGoldError, match="unsupported filters"):
        finalize_retrieval_gold(sampled["template"], sampled["manifest"], vault, tmp_path / "final")
