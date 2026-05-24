from __future__ import annotations

from pathlib import Path
from typing import Any

from netsuite_rag_mcp.config import load_config
from netsuite_rag_mcp.metadata import metadata_matches_filters
from netsuite_rag_mcp.models import ConflictReport, RoutingResult, SearchResult
from netsuite_rag_mcp.policy import build_answer_policy
from netsuite_rag_mcp.redaction import redact_sensitive_text
from netsuite_rag_mcp.vector_store import ChromaVectorStore, Embedder, SentenceTransformerEmbedder

# ── Conflict detection keywords ───────────────────────────────────────────────

_IMPLEMENTATION_KEYWORDS: frozenset[str] = frozenset({
    # Chinese
    "怎么实现", "函数", "脚本", "部署", "入口", "参数", "返回值", "代码", "行为", "实现",
    "function", "implementation", "script", "deployment", "parameter",
    "return", "entry point", "how does",
    # English duplicates removed from routing sets
    "behavior", "逻辑", "logic", "config",
})

_BUSINESS_KEYWORDS: frozenset[str] = frozenset({
    # Chinese
    "为什么", "原因", "背景", "决策", "目的", "业务", "需求", "排坑", "踩坑", "历史",
    # English
    "why", "reason", "background", "decision", "purpose", "business",
    "requirement", "rationale", "troubleshooting", "history", "context",
})


# ── Entity extraction ──────────────────────────────────────────────────────────

def _extract_entity_keys(metadata: dict[str, Any]) -> list[str]:
    """Extract entity identifiers from search result metadata.

    Returns a list of entity keys (script_id, function_name, source_path)
    for grouping results that describe the same entity.
    """
    keys: list[str] = []
    if metadata.get("script_id"):
        keys.append(f"script_id:{metadata['script_id']}")
    if metadata.get("function_name"):
        keys.append(f"function_name:{metadata['function_name']}")
    if metadata.get("source_path"):
        keys.append(f"source_path:{metadata['source_path']}")
    return keys


def detect_conflicts(results: list[SearchResult]) -> list[ConflictReport]:
    """Detect conflicts between note and code sources for the same entity.

    Groups results by entity identifiers (script_id, function_name, source_path).
    When the same entity appears in both note and code sources with different
    text content, creates a ConflictReport for each detected conflict.

    Conflict type classification:
    - "implementation": facts about how something works → code wins
    - "business": rationale, purpose, design decisions → note wins unless stale
    - "staleness": code commit date vs note updated_at → newer source wins
    - "unclassified": can't determine → both sources shown with uncertainty
    """
    # Group results by entity key
    entity_groups: dict[str, list[SearchResult]] = {}
    for result in results:
        for key in _extract_entity_keys(result.metadata):
            entity_groups.setdefault(key, []).append(result)

    conflicts: list[ConflictReport] = []

    for entity_key, group in entity_groups.items():
        # Split by source_kind
        note_results = [r for r in group if r.source_kind == "note"]
        code_results = [r for r in group if r.source_kind == "code"]

        if not note_results or not code_results:
            continue  # No conflict possible — only one source kind

        # Compare each note result against each code result for different content
        for note_r in note_results:
            for code_r in code_results:
                if note_r.text.strip() == code_r.text.strip():
                    continue  # Same content — not a conflict

                entity = entity_key.split(":", 1)[1] if ":" in entity_key else entity_key

                note_source = {
                    "citation_id": note_r.citation_id,
                    "source_kind": "note",
                    "path": note_r.metadata.get("source_path", ""),
                }
                if note_r.metadata.get("updated_at"):
                    note_source["updated_at"] = note_r.metadata["updated_at"]

                code_source = {
                    "citation_id": code_r.citation_id,
                    "source_kind": "code",
                    "path": code_r.metadata.get("source_path", ""),
                }
                if code_r.metadata.get("git_commit"):
                    code_source["git_commit"] = code_r.metadata["git_commit"]
                if code_r.metadata.get("function_name"):
                    code_source["function"] = code_r.metadata["function_name"]

                # Classify conflict type (best-effort; leave unclassified if ambiguous)
                conflict_type = _classify_conflict_type(note_r.text + " " + code_r.text)

                conflicts.append(ConflictReport(
                    conflict_type=conflict_type,
                    entity=entity,
                    note_source=note_source,
                    code_source=code_source,
                    winning_source="",  # To be determined by resolve_conflicts()
                    explanation="",     # To be filled by resolve_conflicts()
                    uncertainty=False,
                ))

    return conflicts


