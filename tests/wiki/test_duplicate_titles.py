from __future__ import annotations

from pathlib import Path

from retrieval.retrieval_index import RetrievalIndexStore
from wiki.duplicate_titles import bigrams, containment, duplicate_title_warnings, jaccard, near_duplicate_titles
from wiki.note_writer import save_obsidian_note
from wiki.wiki_paths import create_wiki_root


def _rows(*items: tuple[str, str, list[str]]) -> list[dict[str, object]]:
    return [{"path": path, "title": title, "aliases": aliases, "type": "entity", "lifecycle": "active"} for path, title, aliases in items]


ROWS = _rows(
    ("wiki/entities/rate-engine.md", "Rate Engine", []),
    ("wiki/entities/rate-limiting.md", "Rate Limiting", []),
    ("wiki/concepts/reliability/idempotency-key.md", "Idempotency Key", []),
    ("wiki/projects/ledger/specs/invoice-dates.md", "Invoice Dates Spec", []),
    ("wiki/projects/ledger/specs/invoice-totals.md", "Invoice Totals Spec", []),
    ("wiki/entities/限流器.md", "限流器", ["Throttler"]),
    ("wiki/concepts/billing/发票.md", "发票", []),
    ("wiki/concepts/billing/发票明细.md", "发票明细", []),
    ("wiki/projects/harbor/index.md", "Harbor", []),
)


def _paths(title: str, page: str = "wiki/new/x.md") -> list[tuple[str, str]]:
    return [(item["path"], item["reason"]) for item in near_duplicate_titles(page, title, ROWS)]


def test_bigram_jaccard_and_containment_use_the_alias_normalisation() -> None:
    assert bigrams("Rate-Engine") == bigrams("rate engine")
    assert jaccard(bigrams("Rate Engines"), bigrams("Rate Engine")) > 0.8
    assert containment("限流器组件", "限流器") == 0.6
    assert containment("发票明细", "发票") == 0.0  # the shorter side is below three characters


def test_exact_title_or_alias_match_comes_from_concept_registry_resolution() -> None:
    assert _paths("rate-engine") == [("wiki/entities/rate-engine.md", "same_title_or_alias")]
    assert _paths("throttler") == [("wiki/entities/限流器.md", "same_title_or_alias")]


def test_plural_typo_suffix_and_chinese_variants_are_near_duplicates() -> None:
    assert _paths("Rate Engines") == [("wiki/entities/rate-engine.md", "similar_title")]
    assert _paths("Idempotancy Key") == [("wiki/concepts/reliability/idempotency-key.md", "similar_title")]
    assert _paths("Rate Engine Service") == [("wiki/entities/rate-engine.md", "title_contains")]
    assert _paths("限流器组件") == [("wiki/entities/限流器.md", "title_contains")]


def test_distinct_titles_sharing_words_do_not_warn() -> None:
    # Every existing title checked against the others (leave-one-out) is quiet.
    for row in ROWS:
        assert near_duplicate_titles(str(row["path"]), str(row["title"]), ROWS) == []
    assert _paths("Carrier Onboarding") == []
    assert _paths("发票作废") == []


def test_structural_pages_and_the_page_itself_are_excluded() -> None:
    assert _paths("Harbor") == []
    assert _paths("Rate Engine", page="wiki/entities/rate-engine.md") == []


def test_warnings_are_empty_without_a_built_projection(tmp_path: Path) -> None:
    assert duplicate_title_warnings(tmp_path, "wiki/new/x.md", "Rate Engine") == []


def test_write_note_returns_duplicate_warnings_and_still_creates_the_page(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    (root / "wiki/concepts/general").mkdir(parents=True, exist_ok=True)
    existing = root / "wiki/concepts/general/限流器.md"
    existing.write_text("---\ntitle: 限流器\ntype: concept\n---\n# 限流器\n\n承运商调用限速。\n", encoding="utf-8")
    store = RetrievalIndexStore(root)
    store.build(store.iter_vault_pages())

    result = save_obsidian_note("knowledge", "限流器组件", "新的说明。\n", domain="general", vault_root=root)

    assert result["ok"] is True
    assert (root / str(result["path"])).is_file()
    assert result["duplicate_warnings"] == [
        {"path": "wiki/concepts/general/限流器.md", "title": "限流器", "reason": "title_contains", "score": 0.6, "matched": "限流器组件"}
    ]
    assert existing.read_text(encoding="utf-8").endswith("承运商调用限速。\n")
