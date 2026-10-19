"""Tests for Small-to-Big parent passage expansion."""

from __future__ import annotations

from pathlib import Path

from retrieval.context_packer import ContextPassage, pack_context
from retrieval.parent_expansion import (
    DEFAULT_MAX_PARENT_CHARS,
    ExpandedPassage,
    expand_hit_texts,
)
from retrieval.retrieval_index import PassageHit


def _hit(
    passage_id: str,
    *,
    page: str = "wiki/a.md",
    ordinal: int,
    text: str,
    heading: tuple[str, ...] = ("Setup",),
    score: float = 1.0,
) -> PassageHit:
    return PassageHit(
        passage_id,
        page,
        "A",
        heading,
        text,
        score,
        "active",
        "formal",
        "wiki",
        ordinal,
    )


def _loader(passages: list[PassageHit]):
    by_key = {(p.page_path, p.ordinal): p for p in passages}

    def load(page_path: str, ordinals: list[int]) -> list[PassageHit]:
        return [
            by_key[page_path, ordinal]
            for ordinal in sorted(set(ordinals))
            if (page_path, ordinal) in by_key
        ]

    return load


def test_expand_joins_ordinal_minus_and_plus_one() -> None:
    prev = _hit("p0", ordinal=0, text="Alpha beta gamma delta epsilon zeta eta theta.")
    hit = _hit("p1", ordinal=1, text="Iota kappa lambda mu nu xi omicron pi.")
    nxt = _hit("p2", ordinal=2, text="Rho sigma tau upsilon phi chi psi omega.")
    results = expand_hit_texts([hit], load_passages=_loader([prev, hit, nxt]), window=1)

    assert len(results) == 1
    expanded = results[0]
    assert expanded.passage_id == "p1"
    assert expanded.expanded is True
    assert expanded.neighbor_ordinals == (0, 2)
    assert "Alpha beta" in expanded.text
    assert "Iota kappa" in expanded.text
    assert "Rho sigma" in expanded.text
    assert expanded.text.index("Alpha") < expanded.text.index("Iota") < expanded.text.index("Rho")


def test_expand_skips_missing_edge_neighbors() -> None:
    hit = _hit("p0", ordinal=0, text="Only first passage on the page.")
    results = expand_hit_texts([hit], load_passages=_loader([hit]), window=1)

    assert results[0].passage_id == "p0"
    assert results[0].expanded is False
    assert results[0].neighbor_ordinals == ()
    assert results[0].text == hit.text
    assert results[0].expansion_chars == 0


def test_same_heading_only_drops_cross_heading_neighbors() -> None:
    prev = _hit(
        "p0",
        ordinal=0,
        text="Previous section body with enough words here.",
        heading=("Intro",),
    )
    hit = _hit(
        "p1",
        ordinal=1,
        text="Current section body with enough words here.",
        heading=("Setup",),
    )
    nxt = _hit(
        "p2",
        ordinal=2,
        text="Next same heading body with enough words.",
        heading=("Setup",),
    )
    results = expand_hit_texts(
        [hit],
        load_passages=_loader([prev, hit, nxt]),
        window=1,
        same_heading_only=True,
    )

    assert results[0].neighbor_ordinals == (2,)
    assert "Previous section" not in results[0].text
    assert "Next same heading" in results[0].text


def test_budget_keeps_hit_and_prefers_minus_one_over_plus_one() -> None:
    prev = _hit("p0", ordinal=0, text="P" * 80)
    hit = _hit("p1", ordinal=1, text="H" * 80)
    nxt = _hit("p2", ordinal=2, text="N" * 80)
    results = expand_hit_texts(
        [hit],
        load_passages=_loader([prev, hit, nxt]),
        window=1,
        max_parent_chars=170,
    )

    expanded = results[0]
    assert "H" * 80 in expanded.text
    assert "P" * 80 in expanded.text
    assert "N" * 80 not in expanded.text
    assert expanded.neighbor_ordinals == (0,)


