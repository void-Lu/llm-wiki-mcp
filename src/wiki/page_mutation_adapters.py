"""Kind-specific strategies for durable page mutations.

The coordinator owns the journal, lock and recovery state machine.  This
module owns the two write surfaces that differ in path policy, projection
profile, audit semantics and idempotency.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets
from typing import Any, Callable, Iterable, Mapping, Protocol

from common.privacy_policy import LocatorError, normalize_vault_relative
from wiki.knowledge_dependencies import KnowledgeDependencies
from wiki.page_operation_store import PageOperation
from wiki.page_policy import derive_page_policy
from wiki.projection_profile import projection_stages as profile_stages
from wiki.wiki_index import refresh_navigation
from wiki.wiki_io import read_markdown_page, split_frontmatter
from wiki.wiki_log import WikiLogStore, append_log_entry
from wiki.wiki_models import WikiLogEntry
from wiki.wiki_overview import refresh_overview
from wiki.wiki_paths import WikiPathError, resolve_within_root, validate_wiki_page_path


Projection = Callable[[], Mapping[str, object] | None]


@dataclass(frozen=True)
class ProjectionContext:
    """Dependencies needed by an adapter's local projection callbacks."""

    root: Path
    log_store: WikiLogStore


@dataclass(frozen=True)
class PlanIntent:
    """The domain inputs whose normalized shape is protected by an update plan."""

    body: str
    frontmatter: Mapping[str, Any]


def build_plan_intent(body: str, frontmatter: Mapping[str, Any]) -> PlanIntent:
    """Build one normalized plan intent for every durable write kind."""

    return PlanIntent(body=body, frontmatter=dict(frontmatter))


class WriteAdapterError(ValueError):
    """Stable adapter-registry failure."""

    def __init__(self, code: str, message: str = "write adapter could not be selected") -> None:
        super().__init__(message)
        self.code = code


class WriteAdapter(Protocol):
    """Strategy contract consumed by :class:`PageMutationCoordinator`."""

    name: str
    replay_existing_requests: bool

    def handles_operation_kind(self, operation_kind: str) -> bool: ...

    def handles_path(self, page_path: str) -> bool: ...

    def target(self, root: Path, page_path: str) -> Path: ...

    def projection_stages(self) -> tuple[str, ...]: ...

    def request_key(
        self,
        *,
        operation_kind: str,
        page_path: str,
        base_hash: str | None,
        intended_hash: str,
        text: str,
        plan_id: str | None,
        explicit_request_key: str | None,
    ) -> str: ...

    def existing_request_key(self, root: Path, page_path: str, content_hash: str, target: Path) -> str: ...

    def existing_operation_kind(self) -> str: ...

    def build_projections(self, context: ProjectionContext, operation: PageOperation, target: Path) -> dict[str, Projection]: ...


