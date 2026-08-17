"""Execution state owner and bounded stage migration for one Query V2 call."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal

from retrieval.lexical_analyzer import has_qualified_identifier
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_shared import QueryFilters
from retrieval.query_recovery import (
    FallbackPlan,
    FallbackState,
    RecoveryAssembly,
    assemble_recovery,
    compose_score,
    plan_fallback,
    resolve_fallback_lexical_mode,
    select_best_per_page,
    step_bonus,
)
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.retrieval_index import RetrievalIndexStore
from retrieval import query_recall_policy as recall_policy
from retrieval import discovery as discovery_owner
from retrieval import entity_batch as entity_batch_owner


RawAvailability = Literal["fresh", "stale", "missing"]


def _raw_index_warning(status: Mapping[str, object]) -> str:
    """Expose raw-store failures without conflating them with the Wiki index."""

    state = str(status.get("state") or "")
    if state == "stale":
        return "raw_index_stale"
    code = str(status.get("code") or "unavailable")
    if code.startswith("index_"):
        code = code.removeprefix("index_")
    return f"raw_index_{code}"


def _freeze_value(value: Any) -> Any:
    """Recursively project query state into immutable containers."""

    if isinstance(value, Mapping):
        return MappingProxyType(
            {key: _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_value(item) for item in value)
    return value


def _freeze_recovery(value: RecoveryAssembly) -> RecoveryAssembly:
    return replace(
        value,
        selected=_freeze_value(value.selected),
        hit_stats=_freeze_value(value.hit_stats),
        pool_by_page=_freeze_value(value.pool_by_page),
        context_items=_freeze_value(value.context_items),
        fallback=_freeze_value(value.fallback),
    )


@dataclass(frozen=True)
class QueryExecutionOutcome:
    """Immutable read model published once a query execution is complete."""

    selected: tuple[Mapping[str, Any], ...]
    context_items: tuple[Mapping[str, Any], ...]
    recovery: RecoveryAssembly
    status: Mapping[str, Any]
    raw_availability: RawAvailability
    raw_fts_hits: int
    relaxed_fts_hits: int
    raw_index_warning: str
    coverage_fallback: bool
    lexical_mode: str
    expansion_suggestions: tuple[str, ...]
    uncovered_latin_terms: tuple[str, ...]
    discovery: Mapping[str, Any]
    discovery_entities: tuple[Mapping[str, Any], ...]
    discovery_source_items: tuple[Mapping[str, Any], ...]
    batch_payload: Mapping[str, Any]
    discovery_requested: bool


@dataclass
class QueryExecutionContext:
    """Mutable state owner for one Query V2 invocation.

    The context owns the physical stores and immutable snapshots used by all
    fallback/discovery stages.  It deliberately keeps the raw store lazy so a
    successful active Wiki query never opens the second corpus.
    """

    root: Path
    store: RetrievalIndexStore
    cancellation: QueryCancellationContext
    status: dict[str, Any]
    raw_store: RetrievalIndexStore | None = field(default=None, init=False)
    raw_snapshot: QueryCorpusSnapshot | None = field(default=None, init=False)
    selected: list[dict[str, Any]] = field(default_factory=list, init=False)
    context_items: list[dict[str, Any]] = field(default_factory=list, init=False)
    recovery: RecoveryAssembly | None = field(default=None, init=False)
    raw_fts_hits: int = field(default=0, init=False)
    relaxed_fts_hits: int = field(default=0, init=False)
    raw_index_warning: str = field(default="", init=False)
    coverage_fallback: bool = field(default=False, init=False)
    lexical_mode: str = field(default="strict", init=False)
    expansion_suggestions: list[str] = field(default_factory=list, init=False)
    uncovered_latin_terms: list[str] = field(default_factory=list, init=False)
    discovery: dict[str, Any] = field(default_factory=dict, init=False)
    discovery_entities: list[dict[str, Any]] = field(default_factory=list, init=False)
    discovery_source_items: list[dict[str, Any]] = field(
        default_factory=list, init=False
    )
    batch_payload: dict[str, Any] = field(default_factory=dict, init=False)
    discovery_requested: bool = field(default=False, init=False)
    _raw_status: dict[str, object] | None = field(default=None, init=False, repr=False)
    _raw_availability: RawAvailability = field(
        default="missing", init=False, repr=False
    )
    _sealed: bool = field(default=False, init=False, repr=False)
    _outcome: QueryExecutionOutcome | None = field(default=None, init=False, repr=False)

    def __setattr__(self, name: str, value: object) -> None:
        if name not in {"_sealed", "_outcome"} and getattr(self, "_sealed", False):
            raise RuntimeError("query execution context is sealed")
        object.__setattr__(self, name, value)

    def _ensure_open(self) -> None:
        if self._sealed:
            raise RuntimeError("query execution context is sealed")

    def get_raw_store(self) -> RetrievalIndexStore:
        """Return the single lazily-created raw projection for this query."""

        self._ensure_open()
        if self.raw_store is None:
            self.raw_store = RetrievalIndexStore(self.root, scope="raw")
        return self.raw_store

    def raw_availability(self) -> RawAvailability:
        """Normalize raw index status to the query's three-state vocabulary."""

        self._ensure_open()
        status = self.get_raw_store().status()
        self._raw_status = dict(status)
        state = str(status.get("state") or "")
        if bool(status.get("ok")) and state == "fresh":
            self._raw_availability = "fresh"
        elif state == "stale":
            self._raw_availability = "stale"
        else:
            self._raw_availability = "missing"
        return self._raw_availability

    def capture_raw_snapshot(self) -> QueryCorpusSnapshot:
        """Capture the raw metadata view at most once, including an empty view."""

        self._ensure_open()
        if self.raw_snapshot is None:
            availability = self.raw_availability()
            if availability != "fresh":
                self.raw_snapshot = QueryCorpusSnapshot.empty("raw")
            else:
                self.raw_snapshot = QueryCorpusSnapshot.capture(
                    self.get_raw_store(),
                    cancellation=self.cancellation,
                )
        return self.raw_snapshot

    def _raw_warning(self) -> str:
        return _raw_index_warning(self._raw_status or {})

    def run_raw_branch(
        self,
        *,
        branch: Literal["coverage", "all_coverage", "raw_zero"],
        question: str,
        project: str | None,
        filters: QueryFilters,
        top_k: int,
        extra_terms: list[str],
        term_variants: dict[str, list[str]],
        uncovered_latin_terms: Sequence[str],
        effective_rrf_k: int,
        selected: list[dict[str, Any]],
        candidate_pool: Sequence[dict[str, Any]],
        plan: FallbackPlan,
        lexical_mode: str,
        coverage_fallback: bool,
    ) -> None:
        """Execute one raw branch and migrate its state onto this context."""

        self._ensure_open()
        self.cancellation.checkpoint("fallback")
        raw_store = self.get_raw_store()
        availability = self.raw_availability()
        warning = self._raw_warning()
        raw_snapshot = self.capture_raw_snapshot()
        if availability != "fresh":
            self.selected = selected
            self.raw_fts_hits = 0
            self.raw_index_warning = warning
            self.lexical_mode = lexical_mode
            self.coverage_fallback = coverage_fallback
            return

        _, raw_candidate_items, raw_fts_hits, raw_index_warning, raw_lexical_mode = (
            recall_policy.raw_recovery_candidates(
                raw_store,
                question,
                project=project,
                filters=filters,
                scope="raw",
                extra_terms=extra_terms,
                term_variants=term_variants,
                top_k=top_k,
                snapshot=raw_snapshot,
                raw_availability=availability,
                raw_index_warning=warning,
                cancellation=self.cancellation,
            )
        )
        if raw_index_warning:
            raw_index_warning = _raw_index_warning({"code": raw_index_warning})
        if branch in {"coverage", "all_coverage"}:
            covered_raw_items = recall_policy.merge_coverage_items(
                [],
                raw_candidate_items,
                uncovered_latin_terms,
                rrf_k=effective_rrf_k,
            )
            covered_ids = {item["hit"].passage_id for item in covered_raw_items}
            raw_candidate_items = [
                item
                for item in raw_candidate_items
                if item["hit"].passage_id in covered_ids
            ]
        if not raw_candidate_items:
            self.selected = selected
            self.raw_fts_hits = raw_fts_hits
            self.raw_index_warning = raw_index_warning
            self.lexical_mode = lexical_mode
            self.coverage_fallback = coverage_fallback
            return

        if branch in {"coverage", "all_coverage"}:
            merged = recall_policy.merge_coverage_items(
                selected,
                raw_candidate_items,
                uncovered_latin_terms,
                rrf_k=effective_rrf_k,
            )
            selected = recall_policy.adaptive_expand(merged, top_k)[:top_k]
            candidates = [*candidate_pool, *raw_candidate_items]
            coverage_fallback = any(
                item["hit"].source_kind == "raw" for item in selected
            )
        else:
            selected = select_best_per_page(raw_candidate_items)
            selected = recall_policy.adaptive_expand(selected, top_k)[:top_k]
            candidates = raw_candidate_items
            lexical_mode = (
                resolve_fallback_lexical_mode(
                    plan,
                    raw_lexical_mode,
                    qualified_identifier=has_qualified_identifier(question),
                )
                or lexical_mode
            )

        recovery = assemble_recovery(
            selected,
            condition=plan.condition,
            candidates=candidates,
            store=self.store,
            raw_store=raw_store,
            cancellation=self.cancellation,
        )
        self.selected = selected
        self.context_items = recovery.context_items
        self.recovery = recovery
        self.raw_fts_hits = raw_fts_hits
        self.raw_index_warning = raw_index_warning
        self.lexical_mode = lexical_mode
        self.coverage_fallback = coverage_fallback

    def recovery_or_raise(self) -> RecoveryAssembly:
        if self.recovery is None:
            raise RuntimeError("query execution recovery has not been initialized")
        return self.recovery

    def outcome(self) -> QueryExecutionOutcome:
        """Freeze and publish the complete state for the envelope assembler."""

        if self._outcome is not None:
            return self._outcome
        recovery = self.recovery_or_raise()
        outcome = QueryExecutionOutcome(
            selected=_freeze_value(self.selected),
            context_items=_freeze_value(self.context_items),
            recovery=_freeze_recovery(recovery),
            status=_freeze_value(self.status),
            raw_availability=self._raw_availability,
            raw_fts_hits=self.raw_fts_hits,
            relaxed_fts_hits=self.relaxed_fts_hits,
            raw_index_warning=self.raw_index_warning,
            coverage_fallback=self.coverage_fallback,
            lexical_mode=self.lexical_mode,
            expansion_suggestions=_freeze_value(self.expansion_suggestions),
            uncovered_latin_terms=_freeze_value(self.uncovered_latin_terms),
            discovery=_freeze_value(self.discovery),
            discovery_entities=_freeze_value(self.discovery_entities),
            discovery_source_items=_freeze_value(self.discovery_source_items),
            batch_payload=_freeze_value(self.batch_payload),
            discovery_requested=self.discovery_requested,
        )
        object.__setattr__(self, "_outcome", outcome)
        object.__setattr__(self, "_sealed", True)
        return outcome

    def _run_fallback_recovery(
        self,
        *,
        question: str,
        effective_scope: str,
        project: str | None,
        filters: QueryFilters,
        top_k: int,
        metadata: dict[str, dict[str, Any]],
        snapshot: QueryCorpusSnapshot,
        expansion_terms: dict[str, list[str]] | None,
        intent: str,
        effective_rrf_k: int,
        has_primary_recall: bool,
        selected: list[dict[str, Any]],
        scored: list[dict[str, Any]],
        recovery: RecoveryAssembly,
        stage_lexical_mode: str,
    ) -> None:
        """Run fallback branches and retain their state on this context."""

        self._ensure_open()
        self.selected = selected
        self.context_items = recovery.context_items
        self.recovery = recovery
        self.raw_fts_hits = 0
        self.relaxed_fts_hits = 0
        self.raw_index_warning = ""
        self.coverage_fallback = False
        self.lexical_mode = stage_lexical_mode
        self.expansion_suggestions = []
        self.uncovered_latin_terms = (
            recall_policy.uncovered_latin_terms(question, selected)
            if has_primary_recall and effective_scope in {"knowledge", "all"}
            else []
        )
        query_extra_terms: list[str] = []
        query_term_variants: dict[str, list[str]] = {}
        wiki_relaxed_answered = False

        self.cancellation.checkpoint("fallback")
        coverage_state = FallbackState(
            has_primary_recall=has_primary_recall,
            effective_scope=effective_scope,
            uncovered_latin_terms=tuple(self.uncovered_latin_terms),
            wiki_relaxed_answered=False,
            raw_available="unknown",
            relaxed_available=False,
        )
        coverage_plan = plan_fallback(coverage_state)
        if coverage_plan is not None and coverage_plan.branch == "coverage":
            self.run_raw_branch(
                branch="coverage",
                question=question,
                project=project,
                filters=filters,
                top_k=top_k,
                extra_terms=[],
                term_variants={},
                uncovered_latin_terms=self.uncovered_latin_terms,
                effective_rrf_k=effective_rrf_k,
                selected=self.selected,
                candidate_pool=scored,
                plan=coverage_plan,
                lexical_mode=self.lexical_mode,
                coverage_fallback=self.coverage_fallback,
            )

        relaxed_state = FallbackState(
            has_primary_recall=has_primary_recall,
            effective_scope=effective_scope,
            uncovered_latin_terms=tuple(self.uncovered_latin_terms),
            wiki_relaxed_answered=False,
            raw_available="unknown",
            relaxed_available=True,
        )
        relaxed_plan = plan_fallback(relaxed_state)
        if relaxed_plan is not None and relaxed_plan.branch == "wiki_relaxed":
            self.cancellation.checkpoint("fallback")
            query_extra_terms, query_term_variants, self.expansion_suggestions = (
                recall_policy.query_expansion(
                    question,
                    self.store,
                    None,
                    expansion_terms,
                    project,
                    snapshot=snapshot,
                    raw_snapshot=self.raw_snapshot,
                    cancellation=self.cancellation,
                )
            )
            self.cancellation.checkpoint("fallback")
            wiki_relaxed_items, self.relaxed_fts_hits, relaxed_warning = (
                recall_policy.relaxed_recovery_items(
                    self.store,
                    metadata,
                    question,
                    scope=effective_scope,
                    project=project,
                    filters=filters,
                    extra_terms=query_extra_terms,
                    cancellation=self.cancellation,
                )
            )
            if relaxed_warning:
                self.status = {**self.status, "code": relaxed_warning}
            if wiki_relaxed_items:
                step_counts = recall_policy.step_counts_for_pages(
                    sorted({item["hit"].page_path for item in wiki_relaxed_items}),
                    self.store,
                    None,
                )
                for item in wiki_relaxed_items:
                    count = step_counts.get(item["hit"].page_path, 0)
                    item["score"] = compose_score(
                        item["hit"],
                        question,
                        intent,
                        effective_scope,
                        metadata,
                        base=item["hit"].score,
                        freshness=True,
                        step_bonus=step_bonus(count),
                    )
                wiki_relaxed_items.sort(
                    key=lambda item: (
                        -item["score"],
                        item["hit"].page_path,
                        item["hit"].passage_id,
                    )
                )
                self.selected = recall_policy.adaptive_expand(
                    select_best_per_page(wiki_relaxed_items), top_k
                )
                self.recovery = assemble_recovery(
                    self.selected,
                    condition=relaxed_plan.condition,
                    candidates=wiki_relaxed_items,
                    store=self.store,
                    cancellation=self.cancellation,
                )
                self.context_items = self.recovery.context_items
                self.lexical_mode = relaxed_plan.lexical_mode or self.lexical_mode
                wiki_relaxed_answered = True
                self.uncovered_latin_terms = recall_policy.uncovered_latin_terms(
                    question, self.selected
                )
                all_coverage_state = FallbackState(
                    has_primary_recall=has_primary_recall,
                    effective_scope=effective_scope,
                    uncovered_latin_terms=tuple(self.uncovered_latin_terms),
                    wiki_relaxed_answered=True,
                    raw_available="unknown",
                    relaxed_available=True,
                )
                all_coverage_plan = plan_fallback(all_coverage_state)
                if (
                    all_coverage_plan is not None
                    and all_coverage_plan.branch == "all_coverage"
                ):
                    self.run_raw_branch(
                        branch="all_coverage",
                        question=question,
                        project=project,
                        filters=filters,
                        top_k=top_k,
                        extra_terms=query_extra_terms,
                        term_variants=query_term_variants,
                        uncovered_latin_terms=self.uncovered_latin_terms,
                        effective_rrf_k=effective_rrf_k,
                        selected=self.selected,
                        candidate_pool=wiki_relaxed_items,
                        plan=all_coverage_plan,
                        lexical_mode=self.lexical_mode,
                        coverage_fallback=self.coverage_fallback,
                    )

        raw_zero_state = FallbackState(
            has_primary_recall=has_primary_recall,
            effective_scope=effective_scope,
            uncovered_latin_terms=tuple(self.uncovered_latin_terms),
            wiki_relaxed_answered=wiki_relaxed_answered,
            raw_available="unknown",
            relaxed_available=wiki_relaxed_answered,
        )
        raw_zero_plan = plan_fallback(raw_zero_state)
        if raw_zero_plan is not None and raw_zero_plan.branch == "raw_zero":
            self.run_raw_branch(
                branch="raw_zero",
                question=question,
                project=project,
                filters=filters,
                top_k=top_k,
                extra_terms=query_extra_terms,
                term_variants=query_term_variants,
                uncovered_latin_terms=self.uncovered_latin_terms,
                effective_rrf_k=effective_rrf_k,
                selected=self.selected,
                candidate_pool=(),
                plan=raw_zero_plan,
                lexical_mode=self.lexical_mode,
                coverage_fallback=self.coverage_fallback,
            )

    def _run_discovery_and_batch(
        self,
        *,
        question: str,
        metadata: dict[str, dict[str, Any]],
        effective_scope: str,
        project: str | None,
        filters: QueryFilters,
        snapshot: QueryCorpusSnapshot,
        retrieval_mode: Literal["lexical", "vector", "hybrid"],
        hard_budget_tokens: int,
        confirmation_token: str | None,
    ) -> None:
        """Invoke the discovery and entity-batch owners at their seam."""

        self._ensure_open()

        def raw_discovery_source() -> (
            tuple[RetrievalIndexStore, QueryCorpusSnapshot] | None
        ):
            if self.raw_availability() != "fresh":
                return None
            return self.get_raw_store(), self.capture_raw_snapshot()

        discovery_result = discovery_owner.discover_catalog(
            store=self.store,
            snapshot=snapshot,
            question=question,
            selected=self.selected,
            context_items=self.context_items,
            effective_scope=effective_scope,
            project=project,
            filters=filters,
            cancellation=self.cancellation,
            raw_provider=raw_discovery_source,
        )
        discovery_entities = [dict(entity) for entity in discovery_result.entities]
        self.discovery = dict(discovery_result.discovery)
        self.discovery_entities = discovery_entities
        self.discovery_source_items = [
            dict(item) for item in discovery_result.source_items
        ]
        self.discovery_requested = discovery_result.requested

        self.batch_payload = {
            "status": "not_triggered",
            "reason": "structured_enumeration_evidence_insufficient",
            "entities": [],
        }
        if discovery_entities:
            self.cancellation.checkpoint("fallback")
            batch_raw_snapshot: QueryCorpusSnapshot | None = self.raw_snapshot
            batch_raw_availability: RawAvailability = "missing"
            if effective_scope in {"knowledge", "all"}:
                batch_raw_availability = self.raw_availability()
                batch_raw_snapshot = self.capture_raw_snapshot()
            batch_result = entity_batch_owner.run_entity_batch(
                discovery_entities,
                question,
                primary_store=self.store,
                effective_scope=effective_scope,
                project=project,
                filters=filters,
                retrieval_mode=retrieval_mode,
                hard_budget_tokens=hard_budget_tokens,
                confirmation_token=confirmation_token,
                snapshot=snapshot,
                raw_snapshot=batch_raw_snapshot,
                raw_store=self.raw_store,
                raw_availability=batch_raw_availability,
                cancellation=self.cancellation,
            )
            self.batch_payload = dict(batch_result.payload)
