from typing import cast

from retrieval.context_packer import ContextPassage, pack_context


def test_context_pack_contains_each_body_once_and_honours_limit() -> None:
    packed = pack_context([
        ContextPassage("a", "wiki/concepts/a.md", "A", "alpha beta gamma", 1.0, "formal_knowledge"),
        ContextPassage("b", "wiki/concepts/a.md", "A", "gamma delta", 0.9, "formal_knowledge"),
    ], hard_limit=4, intent="concept")
    passages = cast(list[dict[str, object]], packed["passages"])
    assert len(passages) == 1
    assert cast(str, passages[0]["content"]).count("gamma") == 1
    budget = cast(dict[str, int], packed["budget"])
    assert budget["used"] <= budget["total"]


def test_context_pack_keeps_trace_metadata_on_citations_only() -> None:
    packed = pack_context([
        ContextPassage(
            "history-1", "raw/sources/chat/session.md", "Session", "historical decision",
            1.0, "history_evidence",
            {"session_id": "session", "occurred_at": "2026-07-31T08:30:00+00:00", "project": "billing", "content_hash": "a" * 64},
        ),
    ], hard_limit=10, intent="history")

    passage = cast(list[dict[str, object]], packed["passages"])[0]
    citation = cast(list[dict[str, object]], packed["citations"])[0]
    assert "citation_metadata" not in passage
    assert citation["metadata"] == {
        "session_id": "session",
        "occurred_at": "2026-07-31T08:30:00+00:00",
        "project": "billing",
        "content_hash": "a" * 64,
    }


def test_context_pack_aggregates_each_path_and_budget_matches_result_tokens() -> None:
    packed = pack_context([
        ContextPassage("a-1", "wiki/a.md", "A", "alpha beta", 1.0, "formal_knowledge"),
        ContextPassage("b-1", "wiki/b.md", "B", "gamma delta", 0.9, "formal_knowledge"),
        ContextPassage("a-2", "wiki/a.md", "Other", "delta epsilon", 0.8, "formal_knowledge"),
    ], hard_limit=10, intent="concept")

    passages = cast(list[dict[str, object]], packed["passages"])
    budget = cast(dict[str, int], packed["budget"])
    assert [item["path"] for item in passages] == ["wiki/a.md", "wiki/b.md"]
    assert budget["used"] == sum(int(item["tokens"]) for item in passages)
    assert cast(str, passages[0]["content"]) == "alpha beta\n\ndelta epsilon"


def test_context_pack_omitted_uses_original_candidates_minus_retained_items() -> None:
    packed = pack_context([
        ContextPassage("a-1", "wiki/a.md", "A", "alpha beta", 1.0, "formal_knowledge"),
        ContextPassage("a-2", "wiki/a.md", "A", "beta gamma", 0.9, "formal_knowledge"),
    ], hard_limit=10, intent="concept")

    budget = cast(dict[str, int], packed["budget"])
    assert budget["used"] == 3
    assert budget["omitted"] == 1
