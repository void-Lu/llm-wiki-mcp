from __future__ import annotations

from hashlib import sha256
from pathlib import Path

from retrieval.retrieval_index import RetrievalIndexStore
from wiki.link_suggestions import eligible_link_targets, suggest_unlinked_mentions, unlinked_mention_suggestions
from wiki.note_writer import save_obsidian_note
from wiki.wiki_paths import create_wiki_root
from wiki.wiki_update import apply_update, preview_update


def _targets(*rows: tuple[str, str, list[str]] | tuple[str, str, list[str], dict[str, str]]):
    records = []
    for row in rows:
        path, title, aliases = row[0], row[1], row[2]
        extra = row[3] if len(row) > 3 else {}
        records.append({"path": path, "title": title, "aliases": aliases, "type": "entity", "lifecycle": "active", **extra})
    return eligible_link_targets(records)


def _hints(page: str, body: str, targets) -> list[tuple[str, str]]:
    return [(item["target"], item["mention"]) for item in suggest_unlinked_mentions(page, body, targets)]


TARGETS = _targets(
    ("wiki/entities/rate-engine.md", "Rate Engine", []),
    ("wiki/entities/carrier-gateway.md", "Carrier Gateway", []),
    ("wiki/entities/gateway.md", "Gateway", []),
    ("wiki/entities/ledger.md", "Ledger", []),
    ("wiki/entities/api.md", "API", []),
    ("wiki/entities/shared-specs/retry-budget.md", "Retry Budget Rule", []),
    ("wiki/entities/限流器.md", "限流器", ["Throttler"]),
    ("wiki/concepts/billing/发票.md", "发票", []),
    ("wiki/concepts/billing/发票明细.md", "发票明细", []),
    ("wiki/entities/票.md", "票", []),
    ("wiki/entities/dispatch-queue.md", "调度队列", ["队列"]),
    ("wiki/entities/message-queue.md", "消息队列", ["队列"]),
)


def test_reports_first_unlinked_mention_per_target_with_line_and_link() -> None:
    body = "Intro.\n\nThe rate engine calls the Rate Engine again.\n"
    hints = suggest_unlinked_mentions("wiki/concepts/x.md", body, TARGETS)

    assert hints == [
        {"target": "wiki/entities/rate-engine.md", "title": "Rate Engine", "mention": "rate engine", "line": 3, "link": "[[rate-engine|rate engine]]"}
    ]


def test_skips_code_existing_links_markdown_links_and_already_linked_targets() -> None:
    body = (
        "```\nRate Engine in a fence\n```\n"
        "Inline `Carrier Gateway` code. [Rate Engine](https://example.com) link.\n"
        "Already linked: [[carrier-gateway]] and later Carrier Gateway prose.\n"
    )
    assert _hints("wiki/concepts/x.md", body, TARGETS) == []


def test_longer_terms_win_and_short_or_ambiguous_terms_are_skipped() -> None:
    body = "Carrier Gateway uses the API. 队列 积压。发票明细 与 票 无关。"
    # "Gateway" inside "Carrier Gateway" is claimed by the longer title, "API"
    # is too short, "队列" names two pages, "票" is a single ideograph and
    # "发票" inside "发票明细" is claimed by the longer title.
    assert _hints("wiki/concepts/x.md", body, TARGETS) == [
        ("wiki/entities/carrier-gateway.md", "Carrier Gateway"),
        ("wiki/concepts/billing/发票明细.md", "发票明细"),
    ]


def test_latin_word_boundaries_and_case_sensitive_single_words() -> None:
    body = "Gateways are not a match, ledger in prose is not, but the Ledger project is. Gateway alone is."
    assert _hints("wiki/concepts/x.md", body, TARGETS) == [
        ("wiki/entities/ledger.md", "Ledger"),
        ("wiki/entities/gateway.md", "Gateway"),
    ]


def test_cjk_matches_without_spaces_and_aliases_and_stem_phrases_count() -> None:
    body = "每次重试前先经过限流器；retry budget 用完即停。Throttler 也叫限流器。"
    # The earliest of the title/alias occurrences is reported.
    assert _hints("wiki/concepts/x.md", body, TARGETS) == [
        ("wiki/entities/限流器.md", "限流器"),
        ("wiki/entities/shared-specs/retry-budget.md", "retry budget"),
    ]


def test_page_itself_and_its_own_terms_are_never_suggested() -> None:
    body = "Rate Engine describes itself; Carrier Gateway is another page."
    assert _hints("wiki/entities/rate-engine.md", body, TARGETS) == [("wiki/entities/carrier-gateway.md", "Carrier Gateway")]


