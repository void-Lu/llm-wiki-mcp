import re
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


def test_context_pack_keeps_trace_metadata_out_of_passages() -> None:
    packed = pack_context([
        ContextPassage(
            "history-1", "raw/sources/chat/session.md", "Session", "historical decision",
            1.0, "history_evidence",
            {"session_id": "session", "occurred_at": "2026-07-31T08:30:00+00:00", "project": "billing", "content_hash": "a" * 64},
        ),
    ], hard_limit=10, intent="history")

    passage = cast(list[dict[str, object]], packed["passages"])[0]
    assert set(packed) == {"passages", "budget"}
    assert "citation_metadata" not in passage
    assert "metadata" not in passage
    assert "session_id" not in passage


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


def test_context_pack_aggregates_non_adjacent_paths_in_first_seen_order() -> None:
    packed = pack_context([
        ContextPassage("a-1", "wiki/a.md", "A", "alpha beta", 1.0, "formal_knowledge"),
        ContextPassage("b-1", "wiki/b.md", "B", "gamma delta", 0.9, "formal_knowledge"),
        ContextPassage("a-2", "wiki/a.md", "A", "epsilon zeta", 0.8, "formal_knowledge"),
    ], hard_limit=10, intent="concept")

    passages = cast(list[dict[str, object]], packed["passages"])
    assert [item["path"] for item in passages] == ["wiki/a.md", "wiki/b.md"]
    assert all("citation" not in item for item in passages)
    assert passages[0]["heading"] == "A"
    assert passages[0]["content"] == "alpha beta\n\nepsilon zeta"
    assert passages[0]["tokens"] == 4


def test_context_pack_keeps_the_first_heading_for_a_path() -> None:
    packed = pack_context([
        ContextPassage("a-1", "wiki/a.md", "First heading", "alpha beta", 1.0, "formal_knowledge"),
        ContextPassage("a-2", "wiki/a.md", "Later heading", "gamma delta", 0.9, "formal_knowledge"),
    ], hard_limit=10, intent="concept")

    passages = cast(list[dict[str, object]], packed["passages"])
    assert len(passages) == 1
    assert passages[0]["heading"] == "First heading"


def test_context_pack_omitted_uses_original_candidates_minus_retained_items() -> None:
    packed = pack_context([
        ContextPassage("a-1", "wiki/a.md", "A", "alpha beta", 1.0, "formal_knowledge"),
        ContextPassage("a-2", "wiki/a.md", "A", "beta gamma", 0.9, "formal_knowledge"),
    ], hard_limit=10, intent="concept")

    budget = cast(dict[str, int], packed["budget"])
    assert budget["used"] == 3
    assert budget["omitted"] == 1


def _chunks(text: str, *, max_tokens: int = 60, overlap: int = 16):
    from retrieval.passage_chunker import chunk_markdown

    return chunk_markdown("wiki/a.md", text, target_tokens=max_tokens - 10, max_tokens=max_tokens, overlap_tokens=overlap)


def _long_section() -> str:
    paragraphs = [" ".join(f"p{index}w{word}, value." for word in range(12)) for index in range(8)]
    return "# A\n\n## Setup\n\n" + "\n\n".join(paragraphs)


def test_adjacent_chunks_are_stitched_in_reading_order_without_their_overlap() -> None:
    from retrieval.context_packer import ContextPassage, pack_context

    chunks = _chunks(_long_section())
    assert len(chunks) >= 3
    # The best passage (ordinal 1) is packed first, then the page from the top.
    order = [chunks[1], chunks[0], *chunks[2:]]
    passages = [ContextPassage(c.passage_id, c.page_path, "Setup", c.text, 1.0, "formal_knowledge", ordinal=c.ordinal) for c in order]

    packed = pack_context(passages, hard_limit=10_000, intent="lookup")

    content = packed["passages"][0]["content"]
    for index in range(8):
        for word in range(12):
            # The chunker may re-tokenize an overlap ("p0w8 ,"), so count words.
            assert len(re.findall(rf"\bp{index}w{word}\b", content)) == 1, (index, word)
    assert content.index("p0w0") < content.index("p7w11")


def test_strip_chunk_overlap_ignores_short_coincidental_matches() -> None:
    from retrieval.context_packer import strip_chunk_overlap

    assert strip_chunk_overlap("the next step starts here", previous="ends with the", following=None) == "the next step starts here"
    tail = "alpha beta gamma delta epsilon zeta eta theta"
    assert strip_chunk_overlap(f"{tail} iota kappa", previous=f"start {tail}", following=None) == "iota kappa"
    assert strip_chunk_overlap(f"lead {tail}", previous=None, following=f"{tail} after") == "lead"


def test_paragraphs_repeated_across_pages_are_packed_once() -> None:
    from retrieval.context_packer import ContextPassage, pack_context

    boiler = "需要实施细节时，应回到锁定的 raw 原文核对；本页不合并任何其他文档。"
    passages = [
        ContextPassage(f"p{index}", f"wiki/{index}.md", "H", f"Topic {index} specific text.\n\n{boiler}", 1.0, "formal_knowledge", ordinal=0)
        for index in range(3)
    ]

    packed = pack_context(passages, hard_limit=10_000, intent="lookup")

    contents = [item["content"] for item in packed["passages"]]
    assert [boiler in content for content in contents] == [True, False, False]
    assert all(f"Topic {index} specific text." in contents[index] for index in range(3))
    # A page whose whole first passage repeats an earlier page keeps its body.
    same = [ContextPassage(f"d{i}", f"wiki/d{i}.md", "H", boiler, 1.0, "formal_knowledge", ordinal=0) for i in range(2)]
    assert [item["content"] for item in pack_context(same, hard_limit=10_000, intent="lookup")["passages"]] == [boiler, boiler]
    # Short paragraphs ("Yes.") are never treated as repeats.
    short = [ContextPassage(f"s{i}", f"wiki/s{i}.md", "H", "Yes.", 1.0, "formal_knowledge", ordinal=0) for i in range(2)]
    assert [item["content"] for item in pack_context(short, hard_limit=100, intent="lookup")["passages"]] == ["Yes.", "Yes."]
