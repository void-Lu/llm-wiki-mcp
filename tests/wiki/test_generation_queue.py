from __future__ import annotations

import pytest

from wiki.generation_queue import GenerationQueue


def test_retired_capsule_job_types_cannot_be_created(tmp_path) -> None:
    queue = GenerationQueue(tmp_path)

    with pytest.raises(ValueError, match="disabled"):
        queue.create(
            job_type="source_capsule",
            target_path="wiki/sources/a/capsules/x.md",
            sources={"raw/sources/a/x.md": "one"},
            prompt_version="v1",
            schema_version=1,
        )


def test_retired_queued_jobs_are_superseded_and_never_claimed(tmp_path) -> None:
    queue = GenerationQueue(tmp_path)
    with queue._connection() as connection:  # noqa: SLF001 - seed a legacy row
        connection.execute(
            "INSERT INTO generation_jobs(job_id,job_type,target_path,state,prompt_version,schema_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
            ("legacy-job", "chat_source_capsule", "wiki/sources/chat/capsules/x.md", "pending", "v1", 1, "2026-08-03T00:00:00+00:00", "2026-08-03T00:00:00+00:00"),
        )

    assert queue.claim("worker")["job"] is None
    assert queue.supersede_job_types({"source_capsule", "chat_source_capsule"}) == ["legacy-job"]
    assert queue.get("legacy-job")["state"] == "superseded"
