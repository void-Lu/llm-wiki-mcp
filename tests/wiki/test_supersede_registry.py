from __future__ import annotations

import sqlite3
from pathlib import Path

from wiki.supersede_registry import SupersedeRegistry


def _seed_job(
    registry: SupersedeRegistry,
    job_id: str,
    state: str,
    source_path: str,
    *,
    created_at: str,
) -> None:
    with registry._connection() as connection:  # noqa: SLF001 - seed durable legacy rows
        connection.execute(
            "INSERT INTO generation_jobs(job_id,job_type,target_path,state,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (job_id, "legacy", f"wiki/{job_id}.md", state, created_at, created_at),
        )
        connection.execute(
            "INSERT INTO job_sources(job_id,source_path,source_hash) VALUES(?,?,?)",
            (job_id, source_path, f"hash-{job_id}"),
        )


def _create_legacy_state(root: Path, *, with_sources: bool = True) -> Path:
    path = root / ".llm-wiki" / "state.sqlite3"
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE generation_jobs("
            "job_id TEXT PRIMARY KEY,job_type TEXT NOT NULL,target_path TEXT NOT NULL,"
            "state TEXT NOT NULL,prompt_version TEXT NOT NULL,schema_version INTEGER NOT NULL,"
            "created_at TEXT NOT NULL,updated_at TEXT NOT NULL)"
        )
        if with_sources:
            connection.execute(
                "CREATE TABLE job_sources("
                "job_id TEXT NOT NULL,source_path TEXT NOT NULL,source_hash TEXT NOT NULL,"
                "PRIMARY KEY(job_id,source_path))"
            )
        connection.execute(
            "INSERT INTO generation_jobs(job_id,job_type,target_path,state,prompt_version,schema_version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                "legacy-job",
                "source_capsule",
                "wiki/sources/capsules/readme.md",
                "pending",
                "v1",
                1,
                "2026-08-03T00:00:00+00:00",
                "2026-08-03T00:00:00+00:00",
            ),
        )
        if with_sources:
            connection.execute(
                "INSERT INTO job_sources(job_id,source_path,source_hash) VALUES(?,?,?)",
                ("legacy-job", "raw/sources/file/default/readme.md", "legacy-hash"),
            )
    return path


def test_supersede_sources_marks_matching_active_jobs_only(tmp_path: Path) -> None:
    registry = SupersedeRegistry(tmp_path)
    source = "raw/sources/file/default/readme.md"
    _seed_job(registry, "pending-job", "pending", source, created_at="2026-08-03T00:00:02+00:00")
    _seed_job(registry, "leased-job", "leased", source, created_at="2026-08-03T00:00:01+00:00")
    _seed_job(registry, "failed-job", "failed", source, created_at="2026-08-03T00:00:03+00:00")
    _seed_job(registry, "applied-job", "applied", source, created_at="2026-08-03T00:00:00+00:00")
    _seed_job(registry, "other-job", "pending", "raw/sources/file/default/other.md", created_at="2026-08-03T00:00:04+00:00")

    assert registry.supersede_sources({source}) == ["leased-job", "pending-job", "failed-job"]
    assert registry.status() == {
        "path": ".llm-wiki/supersede-state.sqlite3",
        "total": 5,
        "counts": {"applied": 1, "pending": 1, "superseded": 3},
    }


def test_supersede_sources_returns_empty_for_empty_or_unmatched_paths(tmp_path: Path) -> None:
    registry = SupersedeRegistry(tmp_path)
    _seed_job(
        registry,
        "pending-job",
        "pending",
        "raw/sources/file/default/readme.md",
        created_at="2026-08-03T00:00:00+00:00",
    )

    assert registry.supersede_sources(set()) == []
    assert registry.supersede_sources({"raw/sources/file/default/other.md"}) == []
    assert registry.status()["counts"] == {"pending": 1}


def test_legacy_generation_rows_are_lazily_migrated_and_supersedable(tmp_path: Path) -> None:
    legacy_path = _create_legacy_state(tmp_path)
    before = legacy_path.read_bytes()

    registry = SupersedeRegistry(tmp_path)

    assert registry.path.exists()
    assert registry.status()["counts"] == {"pending": 1}
    assert registry.supersede_sources({"raw/sources/file/default/readme.md"}) == ["legacy-job"]
    assert registry.status()["counts"] == {"superseded": 1}
    assert legacy_path.read_bytes() == before


def test_missing_legacy_generation_table_creates_empty_registry(tmp_path: Path) -> None:
    legacy_path = tmp_path / ".llm-wiki" / "state.sqlite3"
    legacy_path.parent.mkdir(parents=True)
    with sqlite3.connect(legacy_path) as connection:
        connection.execute("CREATE TABLE unrelated(value TEXT)")

    registry = SupersedeRegistry(tmp_path)

    assert registry.status() == {
        "path": ".llm-wiki/supersede-state.sqlite3",
        "total": 0,
        "counts": {},
    }


def test_read_status_observes_compatible_legacy_rows_without_migrating(tmp_path: Path) -> None:
    legacy_path = _create_legacy_state(tmp_path)
    before = legacy_path.read_bytes()

    assert SupersedeRegistry.read_status(tmp_path) == {
        "path": ".llm-wiki/supersede-state.sqlite3",
        "total": 1,
        "counts": {"pending": 1},
    }
    assert not (tmp_path / ".llm-wiki" / "supersede-state.sqlite3").exists()
    assert legacy_path.read_bytes() == before


def test_read_status_does_not_report_rows_that_cannot_be_migrated(tmp_path: Path) -> None:
    legacy_path = tmp_path / ".llm-wiki" / "state.sqlite3"
    legacy_path.parent.mkdir(parents=True)
    with sqlite3.connect(legacy_path) as connection:
        connection.execute("CREATE TABLE generation_jobs(state TEXT)")

    assert SupersedeRegistry.read_status(tmp_path) == {
        "path": ".llm-wiki/supersede-state.sqlite3",
        "total": 0,
        "counts": {},
    }
    assert not (tmp_path / ".llm-wiki" / "supersede-state.sqlite3").exists()


def test_corrupt_legacy_state_degrades_to_empty_registry(tmp_path: Path) -> None:
    legacy_path = tmp_path / ".llm-wiki" / "state.sqlite3"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_bytes(b"not a sqlite database")

    registry = SupersedeRegistry(tmp_path)

    assert registry.status()["counts"] == {}
    assert registry.path.exists()


def test_migration_with_missing_legacy_sources_table_degrades_to_empty_registry(tmp_path: Path) -> None:
    _create_legacy_state(tmp_path, with_sources=False)

    registry = SupersedeRegistry(tmp_path)

    assert registry.status()["counts"] == {}


def test_initialization_is_idempotent_after_migration(tmp_path: Path) -> None:
    _create_legacy_state(tmp_path)
    first = SupersedeRegistry(tmp_path)
    first.supersede_sources({"raw/sources/file/default/readme.md"})
    new_bytes = first.path.read_bytes()

    second = SupersedeRegistry(tmp_path)

    assert second.status()["counts"] == {"superseded": 1}
    assert second.path.read_bytes() == new_bytes