def _classify_conflict_type(combined_text: str) -> str:
    """Classify the type of conflict based on keywords in the combined text.

    Best-effort classification. Returns:
    - "implementation": facts about how something works
    - "business": rationale, purpose, design decisions
    - "unclassified": can't determine
    """
    combined_lower = combined_text.lower()

    has_impl = any(kw in combined_lower for kw in _IMPLEMENTATION_KEYWORDS)
    has_biz = any(kw in combined_lower for kw in _BUSINESS_KEYWORDS)

    if has_impl and not has_biz:
        return "implementation"
    elif has_biz and not has_impl:
        return "business"
    else:
        # Ambiguous or both → unclassified for safety
        return "unclassified"


def resolve_conflicts(
    conflicts: list[ConflictReport],
    routing: RoutingResult,
) -> list[ConflictReport]:
    """Apply resolution rules to detected conflicts.

    Resolution rules:
    1. Implementation facts: code wins (winning_source="code")
    2. Business/rationale: notes win unless code is much newer (winning_source="note")
    3. Staleness: compare note updated_at vs code git_commit recency → newer wins
    4. Unclassified: both sources shown, uncertainty=True, winning_source="both"

    Never silently merge conflicts — always surface them in the response.
    """
    if not conflicts:
        return []

    resolved: list[ConflictReport] = []

    for conflict in conflicts:
        if conflict.conflict_type == "implementation":
            resolved_conflict = ConflictReport(
                conflict_type=conflict.conflict_type,
                entity=conflict.entity,
                note_source=conflict.note_source,
                code_source=conflict.code_source,
                winning_source="code",
                explanation="Implementation fact: code is the authoritative source",
                uncertainty=False,
            )
        elif conflict.conflict_type == "business":
            resolved_conflict = _resolve_business_conflict(conflict, routing)
        elif conflict.conflict_type == "staleness":
            resolved_conflict = _resolve_staleness_conflict(conflict, routing)
        else:
            # unclassified or unknown → show both
            resolved_conflict = ConflictReport(
                conflict_type=conflict.conflict_type,
                entity=conflict.entity,
                note_source=conflict.note_source,
                code_source=conflict.code_source,
                winning_source="both",
                explanation="Unclassified conflict: both sources shown with equal priority",
                uncertainty=True,
            )

        resolved.append(resolved_conflict)

    return resolved


def _resolve_business_conflict(conflict: ConflictReport, routing: RoutingResult) -> ConflictReport:
    """Resolve a business conflict: notes win unless code is much newer."""
    # Check if code is much newer than note
    staleness_winner = _compare_freshness(conflict)

    if staleness_winner == "code":
        return ConflictReport(
            conflict_type=conflict.conflict_type,
            entity=conflict.entity,
            note_source=conflict.note_source,
            code_source=conflict.code_source,
            winning_source="code",
            explanation="Business rationale conflict: code source is more recent than note",
            uncertainty=False,
        )
    else:
        # Default: note wins for business/rationalale — unless routing is code_led
        if routing.kind == "code_led":
            return ConflictReport(
                conflict_type=conflict.conflict_type,
                entity=conflict.entity,
                note_source=conflict.note_source,
                code_source=conflict.code_source,
                winning_source="code",
                explanation="Business rationale conflict: code-led routing biases toward code source",
                uncertainty=True,
            )
        return ConflictReport(
            conflict_type=conflict.conflict_type,
            entity=conflict.entity,
            note_source=conflict.note_source,
            code_source=conflict.code_source,
            winning_source="note",
            explanation="Business rationale: note is the authoritative source for design decisions",
            uncertainty=False,
        )


