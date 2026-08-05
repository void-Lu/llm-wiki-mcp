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
