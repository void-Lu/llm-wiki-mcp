from __future__ import annotations

from netsuite_llm_wiki_mcp.generation_queue import GenerationQueue


def test_content_addressed_jobs_and_idempotent_complete(tmp_path) -> None:
    queue = GenerationQueue(tmp_path)
    first = queue.create(job_type="source_capsule", target_path="wiki/sources/a/capsules/x.md", sources={"raw/sources/a/x.md": "one"}, prompt_version="v1", schema_version=1)
    again = queue.create(job_type="source_capsule", target_path="wiki/sources/a/capsules/x.md", sources={"raw/sources/a/x.md": "one"}, prompt_version="v1", schema_version=1)
    assert first["created"] and not again["created"]
    claimed = queue.claim("worker")["job"]
    assert claimed
    assert queue.complete(claimed["job_id"], claimed["lease_token"], "result")["ok"]
    assert queue.complete(claimed["job_id"], claimed["lease_token"], "result")["idempotent"]


def test_lease_release_fail_and_supersede(tmp_path) -> None:
    queue = GenerationQueue(tmp_path)
    created = queue.create(job_type="source_capsule", target_path="wiki/sources/a/capsules/x.md", sources={"raw/sources/a/x.md": "one"}, prompt_version="v1", schema_version=1)
    job = queue.claim("worker")["job"]
    assert queue.release(job["job_id"], job["lease_token"])["ok"]
    job = queue.claim("worker")["job"]
    assert queue.fail(job["job_id"], job["lease_token"], "bad_output")["job"]["state"] == "failed"
    assert queue.supersede_sources({"raw/sources/a/x.md"}) == [created["job"]["job_id"]]
    assert queue.get(created["job"]["job_id"])["state"] == "superseded"
