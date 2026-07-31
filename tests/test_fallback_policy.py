from netsuite_llm_wiki_mcp.fallback_policy import decide_fallback


def test_exact_evidence_requires_scoped_raw_fallback() -> None:
    decision = decide_fallback(intent="exact_evidence", top_score=1.0, eligible_formal_count=1, citation_count=1, source_paths=["raw/a.md"])
    assert decision.level == "raw"
    assert decision.reasons == ("exact_evidence_requested",)
    assert decision.allowed_source_paths == ("raw/a.md",)


def test_confident_cited_concept_does_not_fallback() -> None:
    assert decide_fallback(intent="concept", top_score=1.0, eligible_formal_count=1, citation_count=2).level == "none"
