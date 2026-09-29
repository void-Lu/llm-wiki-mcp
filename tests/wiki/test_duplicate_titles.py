from __future__ import annotations

from pathlib import Path

from retrieval.retrieval_index import RetrievalIndexStore
from wiki.duplicate_titles import bigrams, containment, duplicate_title_warnings, jaccard, near_duplicate_titles, new_title_surfaces
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


def _confidence(title: str, rows: list[dict[str, object]], page: str = "wiki/new/x.md") -> list[tuple[str, str, str]]:
    return [(item["path"], item["reason"], item["confidence"]) for item in near_duplicate_titles(page, title, rows)]


def test_confidence_is_related_for_containment_and_project_pages_near_general_concepts() -> None:
    rows = [
        {"path": "wiki/concepts/reliability/rate-limiting.md", "title": "Rate Limiting", "aliases": [], "type": "concept", "lifecycle": "active"},
        {"path": "wiki/concepts/reliability/circuit-breaker.md", "title": "Circuit Breaker", "aliases": [], "type": "knowledge", "lifecycle": "active"},
        {"path": "wiki/entities/rate-engine.md", "title": "Rate Engine", "aliases": [], "type": "entity", "lifecycle": "active"},
    ]
    concept = "wiki/concepts/reliability/rate-limiting.md"
    breaker = "wiki/concepts/reliability/circuit-breaker.md"
    spec = "wiki/projects/harbor/specs/x.md"
    # Bigram-similar (0.688) but only adds a whole word: "related" anywhere.
    assert _confidence("Rate Limiting Policy", rows) == [(concept, "similar_title", "related")]
    assert _confidence("Rate Limiting Policy", rows, spec) == [(concept, "similar_title", "related")]
    # Containment below the Jaccard threshold is "related" too.
    assert _confidence("Rate Engine Service", rows) == [("wiki/entities/rate-engine.md", "title_contains", "related")]
    # Similar titles are "high" for general pages, "related" for a project
    # page near a concept/knowledge page, "high" again near an entity.
    assert _confidence("Circuit Breakers", rows) == [(breaker, "similar_title", "high")]
    assert _confidence("Circuit Breakers", rows, spec) == [(breaker, "similar_title", "related")]
    assert _confidence("Rate Engines", rows, spec) == [("wiki/entities/rate-engine.md", "similar_title", "high")]
    # The same title stays "high" even for a project page.
    assert _confidence("rate limiting", rows, spec) == [(concept, "same_title_or_alias", "high")]


def test_high_confidence_warnings_sort_before_related_ones() -> None:
    # "Rate Limiter Service" scores 0.6 but only adds a word; the plural is a spelling variant.
    rows = [
        {"path": "wiki/entities/rate-limiter-service.md", "title": "Rate Limiter Service", "aliases": [], "type": "entity", "lifecycle": "active"},
        {"path": "wiki/entities/rate-limiters.md", "title": "Rate Limiters", "aliases": [], "type": "entity", "lifecycle": "active"},
    ]
    got = _confidence("Rate Limiter", rows)
    assert [item[2] for item in got] == ["high", "related"]


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
        {"path": "wiki/concepts/general/限流器.md", "title": "限流器", "reason": "title_contains", "score": 0.6, "matched": "限流器组件", "confidence": "related"}
    ]
    assert existing.read_text(encoding="utf-8").endswith("承运商调用限速。\n")


def test_new_title_surfaces_only_counts_title_and_aliases_the_page_gains() -> None:
    assert new_title_surfaces("Rate Engine", [], "Rate Engine", []) == []
    assert new_title_surfaces("Rate Engine", [], "rate-engine", []) == []
    assert new_title_surfaces("Rate Engine", ["Pricer"], "Pricing Core", ["Rate Engine", "Pricer", "Tariff Core"]) == ["Pricing Core", "Tariff Core"]
    assert new_title_surfaces("", [], "Retry Budget", ["retry budget"]) == ["Retry Budget"]


def _update_vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    create_wiki_root(root)
    for rel, title in (("wiki/concepts/reliability/rate-limiting.md", "Rate Limiting"), ("wiki/entities/general/carrier-gateway.md", "Carrier Gateway"), ("wiki/entities/general/dispatch-queue.md", "Dispatch Queue")):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntitle: {title}\ntype: entity\naliases:\n- {title} Old Alias\n---\n# {title}\n\nBody of {title}.\n", encoding="utf-8")
    store = RetrievalIndexStore(root)
    store.build(store.iter_vault_pages())
    return root


