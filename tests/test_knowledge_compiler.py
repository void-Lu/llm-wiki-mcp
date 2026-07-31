from __future__ import annotations

import os
from pathlib import Path

import pytest

import netsuite_llm_wiki_mcp.knowledge_compiler as knowledge_compiler_module
from netsuite_llm_wiki_mcp.knowledge_compiler import KnowledgeCompiler, capsule_path, file_hash, filesystem_path
from netsuite_llm_wiki_mcp.wiki_io import read_markdown_page


def _result() -> dict[str, object]:
    return {"title": "SuiteQL API", "summary": "中文摘要 SuiteQL", "aliases": ["SuiteQL", "套件查询"], "keywords": ["SuiteQL", "N/query"], "coverage": ["API"], "body": "SuiteQL API 使用 N/query。fieldId custbody_test。", "uncertainties": []}


def test_capsule_path_and_chat_rejection(tmp_path) -> None:
    assert capsule_path("raw/sources/file/default/docs/readme.md").as_posix() == "wiki/sources/file/default/docs/capsules/readme.md"
    compiler = KnowledgeCompiler(tmp_path)
    assert compiler.enqueue_capsule("raw/sources/chat/a.md")["code"] == "chat_capsule_forbidden"


def test_apply_capsule_checks_source_and_target_cas(tmp_path) -> None:
    raw = tmp_path / "raw/sources/file/default/docs/readme.md"
    raw.parent.mkdir(parents=True)
    raw.write_text("# API\n\nSuiteQL", encoding="utf-8")
    compiler = KnowledgeCompiler(tmp_path)
    queued = compiler.enqueue_capsule(raw.relative_to(tmp_path))
    job = compiler.claim("test")["job"]
    assert job and job["job_id"] == queued["job"]["job_id"]
    applied = compiler.apply_capsule(job["job_id"], job["lease_token"], _result())
    assert applied["ok"]
    assert applied["navigation"]["ok"]
    written = tmp_path / applied["path"]
    assert "source_hash:" in written.read_text(encoding="utf-8")
    assert compiler.apply_capsule(job["job_id"], job["lease_token"], _result())["idempotent"]


def test_changed_raw_rejects_old_apply_and_marks_job_superseded(tmp_path) -> None:
    raw = tmp_path / "raw/sources/file/default/docs/readme.md"
    raw.parent.mkdir(parents=True)
    raw.write_text("old", encoding="utf-8")
    compiler = KnowledgeCompiler(tmp_path)
    compiler.enqueue_capsule(raw.relative_to(tmp_path))
    job = compiler.claim("test")["job"]
    raw.write_text("new", encoding="utf-8")
    assert compiler.apply_capsule(job["job_id"], job["lease_token"], _result())["code"] == "source_hash_mismatch"


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length paths are platform-specific")
def test_long_raw_path_preserves_queue_apply_source_hash_contract(tmp_path) -> None:
    nested = Path("raw/sources/file/default")
    for index in range(9):
        nested /= f"deep-provenance-segment-{index:02d}-with-descriptive-name"
    relative_raw = nested / "source-document.md"
    raw = filesystem_path(tmp_path / relative_raw)
    raw.parent.mkdir(parents=True)
    raw.write_text("# Long path source\n\nSuiteQL source content.", encoding="utf-8")
    assert len(str(tmp_path / relative_raw)) > 260

    compiler = KnowledgeCompiler(tmp_path)
    queued = compiler.enqueue_capsule(relative_raw)
    assert queued["ok"]
    job = compiler.claim("long-path-test")["job"]
    assert job and job["job_id"] == queued["job"]["job_id"]
    assert job["sources"][relative_raw.as_posix()] == file_hash(raw)

    applied = compiler.apply_capsule(job["job_id"], job["lease_token"], _result())
    assert applied["ok"]
    capsule = filesystem_path(tmp_path / applied["path"])
    page = read_markdown_page(capsule, compiler.root)
    assert page.frontmatter["type"] == "source_capsule"
    assert page.frontmatter["source_path"] == relative_raw.as_posix()
    assert page.frontmatter["source_hash"] == file_hash(raw)
    assert page.frontmatter["lifecycle"] == "active"