def test_structural_and_inactive_pages_are_not_targets() -> None:
    targets = _targets(
        ("wiki/projects/harbor/index.md", "Harbor Project", []),
        ("wiki/entities/old-router.md", "Old Router", [], {"lifecycle": "superseded"}),
        ("wiki/sources/capsules/case.md", "Source Capsule", []),
    )
    assert targets == []


def test_suggestions_are_empty_without_a_built_projection(tmp_path: Path) -> None:
    assert unlinked_mention_suggestions(tmp_path, "wiki/concepts/x.md", "Rate Engine") == []


def _vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    create_wiki_root(root)
    (root / "wiki/concepts/general").mkdir(parents=True, exist_ok=True)
    for rel, title in (("wiki/entities/general/rate-engine.md", "Rate Engine"), ("wiki/entities/general/carrier-gateway.md", "Carrier Gateway")):
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntitle: {title}\ntype: entity\n---\n# {title}\n\nBody of {title}.\n", encoding="utf-8")
    store = RetrievalIndexStore(root)
    store.build(store.iter_vault_pages())
    return root


def test_write_note_returns_link_suggestions_without_rewriting_the_page(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    content = "The Rate Engine prices every call that the Carrier Gateway sends.\n"

    result = save_obsidian_note("knowledge", "Pricing Flow", content, domain="general", vault_root=root)

    assert result["ok"] is True
    assert [item["target"] for item in result["link_suggestions"]] == [
        "wiki/entities/general/rate-engine.md",
        "wiki/entities/general/carrier-gateway.md",
    ]
    written = (root / str(result["path"])).read_text(encoding="utf-8")
    assert "[[" not in written


def test_write_note_omits_link_suggestions_when_there_are_none(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    result = save_obsidian_note("knowledge", "Unrelated", "Nothing to link here.\n", domain="general", vault_root=root)
    assert result["ok"] is True
    assert "link_suggestions" not in result


def test_update_apply_returns_link_suggestions(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    page = root / "wiki/entities/general/rate-engine.md"

    result = apply_update(root, "wiki/entities/general/rate-engine.md", "Receives calls from the Carrier Gateway.\n", expected_hash=sha256(page.read_bytes()).hexdigest())

    assert result["ok"] is True
    assert [(item["target"], item["link"]) for item in result["link_suggestions"]] == [
        ("wiki/entities/general/carrier-gateway.md", "[[carrier-gateway|Carrier Gateway]]")
    ]
    assert "[[" not in page.read_text(encoding="utf-8")


def test_update_preview_returns_the_same_link_suggestions_as_its_apply(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    page_path = "wiki/entities/general/rate-engine.md"
    body = "# Ignored Heading\n\nReceives calls from the Carrier Gateway.\n\nThe Rate Engine itself is not a hint.\n"
    incoming = {"aliases": ["Pricing Core"]}

    preview = preview_update(root, page_path, body, incoming)
    applied = apply_update(root, page_path, body, incoming_frontmatter=incoming, plan_id=preview["plan_id"], expected_hash=preview["current_hash"])

    assert preview["ok"] is True and applied["ok"] is True
    assert preview["link_suggestions"] == applied["link_suggestions"]
    assert [(item["target"], item["line"]) for item in preview["link_suggestions"]] == [("wiki/entities/general/carrier-gateway.md", 3)]


def test_update_preview_parity_holds_when_title_or_aliases_change(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    page_path = "wiki/entities/general/rate-engine.md"
    # The projection still holds the old title/aliases during preview.  The
    # new alias makes "Carrier Gateway" one of this page's own terms, so
    # neither phase may suggest it; "Rate Engine" stays the page's own stem.
    body = "Pricing Service calls the Carrier Gateway and the Rate Engine.\n"
    incoming = {"title": "Pricing Service", "aliases": ["Carrier Gateway"]}

    preview = preview_update(root, page_path, body, incoming)
    applied = apply_update(root, page_path, body, incoming_frontmatter=incoming, plan_id=preview["plan_id"], expected_hash=preview["current_hash"])

    assert applied["ok"] is True
    assert "link_suggestions" not in preview
    assert "link_suggestions" not in applied


def test_update_preview_omits_link_suggestions_when_there_are_none(tmp_path: Path) -> None:
    root = _vault(tmp_path)
    preview = preview_update(root, "wiki/entities/general/rate-engine.md", "Nothing to link here.\n")
    assert preview["ok"] is True
    assert "link_suggestions" not in preview
