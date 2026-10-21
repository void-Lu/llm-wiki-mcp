from __future__ import annotations

from wiki.concept_registry import ConceptRegistry
from wiki.ingest_service import sync_retrieval_index


def test_alias_resolution_and_raw_source_promotion_rules(tmp_path) -> None:
    path = tmp_path / "wiki/concepts/finance/invoice.md"
    path.parent.mkdir(parents=True)
    path.write_text("---\ntype: concept\nconcept_id: concept_invoice\naliases: [Invoice Approval, 发票审批]\nsources: [raw/sources/file/a/one.md, raw/sources/file/b/two.md]\n---\n\n# Invoice\n", encoding="utf-8")
    registry = ConceptRegistry(tmp_path)
    assert registry.resolve("发票审批")["action"] == "existing"
    assert registry.promotion(["a", "b"]) == "promote"
    assert registry.promotion(["a", "b"], has_chat_source=True) == "review_required"


def test_fts_and_wikilink_candidates_create_review_evidence(tmp_path) -> None:
    concept = tmp_path / "wiki/concepts/finance/invoice.md"
    concept.parent.mkdir(parents=True)
    concept.write_text("---\ntype: concept\n---\n\n# Invoice Approval\n\napproval workflow\n", encoding="utf-8")
    linked = tmp_path / "wiki/projects/p/researches/link.md"
    linked.parent.mkdir(parents=True)
    linked.write_text("# Link\n\n[[Invoice Approval]]\n", encoding="utf-8")
    sync_retrieval_index(tmp_path, full_build=True)
    registry = ConceptRegistry(tmp_path)
    resolved = registry.resolve("approval")
    assert "wiki/concepts/finance/invoice.md" in resolved["evidence"]["fts"]
    assert registry._wikilink_candidates("Invoice Approval") == ["wiki/concepts/finance/invoice.md"]


def test_registry_accepts_preloaded_records_and_can_skip_evidence_scans(tmp_path, monkeypatch) -> None:
    from wiki.concept_registry import ConceptRecord

    record = ConceptRecord("concept_x", "wiki/entities/rate-engine.md", "Rate Engine", ("Pricing Engine",), "", (), "active")
    registry = ConceptRegistry(tmp_path, records=[record])
    monkeypatch.setattr(registry, "_fts_candidates", lambda candidate: (_ for _ in ()).throw(AssertionError("fts")))
    monkeypatch.setattr(registry, "_wikilink_candidates", lambda candidate: (_ for _ in ()).throw(AssertionError("links")))

    assert registry.resolve("pricing-engine", collect_evidence=False)["action"] == "existing"
    candidate = registry.resolve("rate", collect_evidence=False)
    assert candidate["action"] == "candidate"
    assert [match.path for match in candidate["matches"]] == ["wiki/entities/rate-engine.md"]