def test_apply_capsules_commits_each_job_and_refreshes_once(tmp_path, monkeypatch) -> None:
    compiler = KnowledgeCompiler(tmp_path)
    jobs = []
    for name in ("one", "two"):
        raw = tmp_path / f"raw/sources/file/default/{name}.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f"source {name}", encoding="utf-8")
        queued = compiler.enqueue_capsule(raw.relative_to(tmp_path))
        job = compiler.claim("batch-test")["job"]
        assert job and job["job_id"] == queued["job"]["job_id"]
        jobs.append((job["job_id"], job["lease_token"], _result()))

    refreshes = []
    monkeypatch.setattr(knowledge_compiler_module, "refresh_indexes", lambda root: refreshes.append(root) or {"ok": True, "written": ["wiki/index.md"]})
    applied = compiler.apply_capsules(jobs)

    assert applied["ok"]
    assert len(refreshes) == 1
    assert [item["ok"] for item in applied["results"]] == [True, True]
    states = []
    for job_id, _, _ in jobs:
        record = compiler.queue.get(job_id)
        assert record is not None, f"missing queue record for job {job_id}"
        states.append(record["state"])
    assert states == ["applied", "applied"]


def test_apply_capsules_isolates_failed_job_and_refreshes_successes_once(tmp_path, monkeypatch) -> None:
    compiler = KnowledgeCompiler(tmp_path)
    jobs = []
    for name in ("bad", "good"):
        raw = tmp_path / f"raw/sources/file/default/{name}.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f"source {name}", encoding="utf-8")
        compiler.enqueue_capsule(raw.relative_to(tmp_path))
        job = compiler.claim("batch-test")["job"]
        assert job
        jobs.append(job)
    invalid = dict(_result())
    invalid["title"] = ""
    refreshes = []
    monkeypatch.setattr(knowledge_compiler_module, "refresh_indexes", lambda root: refreshes.append(root) or {"ok": True, "written": ["wiki/index.md"]})

    applied = compiler.apply_capsules([(jobs[0]["job_id"], jobs[0]["lease_token"], invalid), (jobs[1]["job_id"], jobs[1]["lease_token"], _result())])

    assert not applied["ok"]
    assert applied["results"][0]["code"] == "generation_schema_invalid"
    assert applied["results"][1]["ok"]
    failed_record = compiler.queue.get(jobs[0]["job_id"])
    assert failed_record is not None, f"missing queue record for job {jobs[0]['job_id']}"
    assert failed_record["state"] == "failed"
    applied_record = compiler.queue.get(jobs[1]["job_id"])
    assert applied_record is not None, f"missing queue record for job {jobs[1]['job_id']}"
    assert applied_record["state"] == "applied"
    assert len(refreshes) == 1


def test_concept_is_promoted_only_from_non_chat_capsules(tmp_path) -> None:
    compiler = KnowledgeCompiler(tmp_path)
    assert compiler.prepare_concept(title="Invoice", source_capsules=["a", "b"])["action"] == "promote"
    assert compiler.prepare_concept(title="Invoice", source_capsules=["raw/sources/chat/a.md"])["action"] == "review_required"


def test_concept_resolves_capsule_provenance_and_raw_change_stales_it(tmp_path) -> None:
    compiler = KnowledgeCompiler(tmp_path)
    capsules: list[str] = []
    for name in ("one", "two"):
        raw = tmp_path / f"raw/sources/file/default/{name}.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text(f"source {name}", encoding="utf-8")
        compiler.enqueue_capsule(raw.relative_to(tmp_path))
        job = compiler.claim("test")["job"]
        assert job
        assert compiler.apply_capsule(job["job_id"], job["lease_token"], _result())["ok"]
        capsules.append(capsule_path(raw.relative_to(tmp_path)).as_posix())
    concept = compiler.apply_concept(title="Invoice", body="Invoice approval concept body.", aliases=["Invoice"], source_capsules=capsules)
    assert concept["ok"]
    stale = compiler.raw_changed("raw/sources/file/default/one.md")["stale"]
    assert concept["path"] in stale
    assert capsules[0] in stale


def test_concept_rejects_capsule_without_locked_raw_provenance(tmp_path) -> None:
    path = tmp_path / "wiki/sources/file/default/capsules/bad.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntype: source_capsule\ngenerated: true\n---\n\n# Bad\n", encoding="utf-8")
    result = KnowledgeCompiler(tmp_path).apply_concept(title="Bad", body="body", aliases=[], source_capsules=[path.relative_to(tmp_path).as_posix(), "wiki/sources/file/default/capsules/other.md"])
    assert result["code"] == "capsule_provenance_invalid"
