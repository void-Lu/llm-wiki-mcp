from pathlib import Path

from netsuite_llm_wiki_mcp.retrieval_index import RetrievalIndexStore


def _write(root: Path, path: str, text: str) -> Path:
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def test_active_store_build_search_update_delete_and_reconcile(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    _write(root, "wiki/concepts/invoice.md", "---\ntitle: Invoice API\ntags: [finance, api]\n---\n\n# Invoice\n\n发票审批 custbody_invoice_id")
    _write(root, "wiki/index.md", "# Navigation\n\ninvoice")
    _write(root, "raw/sources/file/finance/private.txt", "invoice raw source must not be active")
    _write(root, "raw/sources/chat/2026/07/31/session/a.md", "token=sk-abcdefghijklmno chat invoice")
    store = RetrievalIndexStore(root)

    built = store.build(store.iter_vault_pages())
    hits = store.search_fts("发票审批 custbody_invoice_id", tags=["finance"])

    assert built["ok"] is True
    assert hits[0].page_path == "wiki/concepts/invoice.md"
    assert all("sk-abcdefghijklmno" not in hit.text for hit in store.search_fts("invoice"))
    assert not store.search_fts("raw source")
    status_info = store.status()
    assert isinstance(status_info["passage_count"], int)
    assert status_info["passage_count"] >= 2

    # Querying an existing store must not walk/read the source corpus.
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("source scan")))
    assert store.search_fts("invoice")
    monkeypatch.undo()

    page = root / "wiki/concepts/invoice.md"
    page.write_text(page.read_text(encoding="utf-8") + "\n\n## Limit\n\nUsage units", encoding="utf-8")
    reconciled = store.reconcile()
    assert reconciled["ok"] is True
    assert store.search_fts("usage units")

    deleted = store.delete_page("wiki/concepts/invoice.md")
    assert deleted["ok"] is True
    assert not store.search_fts("custbody_invoice_id")


def test_chat_projection_uses_a_full_session_locator_and_never_uses_year_as_project(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(root, "raw/sources/chat/2026/07/31/session/a.md", "# Review\n\nprovisional approval")
    store = RetrievalIndexStore(root)
    store.build(store.iter_vault_pages())

    page = store.page_candidates()[0]

    assert page["session_id"] == "2026/07/31/session"
    assert page["project"] == "unknown"
    assert page["occurred_at"]


def test_index_projection_redacts_frontmatter_as_well_as_body(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    secret = "token=abc1234567890"
    _write(
        root,
        "wiki/concepts/security.md",
        f"---\ntitle: Security\nsources: [{secret}]\n---\n\n# Security\n\nbody {secret}",
    )
    store = RetrievalIndexStore(root)
    store.build(store.iter_vault_pages())

    candidates = store.page_candidates()
    records = store.vector_records()
    assert candidates
    assert all(secret not in str(candidate) for candidate in candidates)
    assert all(secret not in str(record) for record in records)


def test_failed_staged_build_keeps_the_previous_searchable_store(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    _write(root, "wiki/concepts/a.md", "# A\n\nprevious content")
    store = RetrievalIndexStore(root)
    store.build(store.iter_vault_pages())
    monkeypatch.setattr(store, "_upsert", lambda *_args: (_ for _ in ()).throw(RuntimeError("injected failure")))

    try:
        store.build(store.iter_vault_pages())
    except RuntimeError:
        pass
    else:
        raise AssertionError("fault injection should fail")

    assert store.search_fts("previous content")


def test_active_and_archive_stores_are_physically_isolated(tmp_path: Path) -> None:
    root = tmp_path / "vault"
    _write(root, "wiki/concepts/live.md", "# Live\n\nactive invoice")
    _write(root, "archives/bundles/a/archived.md", "# Old\n\narchived invoice")
    active = RetrievalIndexStore(root)
    archive = RetrievalIndexStore(root, scope="archive")

    active.build(active.iter_vault_pages())
    archive.build(archive.iter_vault_pages())

    # Archive sources are intentionally only opened through the archive store.
    assert active.path != archive.path
    assert [hit.page_path for hit in active.search_fts("invoice")] == ["wiki/concepts/live.md"]
    assert [hit.page_path for hit in archive.search_fts("invoice")] == ["archives/bundles/a/archived.md"]


def test_raw_store_is_physically_isolated_and_queryable_without_source_reads(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "vault"
    _write(root, "wiki/concepts/live.md", "# Live\n\nactive invoice")
    _write(root, "raw/sources/file/finance/manual/invoice.txt", "raw-only custbody_approval_state")
    active = RetrievalIndexStore(root)
    raw = RetrievalIndexStore(root, scope="raw")

    active.build(active.iter_vault_pages())
    raw.build(raw.iter_vault_pages())

    assert active.path != raw.path
    assert not active.search_fts("custbody_approval_state")
    monkeypatch.setattr(Path, "read_text", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("source scan")))
    assert [hit.page_path for hit in raw.search_fts("custbody_approval_state")] == ["raw/sources/file/finance/manual/invoice.txt"]