def test_passage_id_stays_on_hit_after_expansion() -> None:
    prev = _hit("neighbor-prev", ordinal=0, text="Neighbor previous content for the page.")
    hit = _hit("hit-id", ordinal=1, text="Hit passage content for the page body.")
    results = expand_hit_texts([hit], load_passages=_loader([prev, hit]), window=1)

    assert results[0].passage_id == "hit-id"
    assert isinstance(results[0], ExpandedPassage)


def test_overlap_between_hit_and_neighbor_is_stripped() -> None:
    shared = " ".join(f"w{i}" for i in range(16))
    prev = _hit("p0", ordinal=0, text=f"start {shared}")
    hit = _hit("p1", ordinal=1, text=f"{shared} exclusive-hit-tail")
    results = expand_hit_texts([hit], load_passages=_loader([prev, hit]), window=1)

    text = results[0].text
    assert text.count("w0") == 1
    assert "exclusive-hit-tail" in text
    assert "start" in text


def test_adjacent_hits_expand_without_changing_ids() -> None:
    p0 = _hit("p0", ordinal=0, text="Zero block text for adjacent expansion case.")
    p1 = _hit("p1", ordinal=1, text="One block text for adjacent expansion case.")
    p2 = _hit("p2", ordinal=2, text="Two block text for adjacent expansion case.")
    results = expand_hit_texts([p0, p1], load_passages=_loader([p0, p1, p2]), window=1)

    assert [item.passage_id for item in results] == ["p0", "p1"]
    assert "One block" in results[0].text
    assert "Zero block" in results[1].text
    packed = pack_context(
        [
            ContextPassage(
                item.passage_id,
                item.page_path,
                "Setup",
                item.text,
                1.0,
                "formal_knowledge",
                ordinal=item.ordinal,
                expanded=item.expanded,
                neighbor_ordinals=item.neighbor_ordinals,
                expansion_chars=item.expansion_chars,
            )
            for item in results
        ],
        hard_limit=10_000,
        intent="lookup",
    )
    content = packed["passages"][0]["content"]
    assert content.count("Zero block") >= 1
    assert content.count("One block") >= 1


def test_unknown_ordinal_is_left_unchanged() -> None:
    hit = _hit("synth", ordinal=-1, text="Synthetic hit without ordinal.")
    results = expand_hit_texts([hit], load_passages=_loader([hit]), window=1)
    assert results[0].text == hit.text
    assert results[0].expanded is False


def test_default_max_parent_chars_is_documented() -> None:
    assert DEFAULT_MAX_PARENT_CHARS >= 500


def test_passages_at_ordinals_loads_neighbors(tmp_path: Path) -> None:
    from retrieval.retrieval_index import IndexedPage, RetrievalIndexStore

    root = tmp_path / "vault"
    root.mkdir()
    store = RetrievalIndexStore(root)
    paragraphs = [
        " ".join(f"s{section}w{word}, value." for word in range(40)) for section in range(30)
    ]
    body = "# A\n\n## Setup\n\n" + "\n\n".join(paragraphs)
    page = IndexedPage(
        path="wiki/concepts/a.md",
        title="A",
        body=body,
        frontmatter={"title": "A", "type": "concept"},
        corpus="knowledge",
        authority="high",
        lifecycle_status="active",
        source_kind="concept",
        original_content_hash="a" * 64,
        redacted_content_hash="b" * 64,
        mtime_ns=0,
        size_bytes=len(body),
    )
    store.build([page])
    page_path = "wiki/concepts/a.md"
    all_hits = store.passages_for_pages([page_path], limit_per_page=20)
    assert len(all_hits) >= 3
    mid = all_hits[1]
    loaded = store.passages_at_ordinals(
        page_path, [mid.ordinal - 1, mid.ordinal, mid.ordinal + 1]
    )
    assert [item.ordinal for item in loaded] == [
        mid.ordinal - 1,
        mid.ordinal,
        mid.ordinal + 1,
    ]
    assert store.passages_at_ordinals(page_path, [-1, 9999]) == []