class FormalPageAdapter:
    """Strategy for ordinary Wiki pages, notes and controlled updates."""

    name = "formal"
    replay_existing_requests = False
    _operation_kinds = frozenset({"formal", "create", "note", "update"})

    def handles_operation_kind(self, operation_kind: str) -> bool:
        return operation_kind in self._operation_kinds

    def handles_path(self, page_path: str) -> bool:
        try:
            validate_wiki_page_path(page_path, allow_navigation_index=False)
        except WikiPathError:
            return False
        return True

    def target(self, root: Path, page_path: str) -> Path:
        relative = validate_wiki_page_path(page_path, allow_navigation_index=False)
        return resolve_within_root(root, relative)

    def projection_stages(self) -> tuple[str, ...]:
        return profile_stages("formal")

    def request_key(
        self,
        *,
        operation_kind: str,
        page_path: str,
        base_hash: str | None,
        intended_hash: str,
        text: str,
        plan_id: str | None,
        explicit_request_key: str | None,
    ) -> str:
        del text
        if explicit_request_key:
            return explicit_request_key
        if plan_id:
            return plan_id
        if operation_kind == "note":
            return f"note:{page_path}:{base_hash or 'missing'}:{intended_hash}"
        # A body-only update intentionally opts out of idempotence.  The
        # random token is a policy choice owned by the formal adapter rather
        # than a request-key format leaked into wiki_update.
        return f"body:{secrets.token_urlsafe(18)}"

    def existing_request_key(self, root: Path, page_path: str, content_hash: str, target: Path) -> str:
        del root, target
        return f"existing:{self.name}:{page_path}:{content_hash}"

    def existing_operation_kind(self) -> str:
        return "formal"

    def build_projections(self, context: ProjectionContext, operation: PageOperation, target: Path) -> dict[str, Projection]:
        page = read_markdown_page(target, context.root)
        source_hashes = _source_hashes(page.frontmatter)
        sources = _sources(page.frontmatter)
        policy = derive_page_policy(page.frontmatter, source_hashes)

        def dependencies() -> dict[str, object]:
            KnowledgeDependencies(context.root).update_page(
                operation.page_path,
                operation.intended_hash,
                source_hashes,
                policy=policy,
            )
            return {"ok": True, "state": "ready"}

        def retrieval() -> dict[str, object]:
            from retrieval.retrieval_index import RetrievalIndexStore

            return RetrievalIndexStore(context.root, scope="active").update_page_from_file(target)

        def navigation() -> dict[str, object]:
            return refresh_navigation(context.root)

        def overview() -> dict[str, object]:
            return refresh_overview(context.root)

        def audit_log() -> dict[str, object]:
            return append_log_entry(
                context.root,
                WikiLogEntry(
                    operation="update" if operation.operation_kind == "update" else "note",
                    title=page.title,
                    paths=[operation.page_path],
                    sources=sources,
                    project=str(page.frontmatter.get("project") or ""),
                    status="ok",
                    operation_id=operation.operation_id,
                ),
                log_store=context.log_store,
            )

        return {
            "dependencies": dependencies,
            "retrieval": retrieval,
            "navigation": navigation,
            "overview": overview,
            "audit_log": audit_log,
        }


