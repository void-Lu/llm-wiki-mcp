from __future__ import annotations

from pathlib import Path

from retrieval.retrieval_index import RetrievalIndexStore
from retrieval.vector_index import embedding_text, vector_index_records
from wiki.wiki_paths import create_wiki_root


def test_embedding_text_prefixes_title_and_heading_path() -> None:
    assert embedding_text("Carrier API", ["Limits", "Retries"], "Capped at three.") == "Carrier API\nLimits > Retries\nCapped at three."


def test_embedding_text_does_not_repeat_an_h1_equal_to_the_title() -> None:
    assert embedding_text("Carrier API", ["Carrier API", "Limits"], "body") == "Carrier API\nLimits\nbody"
    assert embedding_text("carrier api", ["Carrier  API"], "body") == "carrier api\nbody"


def test_embedding_text_without_title_or_headings_is_the_passage() -> None:
    assert embedding_text("", [], "body") == "body"
    assert embedding_text("  ", ["", " "], "body") == "body"


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_vector_records_embed_title_and_headings_and_hash_the_embedded_text(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    create_wiki_root(root)
    page = "---\ntitle: 限流器\ntype: concept\n---\n# 限流器\n\n## 重试\n\n每个承运商最多重试三次。\n"
    _write(root, "wiki/concepts/throttler.md", page)
    store = RetrievalIndexStore(root)
    store.build(store.iter_vault_pages())

    records = [record for record in vector_index_records(root) if record.page_path == "wiki/concepts/throttler.md"]
    assert [record.text for record in records] == ["限流器\n重试\n每个承运商最多重试三次。"]
    before = records[0].content_hash

    # A retitle leaves the passage body (and its passage hash) unchanged but
    # changes the embedded text, so the vector record must become stale.
    _write(root, "wiki/concepts/throttler.md", page.replace("title: 限流器", "title: 承运商限流器"))
    store.build(store.iter_vault_pages())
    retitled = [record for record in vector_index_records(root) if record.page_path == "wiki/concepts/throttler.md"]
    assert retitled[0].text.startswith("承运商限流器\n")
    assert retitled[0].content_hash != before
