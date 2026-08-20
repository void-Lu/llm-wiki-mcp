from __future__ import annotations

import inspect
from hashlib import sha256
from pathlib import Path

from wiki.atomic_file import sha256_file
from wiki.page_mutation_adapters import build_plan_intent
from wiki.page_mutation import ChatSourceAdapter, FormalPageAdapter, PageMutationCoordinator, PlanIntent
from wiki.page_operation_store import PageOperationStore
from wiki.projection_profile import projection_stages
from wiki.wiki_paths import create_wiki_root


def _chat_page(tmp_path: Path) -> Path:
    page = tmp_path / "raw/sources/chat/2026/08/17/session/revision-000001.md"
    page.parent.mkdir(parents=True)
    page.write_text(
        "---\n"
        "type: chat_source\n"
        "session_id: session\n"
        "redacted_hash: abc123\n"
        "revision: 1\n"
        "summary: summary\n"
        "decisions: []\n"
        "open_questions: []\n"
        "tags: []\n"
        "---\n\n"
        "# user\n\nhello\n",
        encoding="utf-8",
    )
    return page


def test_coordinator_registers_formal_and_chat_adapters(tmp_path: Path) -> None:
    coordinator = PageMutationCoordinator(tmp_path)

    assert [type(adapter) for adapter in coordinator._adapters.adapters] == [FormalPageAdapter, ChatSourceAdapter]
    assert coordinator._adapters.adapters[0].projection_stages() is projection_stages("formal")
    assert coordinator._adapters.adapters[1].projection_stages() is projection_stages("chat")
    intent = build_plan_intent(body="body", frontmatter={"title": "Title"})
    assert intent.body == "body"
    assert intent.frontmatter == {"title": "Title"}


def test_build_plan_intent_has_one_module_owner_without_lazy_import() -> None:
    import wiki.page_mutation_adapters as adapters

    source = inspect.getsource(adapters)
    assert source.count("def build_plan_intent") == 1
    assert "from wiki.page_mutation import" not in source


def test_formal_adapter_owns_note_and_body_request_key_policies() -> None:
    adapter = FormalPageAdapter()
    common = {
        "page_path": "wiki/concepts/general/page.md",
        "base_hash": None,
        "intended_hash": "hash",
        "text": "body",
        "plan_id": None,
        "explicit_request_key": None,
    }

    first = adapter.request_key(operation_kind="update", **common)
    second = adapter.request_key(operation_kind="update", **common)
    note = adapter.request_key(operation_kind="note", **common)
    planned = adapter.request_key(operation_kind="update", plan_id="plan-1", **{key: value for key, value in common.items() if key != "plan_id"})

    assert first.startswith("body:")
    assert second.startswith("body:")
    assert first != second
    assert note == "note:wiki/concepts/general/page.md:missing:hash"
    assert planned == "plan-1"


def test_project_existing_path_hash_reuses_one_chat_operation(tmp_path: Path) -> None:
    page = _chat_page(tmp_path)
    original = page.read_bytes()
    coordinator = PageMutationCoordinator(tmp_path)
    relative = page.relative_to(tmp_path).as_posix()
    content_hash = sha256_file(page)

    first = coordinator.project_existing(relative, content_hash)
    second = coordinator.project_existing(relative, content_hash)

    assert first.ok is True
    assert first.state == "completed"
    assert second.ok is True
    assert second.already_applied is True
    assert second.operation_id == first.operation_id
    assert page.read_bytes() == original


def test_chat_existing_projection_does_not_consume_formal_plan(tmp_path: Path) -> None:
    page = _chat_page(tmp_path)
    coordinator = PageMutationCoordinator(tmp_path)
    plan = coordinator.issue_plan(
        page_path="wiki/concepts/general/page.md",
        base_hash=sha256(b"old").hexdigest(),
        intent=PlanIntent(body="new", frontmatter={}),
    )

    result = coordinator.project_existing(page.relative_to(tmp_path).as_posix(), sha256_file(page))

    assert result.ok is True
    assert coordinator._store.get_plan(plan.plan_id).state == "issued"


def test_coordinator_has_no_concrete_chat_kind_branch() -> None:
    assert "chat_source" not in inspect.getsource(PageMutationCoordinator)


def test_formal_projection_callbacks_forward_operation_page_path_on_replay(
    tmp_path: Path, monkeypatch
) -> None:
    create_wiki_root(tmp_path)
    page_path = "wiki/concepts/general/page.md"
    page = tmp_path / page_path
    page.parent.mkdir(parents=True, exist_ok=True)
    page.write_text("---\ntype: concept\ntitle: Page\ngenerated: false\n---\n\n# Page\n", encoding="utf-8")
    store = PageOperationStore(tmp_path)
    coordinator = PageMutationCoordinator(tmp_path, store=store)
    navigation_paths: list[str] = []
    overview_calls: list[tuple[str, str | None]] = []

    def fake_navigation(root: Path, *, changed_path: str) -> dict[str, object]:
        del root
        navigation_paths.append(changed_path)
        return {"ok": True}

    def fake_overview(
        root: Path,
        *,
        changed_path: str,
        changed_page_state: str | None = None,
    ) -> dict[str, object]:
        del root
        overview_calls.append((changed_path, changed_page_state))
        return {"ok": True}

    monkeypatch.setattr("wiki.page_mutation_adapters.refresh_navigation", fake_navigation)
    monkeypatch.setattr("wiki.page_mutation_adapters.refresh_overview", fake_overview)

    page_hash = sha256_file(page)
    update = coordinator.prepare(
        request_key="update-operation",
        operation_kind="update",
        page_path=page_path,
        base_hash=page_hash,
        intended_hash=page_hash,
    )
    update_projections = coordinator.projections_for(update)
    update_projections["navigation"]()
    update_projections["overview"]()

    create = coordinator.prepare(
        request_key="create-operation",
        operation_kind="create",
        page_path=page_path,
        base_hash=None,
        intended_hash=page_hash,
    )
    create_projections = coordinator.projections_for(create)
    create_projections["navigation"]()
    create_projections["overview"]()

    assert navigation_paths == [page_path, page_path]
    assert overview_calls == [(page_path, None), (page_path, "created")]
