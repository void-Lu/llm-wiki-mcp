from __future__ import annotations

from netsuite_llm_wiki_mcp.knowledge_compiler import KnowledgeCompiler, capsule_path


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