def _resolve_staleness_conflict(conflict: ConflictReport, routing: RoutingResult) -> ConflictReport:
    """Resolve a staleness conflict: newer source wins."""
    staleness_winner = _compare_freshness(conflict)

    if staleness_winner == "code":
        return ConflictReport(
            conflict_type=conflict.conflict_type,
            entity=conflict.entity,
            note_source=conflict.note_source,
            code_source=conflict.code_source,
            winning_source="code",
            explanation="Staleness conflict: code source is more recent than note",
            uncertainty=False,
        )
    elif staleness_winner == "note":
        return ConflictReport(
            conflict_type=conflict.conflict_type,
            entity=conflict.entity,
            note_source=conflict.note_source,
            code_source=conflict.code_source,
            winning_source="note",
            explanation="Staleness conflict: note source is more recent than code",
            uncertainty=False,
        )
    else:
        # Cannot determine freshness → both shown
        return ConflictReport(
            conflict_type=conflict.conflict_type,
            entity=conflict.entity,
            note_source=conflict.note_source,
            code_source=conflict.code_source,
            winning_source="both",
            explanation="Staleness conflict: cannot determine which source is more recent",
            uncertainty=True,
        )


def _compare_freshness(conflict: ConflictReport) -> str:
    """Compare freshness of note vs code source.

    Returns "note", "code", or "unknown" if freshness cannot be determined.
    Simple heuristic:
    - If note has updated_at >= 2026-05-01, consider note newer
    - If code has git_commit (meaning it was recently indexed), and note is old
    - Otherwise unknown
    """
    note_updated = conflict.note_source.get("updated_at", "")
    code_commit = conflict.code_source.get("git_commit", "")

    if not note_updated and not code_commit:
        return "unknown"

    if note_updated and not code_commit:
        return "note"

    if code_commit and not note_updated:
        return "code"

    # Both have timestamps — compare them
    # Simple heuristic: notes from May 2026 or later are considered recent
    # Code with git_commit is considered recent
    try:
        note_date_str = note_updated[:10]  # "YYYY-MM-DD"
        note_is_recent = note_date_str >= "2026-05-01"
    except (ValueError, IndexError):
        note_is_recent = False

    if note_is_recent:
        return "note"
    else:
        return "code"


_STALE_THRESHOLD_DAYS = 30