def _preview_and_apply(root: Path, page_path: str, body: str, incoming: dict | None) -> tuple[dict, dict]:
    from wiki.wiki_update import apply_update, preview_update

    preview = preview_update(root, page_path, body, incoming)
    applied = apply_update(root, page_path, body, incoming_frontmatter=incoming, plan_id=preview["plan_id"], expected_hash=preview["current_hash"])
    assert preview["ok"] is True and applied["ok"] is True, (preview, applied)
    return preview, applied


def test_update_that_retitles_a_page_warns_in_preview_and_apply(tmp_path: Path) -> None:
    root = _update_vault(tmp_path)
    preview, applied = _preview_and_apply(root, "wiki/entities/general/dispatch-queue.md", "Queue.\n", {"title": "Rate Limiting"})

    expected = [{"path": "wiki/concepts/reliability/rate-limiting.md", "title": "Rate Limiting", "reason": "same_title_or_alias", "score": 1.0, "matched": "Rate Limiting", "confidence": "high"}]
    assert preview["duplicate_warnings"] == expected
    assert applied["duplicate_warnings"] == expected


def test_update_that_adds_a_colliding_alias_warns(tmp_path: Path) -> None:
    root = _update_vault(tmp_path)
    _, applied = _preview_and_apply(root, "wiki/entities/general/dispatch-queue.md", "Queue.\n", {"aliases": ["Dispatch Queue Old Alias", "Carrier Gateways"]})

    assert [(item["path"], item["matched"]) for item in applied["duplicate_warnings"]] == [("wiki/entities/general/carrier-gateway.md", "Carrier Gateways")]


def test_body_only_or_unchanged_title_updates_never_warn_and_self_is_excluded(tmp_path: Path) -> None:
    root = _update_vault(tmp_path)
    page_path = "wiki/entities/general/dispatch-queue.md"
    # Body mentions another title; the page keeps its own title in frontmatter.
    preview, applied = _preview_and_apply(root, page_path, "Rate Limiting applies here.\n", None)
    assert "duplicate_warnings" not in preview and "duplicate_warnings" not in applied
    # Re-sending the current title, or keeping the old title as an alias.
    preview, applied = _preview_and_apply(root, page_path, "Queue.\n", {"title": "Dispatch Queue", "aliases": ["Dispatch Queue"]})
    assert "duplicate_warnings" not in preview and "duplicate_warnings" not in applied


def test_shared_spec_upsert_preview_warns_on_new_or_changed_title_only(tmp_path: Path) -> None:
    from wiki.spec_reuse import SharedSpecService

    root = _update_vault(tmp_path)
    origin = root / "wiki/projects/demo/specs/limits.md"
    origin.parent.mkdir(parents=True, exist_ok=True)
    origin.write_text("limits\n", encoding="utf-8")
    derived = [{"project": "demo", "path": "wiki/projects/demo/specs/limits.md", "rule": "limits"}]
    service = SharedSpecService(root)
    page_path = "wiki/entities/shared-specs/limits.md"

    created = service.plan("upsert", page_path, title="Rate Limiting", body="Limits.\n", derived_from=derived)
    assert [(item["path"], item["reason"]) for item in created["duplicate_warnings"]] == [("wiki/concepts/reliability/rate-limiting.md", "same_title_or_alias")]
    assert service.apply(str(created["plan_id"]))["ok"] is True

    unchanged = service.plan("upsert", page_path, title="Rate Limiting", body="Limits, revised.\n", derived_from=derived)
    assert "duplicate_warnings" not in unchanged
    retitled = service.plan("upsert", page_path, title="Carrier Gateway", body="Limits.\n", derived_from=derived)
    assert [item["path"] for item in retitled["duplicate_warnings"]] == ["wiki/entities/general/carrier-gateway.md"]
    fresh = service.plan("upsert", "wiki/entities/shared-specs/unique.md", title="Label Printer Drivers", body="Drivers.\n", derived_from=derived)
    assert "duplicate_warnings" not in fresh