class ChatSourceAdapter:
    """Strategy for immutable, raw chat revisions."""

    name = "chat"
    replay_existing_requests = True
    _operation_kinds = frozenset({"chat_source"})
    _path_prefix = ("raw", "sources", "chat")

    def handles_operation_kind(self, operation_kind: str) -> bool:
        return operation_kind in self._operation_kinds

    def handles_path(self, page_path: str) -> bool:
        try:
            normalized = normalize_vault_relative(page_path)
        except LocatorError:
            return False
        return tuple(normalized.split("/")[:3]) == self._path_prefix

    def target(self, root: Path, page_path: str) -> Path:
        try:
            normalized = normalize_vault_relative(page_path)
        except LocatorError as exc:
            raise WikiPathError(exc.code, "chat source path must stay inside the vault") from exc
        return resolve_within_root(root, Path(*normalized.split("/")))

    def projection_stages(self) -> tuple[str, ...]:
        return profile_stages("chat")

    def request_key(
        self,
        *,
        operation_kind: str,
        page_path: str,
        base_hash: str | None,
        intended_hash: str,
        text: str,
        plan_id: str | None,
        explicit_request_key: str | None,
    ) -> str:
        del operation_kind, base_hash, plan_id
        if explicit_request_key:
            return explicit_request_key
        frontmatter, _ = split_frontmatter(text)
        return self._request_key_from_frontmatter(frontmatter, page_path, intended_hash)

    def existing_request_key(self, root: Path, page_path: str, content_hash: str, target: Path) -> str:
        del root
        frontmatter, _ = split_frontmatter(target.read_text(encoding="utf-8"))
        return self._request_key_from_frontmatter(frontmatter, page_path, content_hash)

    def existing_operation_kind(self) -> str:
        return "chat_source"

    def build_projections(self, context: ProjectionContext, operation: PageOperation, target: Path) -> dict[str, Projection]:
        """Build raw-chat projections without treating the source as a page."""

        page = read_markdown_page(target, context.root)
        session_id = str(page.frontmatter.get("session_id") or target.parent.name)
        project = str(page.frontmatter.get("project") or "")
        source_hash = operation.intended_hash

        def dependencies() -> dict[str, object]:
            dependencies = KnowledgeDependencies(context.root)
            affected: set[str] = set()
            # A formal page may pin any immutable revision. A new revision
            # supersedes the whole session lineage, so compare the new bytes
            # against every revision path without changing stored source edges.
            for revision_path in sorted(target.parent.glob("revision-*.md")):
                relative = revision_path.relative_to(context.root).as_posix()
                affected.update(dependencies.source_changed(relative, source_hash))
            return {"ok": True, "state": "ready", "affected_count": len(affected)}

        def retrieval() -> dict[str, object]:
            # Chat revisions live in the active/history projection. The raw
            # store intentionally excludes chat, so update the active store
            # incrementally. Missing/incompatible stores require an explicit
            # administrator rebuild and must not trigger a hidden full build.
            from retrieval.retrieval_index import RetrievalIndexStore

            store = RetrievalIndexStore(context.root, scope="active")
            result = store.update_page_from_file(target)
            if result.get("code") == "not_eligible":
                return {"ok": True, "state": "not_applicable", "code": "not_eligible"}
            if not result.get("ok"):
                return result
            return {
                "ok": True,
                "state": str(result.get("state") or "ready"),
                "code": str(result.get("code") or "ready"),
                "operation": result.get("operation"),
                "retrieval_index": dict(result),
            }

        def not_applicable() -> dict[str, object]:
            return {"ok": True, "state": "not_applicable", "code": "not_applicable"}

        def audit_log() -> dict[str, object]:
            return append_log_entry(
                context.root,
                WikiLogEntry(
                    operation="chat_source",
                    title=session_id,
                    paths=[operation.page_path],
                    sources=[],
                    project=project,
                    status="ok",
                    operation_id=operation.operation_id,
                ),
                log_store=context.log_store,
            )

        return {
            "dependencies": dependencies,
            "retrieval": retrieval,
            "navigation": not_applicable,
            "overview": not_applicable,
            "audit_log": audit_log,
        }

    @staticmethod
    def _request_key_from_frontmatter(frontmatter: Mapping[str, Any], page_path: str, content_hash: str) -> str:
        session_id = str(frontmatter.get("session_id") or "")
        redacted_hash = str(frontmatter.get("redacted_hash") or "")
        if session_id and redacted_hash:
            return f"chat:{session_id}:{redacted_hash}"
        return f"chat:{page_path}:{content_hash}"


class WriteAdapterRegistry:
    """Construction-time registry for the coordinator's write strategies."""

    def __init__(self, adapters: Iterable[WriteAdapter]):
        self.adapters = tuple(adapters)
        if not self.adapters:
            raise WriteAdapterError("adapter_registry_empty")

    def for_operation_kind(self, operation_kind: str) -> WriteAdapter:
        for adapter in self.adapters:
            if adapter.handles_operation_kind(operation_kind):
                return adapter
        raise WriteAdapterError("operation_kind_unknown")

    def for_path(self, page_path: str) -> WriteAdapter:
        for adapter in self.adapters:
            if adapter.handles_path(page_path):
                return adapter
        raise WriteAdapterError("path_not_allowed")


def default_write_adapters() -> tuple[WriteAdapter, ...]:
    """Return the two production write strategies in deterministic order."""

    return (FormalPageAdapter(), ChatSourceAdapter())


def _source_hashes(frontmatter: Mapping[str, Any]) -> dict[str, str]:
    value = frontmatter.get("source_hashes")
    if not isinstance(value, Mapping):
        return {}
    return {str(key): str(item) for key, item in value.items() if str(key) and str(item)}


def _sources(frontmatter: Mapping[str, Any]) -> list[str]:
    value = frontmatter.get("sources", [])
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)] if value else []


__all__ = [
    "ChatSourceAdapter",
    "FormalPageAdapter",
    "Projection",
    "ProjectionContext",
    "WriteAdapter",
    "WriteAdapterError",
    "WriteAdapterRegistry",
    "default_write_adapters",
]