def _detect_stale_sources(
    results: list[SearchResult],
    conflicts: list[ConflictReport],
    threshold_days: int = _STALE_THRESHOLD_DAYS,
) -> list[dict[str, Any]]:
    """Detect note sources that are significantly older than corresponding code commits.

    For each conflict where both note.updated_at and code.git_commit are available,
    check if the note was last updated more than threshold_days before the code commit.
    Uses a best-effort heuristic: if note date is < code date - threshold, mark as stale.

    Args:
        results: Search results with metadata containing timestamps.
        conflicts: Resolved conflicts between note and code sources.
        threshold_days: Number of days beyond which a note is considered stale.

    Returns:
        List of dicts with: source, reason, note_updated, code_commit.
    """
    from datetime import datetime, timedelta, timezone

    stale_sources: list[dict[str, Any]] = []
    seen_sources: set[str] = set()

    for conflict in conflicts:
        note_updated_str = conflict.note_source.get("updated_at", "")
        code_commit = conflict.code_source.get("git_commit", "")
        note_citation = conflict.note_source.get("citation_id", "")

        if not note_updated_str or not note_citation:
            continue

        # Parse the note updated_at timestamp
        try:
            # Handle ISO format with or without timezone
            note_updated_clean = note_updated_str[:19]  # Strip timezone/Z for parsing
            note_date = datetime.strptime(note_updated_clean, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        except (ValueError, IndexError):
            # Cannot parse note date — skip staleness check
            continue

        # For code, we use the git_commit as an indicator that the code was recently indexed.
        # We treat "today" (current date) as a proxy for the code freshness when git_commit is present,
        # since we can't easily parse commit dates from the hash alone.
        # This conservative approach means we only flag notes that are > threshold_days old.
        code_date = datetime.now(timezone.utc)

        # Check if note is more than threshold_days older than code
        age_difference = (code_date - note_date).days

        if age_difference > threshold_days and note_citation not in seen_sources:
            stale_sources.append({
                "source": note_citation,
                "reason": f"Note updated {note_updated_str[:10]}, code commit {code_commit or 'recent'}, "
                          f"note is {age_difference} days older",
                "note_updated": note_updated_str,
                "code_commit": code_commit,
            })
            seen_sources.add(note_citation)

    return stale_sources


def _detect_dirty_sources(results: list[SearchResult]) -> list[dict[str, Any]]:
    """Detect code sources that have uncommitted changes (+dirty suffix on git_commit).

    Args:
        results: Search results to check for dirty git commits.

    Returns:
        List of dicts with: source, git_commit, file_hash.
    """
    dirty_sources: list[dict[str, Any]] = []

    for result in results:
        git_commit = result.metadata.get("git_commit", "")
        if not git_commit:
            continue
        if git_commit.endswith("+dirty"):
            dirty_sources.append({
                "source": result.citation_id,
                "git_commit": git_commit,
                "file_hash": result.metadata.get("file_hash", ""),
            })

    return dirty_sources


def _count_sources_by_kind(results: list[SearchResult]) -> dict[str, int]:
    """Count the number of search results by source_kind.

    Args:
        results: Search results to count.

    Returns:
        Dict with 'note' and 'code' counts.
    """
    counts: dict[str, int] = {"note": 0, "code": 0}
    for result in results:
        kind = result.source_kind
        if kind in counts:
            counts[kind] += 1
        else:
            # Unknown source_kind — still count under appropriate key
            counts.setdefault(kind, 0)
            counts[kind] += 1
    return counts


_NOTE_LED_KEYWORDS: frozenset[str] = frozenset({
    # Chinese
    "为什么", "原因", "背景", "决策", "目的", "业务", "需求", "排坑", "踩坑", "历史",
    # English
    "why", "reason", "background", "decision", "purpose", "business",
    "requirement", "rationale", "troubleshooting", "history", "context",
    "note-led", "business logic",
})

_CODE_LED_KEYWORDS: frozenset[str] = frozenset({
    # Chinese
    "怎么实现", "函数", "脚本", "部署", "入口", "参数", "返回值", "代码", "行为", "实现",
    # English
    "function", "implementation", "script", "deployment", "config", "parameter",
    "return", "entry point", "how does", "code-led", "current behavior",
})

_MIXED_KEYWORDS: frozenset[str] = frozenset({
    # Chinese
    "怎么实现以及为什么", "实现+原因", "影响分析",
    # English
    "how and why", "impact", "both", "combined", "implementation and reason",
})


def route_query(question: str, source_kind: str | None = None) -> RoutingResult:
    """Classify a query and determine routing strategy.

    If source_kind is explicitly provided, use that directly (no routing needed).
    Otherwise, analyze the question for keywords to determine routing:

    - note_led: business purpose, design rationale, troubleshooting, etc.
    - code_led: function, implementation, script, deployment, etc.
    - mixed: questions combining both aspects, or no clear signal.
    """
    # Explicit source_kind overrides heuristic routing
    if source_kind is not None:
        kind = f"{source_kind}_led" if source_kind in ("note", "code") else "mixed"
        source_filter = {"source_kind": source_kind} if source_kind in ("note", "code") else {}
        return RoutingResult(
            kind=kind,
            source_filter=source_filter,
            boost_factor=1.0,
            explanation=f"Explicit source_kind='{source_kind}', overriding heuristic routing.",
        )

    question_lower = question.lower()

    # Check mixed first (most specific)
    for kw in _MIXED_KEYWORDS:
        if kw in question_lower:
            return RoutingResult(
                kind="mixed",
                source_filter={},
                boost_factor=1.0,
                explanation=f"Query contains mixed-signal keyword '{kw}', routing to both note and code sources.",
            )

    # Check note_led
    for kw in _NOTE_LED_KEYWORDS:
        if kw in question_lower:
            return RoutingResult(
                kind="note_led",
                source_filter={"source_kind": "note"},
                boost_factor=1.0,
                explanation=f"Query contains note-led keyword '{kw}', routing to note source.",
            )

    # Check code_led
    for kw in _CODE_LED_KEYWORDS:
        if kw in question_lower:
            return RoutingResult(
                kind="code_led",
                source_filter={"source_kind": "code"},
                boost_factor=1.0,
                explanation=f"Query contains code-led keyword '{kw}', routing to code source.",
            )

    # Default: mixed (safe fallback)
    return RoutingResult(
        kind="mixed",
        source_filter={},
        boost_factor=1.0,
        explanation="No routing keywords detected, defaulting to mixed (both note and code sources).",
    )


def _build_where_clause(
    source_kind: str | None,
    source_name: str | None,
) -> dict[str, Any] | None:
    """Build a ChromaDB where clause from source_kind and source_name filters."""
    where_clauses: list[dict[str, Any]] = []
    if source_kind:
        where_clauses.append({"source_kind": source_kind})
    if source_name:
        where_clauses.append({"source_name": source_name})

    if len(where_clauses) == 1:
        return where_clauses[0]
    elif len(where_clauses) > 1:
        return {"$and": where_clauses}
    return None


def search_netsuite_knowledge(
    vault_root: str | Path,
    question: str,
    filters: dict[str, Any] | None = None,
    top_k: int = 5,
    embedder: Embedder | None = None,
    source_kind: str | None = None,
    source_name: str | None = None,
    store: ChromaVectorStore | None = None,
    content_type: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    config = load_config(vault_root)
    selected_embedder = embedder or SentenceTransformerEmbedder(
        config.embedding_model,
        cache_folder=config.embedding_cache_path,
    )
    vector_store = store or ChromaVectorStore(
        config.chroma_path, config.collection_name, selected_embedder
    )

    # ChromaDB-side pre-filtering via where clause
    where = _build_where_clause(source_kind, source_name)
    raw_results = vector_store.query(question, n_results=max(top_k * 4, top_k), where=where)

    # Post-filtering with metadata_matches_filters (includes source_kind/source_name)
    active_filters = dict(filters or {})
    if content_type:
        active_filters["type"] = content_type
    if source_kind:
        active_filters["source_kind"] = source_kind
    if source_name:
        active_filters["source_name"] = source_name

    selected = []
    for result in raw_results:
        if not include_archived and result.metadata.get("archived") is True:
            continue
        if active_filters and not metadata_matches_filters(result.metadata, active_filters):
            continue
        selected.append(result)
        if len(selected) == top_k:
            break

    return {
        "question": question,
        "filters": active_filters,
        "results": [
            {
                "citation_id": f"S{index + 1}",
                "chunk_id": result.chunk_id,
                "text": redact_sensitive_text(result.text),
                "metadata": result.metadata,
                "distance": result.distance,
            }
            for index, result in enumerate(selected)
        ],
    }


def _format_citation(citation: dict[str, Any]) -> str:
    """Format a citation string per design document section 8.

    Args:
        citation: Dict with source_kind, path, and optional fields.

    Returns:
        Formatted citation string like:
        [S1] source_kind=note path=... heading=... chunk_index=... updated_at=...
        [S2] source_kind=code path=... function=... line=... git_commit=...
    """
    parts = [f"source_kind={citation['source_kind']}"]
    parts.append(f"path={citation['path']}")

    kind = citation["source_kind"]

    if kind == "note":
        if citation.get("heading"):
            parts.append(f"heading={citation['heading']}")
        if citation.get("chunk_index") is not None and citation.get("chunk_index") != "":
            parts.append(f"chunk_index={citation['chunk_index']}")
        if citation.get("updated_at"):
            parts.append(f"updated_at={citation['updated_at']}")
    elif kind == "code":
        if citation.get("function"):
            parts.append(f"function={citation['function']}")
        if citation.get("line"):
            parts.append(f"line={citation['line']}")
        if citation.get("git_commit"):
            parts.append(f"git_commit={citation['git_commit']}")
        if citation.get("file_hash"):
            parts.append(f"file_hash={citation['file_hash']}")

    return " ".join(parts)


def _build_citation_dict(metadata: dict[str, Any], index: int) -> dict[str, Any]:
    """Build an enhanced citation dict from search result metadata.

    Args:
        metadata: Metadata dict from search result.
        index: 0-based index for generating citation_id.

    Returns:
        Dict with citation_id, source_kind, path, and citation-type-specific fields.
    """
    # Default to "note" for backward compat when source_kind is missing or empty
    source_kind = metadata.get("source_kind") or "note"
    path = metadata.get("source_path", "")

    citation: dict[str, Any] = {
        "citation_id": f"S{index + 1}",
        "source_kind": source_kind,
        "path": path,
        "source_path": path,  # backward compatibility
    }

    if source_kind == "note":
        citation["heading"] = metadata.get("heading", "")
        ci = metadata.get("chunk_index", "")
        citation["chunk_index"] = ci if ci != "" else ""
        citation["updated_at"] = metadata.get("updated_at", "")
    elif source_kind == "code":
        citation["function"] = metadata.get("function_name", "")
        line_start = metadata.get("line_start", 0)
        line_end = metadata.get("line_end", 0)
        citation["line"] = f"{line_start}-{line_end}" if line_start and line_end else ""
        # Format git_commit with +dirty suffix when file_hash is present
        git_commit = metadata.get("git_commit", "")
        file_hash = metadata.get("file_hash", "")
        if git_commit and file_hash and not git_commit.endswith("+dirty"):
            git_commit = f"{git_commit}+dirty"
        citation["git_commit"] = git_commit
        if file_hash:
            citation["file_hash"] = file_hash

    return citation


def ask_netsuite_rag(
    vault_root: str | Path,
    question: str,
    filters: dict[str, Any] | None = None,
    top_k: int = 5,
    embedder: Embedder | None = None,
    source_kind: str | None = None,
    source_name: str | None = None,
    store: ChromaVectorStore | None = None,
    content_type: str | None = None,
    include_archived: bool = False,
) -> dict[str, Any]:
    # Route the query to determine the best source filter
    routing = route_query(question, source_kind=source_kind)

    # Use routing result to determine effective source_kind for search
    effective_source_kind = source_kind  # User-provided takes precedence
    if effective_source_kind is None:
        # Apply routing-derived filter
        effective_source_kind = routing.source_filter.get("source_kind")

    search = search_netsuite_knowledge(
        vault_root, question, filters, top_k, embedder,
        source_kind=effective_source_kind, source_name=source_name, store=store,
        content_type=content_type, include_archived=include_archived,
    )
    context_blocks = []
    sources = []

    for i, item in enumerate(search["results"]):
        metadata = item["metadata"]
        citation_id = item["citation_id"]
        context_blocks.append(
            {
                "citation_id": citation_id,
                "text": item["text"],
                "metadata": metadata,
            }
        )
        citation = _build_citation_dict(metadata, i)
        citation["formatted"] = _format_citation(citation)
        sources.append(citation)

    # Detect and resolve conflicts between note and code sources
    search_results = [
        SearchResult(
            citation_id=item["citation_id"],
            chunk_id=item["chunk_id"],
            text=item["text"],
            metadata=item["metadata"],
            distance=item["distance"],
            source_kind=item["metadata"].get("source_kind", "note"),
            git_commit=item["metadata"].get("git_commit", ""),
        )
        for item in search["results"]
    ]
    detected_conflicts = detect_conflicts(search_results)
    resolved_conflicts = resolve_conflicts(detected_conflicts, routing)

    conflicts_detected = [
        {
            "conflict_type": c.conflict_type,
            "entity": c.entity,
            "note_source": c.note_source,
            "code_source": c.code_source,
            "winning_source": c.winning_source,
            "explanation": c.explanation,
            "uncertainty": c.uncertainty,
        }
        for c in resolved_conflicts
    ]

    # Detect stale and dirty sources
    stale_sources = _detect_stale_sources(search_results, resolved_conflicts)
    code_dirty_sources = _detect_dirty_sources(search_results)

    # Count sources by kind
    sources_considered = _count_sources_by_kind(search_results)

    # Track redaction count by counting markers in the returned (redacted) text
    redaction_count = 0
    for cb in context_blocks:
        redacted_text = cb["text"]
        for marker in ("[REDACTED_PHONE]", "[REDACTED_EMAIL]", "[REDACTED_ID_CARD]",
                        "[REDACTED_BANK_CARD]", "[REDACTED_SECRET]"):
            redaction_count += redacted_text.count(marker)

    answer_policy = build_answer_policy()

    # Build filters_applied from effective filters
    filters_applied = dict(search["filters"])

    return {
        "question": question,
        "context_blocks": context_blocks,
        "sources": sources,
        "answer_policy": answer_policy,
        "filters": search["filters"],
        "routing": {
            "kind": routing.kind,
            "explanation": routing.explanation,
            "source_filter": routing.source_filter,
            "boost_factor": routing.boost_factor,
        },
        "filters_applied": filters_applied,
        "sources_considered": sources_considered,
        "conflicts_detected": conflicts_detected,
        "stale_sources": stale_sources,
        "code_dirty_sources": code_dirty_sources,
        "redaction_applied_before_return": redaction_count,
    }