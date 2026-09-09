"""Pure catalog-discovery owner for Query V2.

Catalog discovery is an orthogonal, bounded branch over the invocation's
immutable corpus snapshot.  It does not own a retrieval store, cancellation
state, or query execution lifecycle; those are supplied by the context at the
single integration boundary.  The returned :class:`DiscoveryResult` is a
frozen value object so later query stages cannot mutate discovery state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
import json
import re
from types import MappingProxyType
from typing import Any, Literal

from retrieval.candidate_items import candidate_item
from retrieval.lexical_analyzer import (
    QualifiedIdentifier,
    extract_namespace_wildcards,
    extract_qualified_identifiers,
    plan_query,
    tokens,
)
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_shared import QueryFilters, eligible, heading, matches_request, snapshot_page_eligible
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.retrieval_index import PassageHit, RetrievalIndexError, RetrievalIndexStore


PASSAGE_SCAN_LIMIT = 500
PASSAGE_PROBE_LIMIT = 20
DISCOVERY_CANDIDATE_LIMIT = 240
DISCOVERY_PAGE_LIMIT = 32
DISCOVERY_SOURCE_PAGE_LIMIT = 8
PUBLIC_DISCOVERY_CANDIDATE_LIMIT = 40
PUBLIC_DISCOVERY_BYTE_LIMIT = 128 * 1024
_DISCOVERY_INTENT_RE = re.compile(
    r"(?:\blist\b|\ball\b|\beach\b|\bevery\b|\bvarious\b|\bdifferent\b|\btypes?\b|\bavailable\b|\bmodules?\b|\bcatalog\b|\bdirectory\b|\boverview\b|列出|有哪些|各|每|分别|类型|目录|模块|清单|列表)",
    re.I,
)
_DISCOVERY_EXPLANATORY_RE = re.compile(
    r"(?:\bwhat\s+is\b|\bexplain\b|\bdescribe\b|\bmeaning\b|是什么|何为|含义|解释|说明|介绍)",
    re.I,
)
_DISCOVERY_ANCHOR_STOPWORDS = frozenset(
    {
        "all",
        "available",
        "catalog",
        "directory",
        "list",
        "module",
        "modules",
        "overview",
        "reference",
        "show",
        "which",
    }
)
_DISCOVERY_CJK_ALIASES = {
    "模块": ("module", "modules"),
    "脚本": ("script", "scripts"),
    "类型": ("type", "types"),
    "目录": ("catalog", "directory", "overview", "list"),
    "清单": ("catalog", "directory", "list"),
    "列表": ("list", "listing"),
}
_DISCOVERY_HEADING_RE = re.compile(r"^\s*(?P<marks>#{2,6})\s+(?P<label>.+?)\s*$")
_DISCOVERY_LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d{1,3}\s*[.)、]\s+|[一二三四五六七八九十百\d]+\s*[、.)]\s+)(?P<label>.+?)\s*$")
_DISCOVERY_LINK_RE = re.compile(r"\[([^\]]+)\]\s*\([^)]*\)")
_DISCOVERY_STOPWORDS = frozenset(
    {
        "available",
        "example",
        "examples",
        "list",
        "module",
        "modules",
        "name",
        "names",
        "note",
        "notes",
        "overview",
        "script",
        "scripts",
        "type",
        "types",
        "usage",
    }
)


@dataclass(frozen=True)
class DiscoveryResult:
    """Frozen output of one bounded catalog-discovery invocation."""

    discovery: Mapping[str, Any]
    entities: tuple[Mapping[str, Any], ...]
    source_items: tuple[Mapping[str, Any], ...]
    requested: bool


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def metadata_from_snapshot(snapshot: QueryCorpusSnapshot, cancellation: QueryCancellationContext | None = None) -> dict[str, dict[str, Any]]:
    """Project page metadata from a snapshot without touching the backing store."""

    metadata: dict[str, dict[str, Any]] = {}
    for index, page in enumerate(snapshot.pages):
        if cancellation is not None:
            cancellation.checkpoint_batch(index, every=16, stage="metadata")
        frontmatter = page.get("frontmatter")
        metadata[str(page["path"])] = dict(frontmatter) if isinstance(frontmatter, Mapping) else {}
    return metadata


def _discovery_fragments(line: str) -> list[tuple[str, str, str]]:
    """Return structured labels, including rows flattened into one passage."""

    heading_match = _DISCOVERY_HEADING_RE.match(line)
    if heading_match:
        label = re.sub(r"^\s*\d{1,3}\s*[.)、]\s*", "", heading_match.group("label"))
        return [(f"heading:{len(heading_match.group('marks'))}", label, "heading")]
    if line.count("|") >= 3 and (line.lstrip().startswith("|") or " | " in line):
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        rows: list[list[str]] = []
        current: list[str] = []
        for cell in cells:
            if cell:
                current.append(cell)
            elif current:
                rows.append(current)
                current = []
        if current:
            rows.append(current)
        fragments: list[tuple[str, str, str]] = []
        starts_at_table_boundary = line.lstrip().startswith("|")
        for row_index, row in enumerate(rows):
            if row_index == 0 and not starts_at_table_boundary:
                continue
            if not row or all(re.fullmatch(r":?(?:-+\s*){2,}:?", cell) for cell in row):
                continue
            fragments.append(("table", row[0], "table"))
        return fragments
    item = _DISCOVERY_LIST_RE.match(line)
    if item:
        return [("list", item.group("label"), "list")]
    return []


def _clean_discovery_label(value: str) -> str:
    if "|" in value:
        value = value.split("|", 1)[0]
    value = re.sub(r"\[([^\]]+)\]\s*\([^)]*\)", r"\1", value)
    value = re.sub(r"[`*_]", "", value).strip()
    value = re.sub(r"(?<=\d)\s*\.\s*(?=\d)", ".", value)
    value = re.split(r"\s+(?:[-–—]|:|：)\s+|[:：]", value, maxsplit=1)[0].strip()
    return value.strip("-–—,，;；。.")


def _generic_discovery_entity(label: str) -> tuple[str, tuple[str, ...]] | None:
    cleaned = _clean_discovery_label(label)
    words = re.findall(r"[A-Za-z0-9_\u3400-\u9fff][A-Za-z0-9_\u3400-\u9fff -]*", cleaned)
    if not words or len(cleaned) > 80 or len(cleaned.split()) > 8:
        return None
    canonical = re.sub(r"\s+", " ", cleaned.casefold()).strip()
    if not canonical or canonical in _DISCOVERY_STOPWORDS or canonical.split()[0] in _DISCOVERY_STOPWORDS:
        return None
    if not re.search(r"[A-Za-z\u3400-\u9fff]", canonical):
        return None
    return canonical, (cleaned, canonical)


def _discovery_candidate_for_fragment(fragment: str) -> list[tuple[str, tuple[str, ...], QualifiedIdentifier | None]]:
    """Extract qualified IDs first, then a structured generic entity label."""

    visible_fragment = re.sub(r"\[([^\]]+)\]\s*\([^)]*\)", r"\1", fragment)
    visible_fragment = re.sub(r"\s*/\s*", "/", visible_fragment).strip()
    link_labels = _DISCOVERY_LINK_RE.findall(fragment)
    candidate_fragments = link_labels or [visible_fragment]
    qualified: list[tuple[str, tuple[str, ...], QualifiedIdentifier | None]] = []
    for candidate_fragment in candidate_fragments:
        candidate_text = re.sub(r"\s*/\s*", "/", candidate_fragment).strip()
        identifiers = extract_qualified_identifiers(candidate_text)
        for identifier in identifiers:
            explicit_boundary = "/" in candidate_text or re.search(r"[A-Za-z0-9][-_]\s*[A-Za-z0-9]", candidate_text) is not None
            spaced_entity = re.fullmatch(r"\s*[A-Za-z]\s+[A-Za-z][A-Za-z0-9_-]*\s*", candidate_text) is not None
            single_segment_prefix = len(identifier.prefix_segments) == 1 and len(identifier.prefix_segments[0]) == 1 and spaced_entity
            if not explicit_boundary and candidate_text.casefold() in _DISCOVERY_STOPWORDS:
                continue
            match = re.search(
                rf"(?<![a-z0-9_]){re.escape(identifier.prefix_segments[0])}"
                rf"(?P<separator>/|[-_]|[ \\t]+)"
                rf"[a-z][a-z0-9_-]*",
                candidate_text,
                re.I,
            )
            if match:
                prefix_is_single_letter = len(identifier.prefix_segments) == 1 and len(identifier.prefix_segments[0]) == 1
                starts_with_identifier = not candidate_text[: match.start()].strip(" -*+`[]()")
                separator = match.group("separator")
                if separator in {"-", "_"} and not prefix_is_single_letter:
                    continue
                if not starts_with_identifier and not prefix_is_single_letter:
                    continue
                if not starts_with_identifier and identifier.prefix_segments == ("a",):
                    continue
            if explicit_boundary or single_segment_prefix:
                qualified.append((identifier.canonical_id, identifier.aliases, identifier))
        if qualified:
            continue
        generic = _generic_discovery_entity(candidate_text)
        if generic is None:
            continue
        canonical, aliases = generic
        qualified.append((canonical, aliases, None))
    return qualified


def discovery_requested(question: str) -> bool:
    """Recognize generic listing intent without widening member queries."""

    wildcards = extract_namespace_wildcards(question)
    if wildcards:
        return not bool(_DISCOVERY_EXPLANATORY_RE.search(question))

    identifiers = _explicit_qualified_identifiers(question)
    if len(identifiers) == 1:
        return False
    return bool(_DISCOVERY_INTENT_RE.search(question))


def _explicit_qualified_identifiers(question: str) -> set[str]:
    """Return identifiers written with a visible namespace separator."""

    return {
        identifier.canonical_id
        for identifier in extract_qualified_identifiers(question)
        if identifier.raw_forms and re.search(r"[/\s_-]", identifier.raw_forms[0])
    }


def _discovery_suppressed(question: str) -> bool:
    """Keep one explicit target or explanatory wildcard out of enumeration."""

    wildcards = extract_namespace_wildcards(question)
    if wildcards and _DISCOVERY_EXPLANATORY_RE.search(question):
        return True
    return len(_explicit_qualified_identifiers(question)) == 1


def _discovery_anchor_query(question: str) -> str:
    plan = plan_query(question)
    wildcard_prefixes = {segment for item in plan.namespace_wildcards for segment in item.prefix_segments}
    anchors = [
        term
        for term in plan.latin_terms
        if len(term) > 1 and term not in wildcard_prefixes and term not in _DISCOVERY_ANCHOR_STOPWORDS
    ]
    return " ".join(dict.fromkeys(anchors))


def _discovery_focus_terms(question: str) -> set[str]:
    plan = plan_query(question)
    terms = {
        term
        for term in plan.latin_terms
        if len(term) > 1 and term not in _DISCOVERY_ANCHOR_STOPWORDS
    }
    for phrase, aliases in _DISCOVERY_CJK_ALIASES.items():
        if phrase in question:
            terms.update(aliases)
    return terms


def _wildcard_evidence_names(question: str, text: str) -> set[str]:
    names: set[str] = set()
    for wildcard in extract_namespace_wildcards(question):
        prefix = r"\s*\.\s*".join(re.escape(segment) for segment in wildcard.prefix_segments)
        pattern = re.compile(
            rf"(?<![a-z0-9]){prefix}\s*/\s*"
            rf"(?P<name>[a-z][a-z0-9_-]*(?:\s*/\s*[a-z][a-z0-9_-]*)*)"
            rf"(?![a-z0-9_/])",
            re.I,
        )
        for match in pattern.finditer(text):
            name = re.sub(r"\s*/\s*", "/", match.group("name")).casefold()
            if name not in {"a", "na", "none", "null"}:
                names.add(f"{wildcard.canonical_prefix}/{name}")
    return names


def _discovery_source_items(
    store: RetrievalIndexStore,
    metadata: dict[str, dict[str, Any]],
    question: str,
    *,
    scope: str,
    project: str | None,
    filters: QueryFilters,
    snapshot: QueryCorpusSnapshot | None = None,
    cancellation: QueryCancellationContext | None = None,
) -> list[dict[str, Any]]:
    """Find bounded discovery pages from an existing read-only projection."""

    anchor_query = _discovery_anchor_query(question)
    hits: list[PassageHit] = []
    if anchor_query:
        try:
            hits = store.search_fts(
                anchor_query,
                limit=DISCOVERY_CANDIDATE_LIMIT,
                project=project,
                page_type=filters.type,
                tags=list(filters.tags),
                mode="strict",
            )
            if not hits:
                hits = store.search_fts(
                    anchor_query,
                    limit=DISCOVERY_CANDIDATE_LIMIT,
                    project=project,
                    page_type=filters.type,
                    tags=list(filters.tags),
                    mode="relaxed",
                )
        except RetrievalIndexError:
            hits = []

    page_paths: list[str] = []
    seen_paths: set[str] = set()
    for hit in hits:
        if hit.page_path in seen_paths:
            continue
        if eligible(hit, metadata, scope=scope) and matches_request(hit, metadata, project=project, filters=filters):
            seen_paths.add(hit.page_path)
            page_paths.append(hit.page_path)

    raw_anchor_terms = set(tokens(anchor_query))
    wildcard = bool(extract_namespace_wildcards(question))
    eligible_pages: list[tuple[str, str, str]] = []
    pages = snapshot.pages if snapshot is not None else store.page_candidates()
    for index, page in enumerate(pages):
        if cancellation is not None:
            cancellation.checkpoint_batch(index, every=16, stage="discovery")
        path = str(page["path"])
        title = str(page.get("title") or "")
        if not snapshot_page_eligible(page, metadata, scope=scope, project=project, filters=filters):
            continue
        eligible_pages.append((path, title, f"{title} {path}".casefold()))

    term_frequency = {
        term: sum(term in title_path for _path, _title, title_path in eligible_pages)
        for term in raw_anchor_terms
    }
    common_term_limit = max(1, len(eligible_pages) // 2)
    anchor_terms = {term for term in raw_anchor_terms if term_frequency.get(term, 0) <= common_term_limit}
    focus_terms = {
        term
        for term in _discovery_focus_terms(question)
        if term not in raw_anchor_terms or term_frequency.get(term, 0) <= common_term_limit
    }

    def contains_term(value: str, term: str) -> bool:
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", value, re.I) is not None

    metadata_candidates: list[tuple[int, str]] = []
    clean_catalog_paths: set[str] = set()
    catalog_paths: set[str] = set()
    for path, title, title_path in eligible_pages:
        overlap = sum(contains_term(title_path, term) for term in anchor_terms)
        title_focus = sum(contains_term(title, term) for term in focus_terms)
        path_focus = sum(contains_term(path, term) for term in focus_terms)
        title_catalog = bool(re.search(r"catalog|directory|overview|modules?|types?|reference|list|目录|模块|类型|清单|列表", title, re.I))
        path_catalog = bool(re.search(r"catalog|directory|overview|modules?|types?|reference|list|目录|模块|类型|清单|列表", path, re.I))
        plural_catalog = bool(re.search(r"\b(?:modules|types|catalogs|directories|lists|listings)\b", title, re.I))
        noisy_title = bool(re.search(r"sample|reference|difference|tutorial|code|entry points?|api|file", title, re.I))
        explicit_catalog_title = bool(re.search(r"catalog|directory|overview|index|contents|list(?:ing)?|目录|模块|类型|清单|列表", title, re.I))
        strong_catalog = plural_catalog or explicit_catalog_title
        if strong_catalog:
            catalog_paths.add(path)
            if not noisy_title:
                clean_catalog_paths.add(path)
        likely_catalog = title_catalog or path_catalog
        if overlap or title_focus or (wildcard and likely_catalog):
            score = (
                overlap
                + title_focus * 2
                + path_focus
                + (3 if title_catalog else 0)
                + (1 if path_catalog else 0)
                + (2 if plural_catalog else 0)
                - (5 if noisy_title else 0)
            )
            metadata_candidates.append((score, path))

    qualified_names_by_path: dict[str, set[str]] = {}
    if wildcard and metadata_candidates:
        probe_paths = [
            path
            for _score, path in sorted(metadata_candidates, key=lambda item: (-item[0], item[1]))[:DISCOVERY_CANDIDATE_LIMIT]
        ]
        for hit in store.passages_for_pages(probe_paths, limit_per_page=PASSAGE_PROBE_LIMIT):
            qualified_names_by_path.setdefault(hit.page_path, set()).update(_wildcard_evidence_names(question, hit.text))
        max_qualified_names = max((len(names) for names in qualified_names_by_path.values()), default=0)
        minimum_qualified_names = max(2, max_qualified_names // 10)
        qualified_paths = {
            path for path, names in qualified_names_by_path.items() if len(names) >= minimum_qualified_names
        }
        if qualified_paths:
            metadata_candidates = [item for item in metadata_candidates if item[1] in qualified_paths]

    sorted_metadata_candidates = sorted(metadata_candidates, key=lambda item: (-item[0], item[1]))
    if wildcard and qualified_names_by_path:
        metadata_paths = [
            path
            for _score, path in sorted(
                sorted_metadata_candidates,
                key=lambda item: (-len(qualified_names_by_path.get(item[1], ())), -item[0], item[1]),
            )
        ][:DISCOVERY_SOURCE_PAGE_LIMIT]
    elif focus_terms and sorted_metadata_candidates:
        catalog_candidates = [item for item in sorted_metadata_candidates if item[1] in clean_catalog_paths] or [
            item for item in sorted_metadata_candidates if item[1] in catalog_paths
        ]
        ranking_candidates = catalog_candidates or sorted_metadata_candidates
        best_score = ranking_candidates[0][0]
        metadata_paths = [path for score, path in ranking_candidates if score >= best_score - 1][:DISCOVERY_SOURCE_PAGE_LIMIT]
    else:
        metadata_paths = [path for _score, path in sorted_metadata_candidates]
    if metadata_paths:
        page_paths = list(dict.fromkeys(metadata_paths))[:DISCOVERY_PAGE_LIMIT]
    else:
        page_paths = page_paths[:DISCOVERY_PAGE_LIMIT]

    passages = store.passages_for_pages(page_paths, limit_per_page=PASSAGE_SCAN_LIMIT)
    return [candidate_item(hit, score=hit.score) for hit in passages]


def _compact_discovery_aliases(aliases: Sequence[str]) -> list[str]:
    """Deduplicate boundary variants by normalizing separators and case."""

    seen: set[str] = set()
    compact: list[str] = []
    for alias in aliases:
        if not alias:
            continue
        key = re.sub(r"[/\s_-]+", "/", alias).casefold().strip("/")
        if key not in seen:
            seen.add(key)
            compact.append(alias)
    return compact


def _discover_enumerated_entities(
    selected: Sequence[Mapping[str, Any]],
    context_items: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Discover entities only from same-page structured evidence."""

    ordered_items: list[Mapping[str, Any]] = []
    seen_passages: set[tuple[str, str]] = set()
    for item in [*selected, *context_items]:
        hit = item.get("hit")
        if not isinstance(hit, PassageHit):
            continue
        key = (hit.page_path, hit.passage_id)
        if key in seen_passages:
            continue
        seen_passages.add(key)
        ordered_items.append(item)

    by_page: dict[str, dict[str, Any]] = {}
    for item in ordered_items:
        hit = item["hit"]
        page = by_page.setdefault(
            hit.page_path,
            {"groups": {}, "entities": {}, "order": len(by_page), "next_order": 0},
        )
        lines = hit.text.splitlines()
        for line_index, line in enumerate(lines):
            if (
                line.lstrip().startswith("|")
                and line_index + 1 < len(lines)
                and re.fullmatch(
                    r"\s*\|?\s*:?-{2,}:?(?:\s*\|\s*:?-{2,}:?)+\s*\|?\s*",
                    lines[line_index + 1],
                )
            ):
                continue
            for group, label, kind in _discovery_fragments(line):
                for canonical, aliases, identifier in _discovery_candidate_for_fragment(label):
                    entities = page["entities"]
                    entity = entities.get(canonical)
                    if entity is None:
                        entity = {
                            "canonical_id": canonical,
                            "aliases": _compact_discovery_aliases(aliases),
                            "evidence": {
                                "path": hit.page_path,
                                "passage_id": hit.passage_id,
                                "heading": heading(hit),
                                "excerpt": line.strip()[:360],
                            },
                            "evidence_fragments": [],
                            "_identifier": identifier,
                            "_order": page["next_order"],
                        }
                        entities[canonical] = entity
                        page["next_order"] += 1
                    else:
                        entity["evidence_fragments"].append(
                            {
                                "path": hit.page_path,
                                "passage_id": hit.passage_id,
                                "heading": heading(hit),
                                "excerpt": line.strip()[:360],
                            }
                        )
                    page["groups"].setdefault((group, kind), set()).add(canonical)

    valid_pages: list[tuple[str, dict[str, Any], set[str]]] = []
    for path, page in by_page.items():
        table_groups = [
            members
            for (group, kind), members in page["groups"].items()
            if kind == "table" and len(members) >= 2
        ]
        valid_groups = table_groups or [members for members in page["groups"].values() if len(members) >= 2]
        valid_entities = set().union(*valid_groups) if valid_groups else set()
        if len(valid_entities) >= 2:
            valid_pages.append((path, page, valid_entities))

    candidates: list[dict[str, Any]] = []
    for _path, page, valid_entities in valid_pages:
        for canonical, entity in page["entities"].items():
            if canonical in valid_entities:
                candidates.append(entity)
    candidates.sort(
        key=lambda item: (
            next(index for index, entry in enumerate(ordered_items) if entry["hit"].page_path == item["evidence"]["path"]),
            item.get("_order", 0),
        )
    )
    unique_candidates: list[dict[str, Any]] = []
    by_canonical: dict[str, dict[str, Any]] = {}
    for candidate in candidates:
        existing = by_canonical.get(candidate["canonical_id"])
        if existing is None:
            by_canonical[candidate["canonical_id"]] = candidate
            unique_candidates.append(candidate)
            continue
        existing["aliases"] = list(dict.fromkeys([*existing["aliases"], *candidate["aliases"]]))
        existing["evidence_fragments"].extend(candidate["evidence_fragments"])
    discovery = {
        "triggered": bool(unique_candidates),
        "enumeration_evidence": bool(unique_candidates),
        "candidate_entities": [],
        "source_pages": [path for path, _page, _entities in valid_pages],
    }
    return _public_discovery_projection(discovery, unique_candidates), unique_candidates if len(unique_candidates) >= 2 else []


def _public_discovery_entity(entity: Mapping[str, Any]) -> dict[str, Any]:
    evidence_value = entity.get("evidence")
    evidence = evidence_value if isinstance(evidence_value, Mapping) else {}
    aliases_value = entity.get("aliases", ())
    aliases = (
        [str(alias) for alias in aliases_value]
        if isinstance(aliases_value, Sequence) and not isinstance(aliases_value, (str, bytes))
        else []
    )
    return {
        "canonical_id": str(entity.get("canonical_id", "")),
        "aliases": aliases,
        "evidence": {
            "path": str(evidence.get("path", "")),
            "heading": str(evidence.get("heading", "")),
        },
    }


def _discovery_json_size(value: Mapping[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _public_discovery_projection(
    discovery: Mapping[str, Any],
    entities: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Project discovery with deterministic count and byte bounds."""

    public_entities = [_public_discovery_entity(entity) for entity in entities]
    source_pages_value = discovery.get("source_pages", ())
    source_pages: list[str] = []
    if isinstance(source_pages_value, Sequence) and not isinstance(source_pages_value, (str, bytes)):
        source_pages = list(dict.fromkeys(str(path) for path in source_pages_value))
    base = {
        key: value
        for key, value in discovery.items()
        if key not in {"candidate_entities", "source_pages", "total_count", "returned_count", "truncated"}
    }
    base["source_pages"] = source_pages
    base["total_count"] = len(public_entities)
    base["returned_count"] = 0
    base["truncated"] = True

    def payload(candidates: list[dict[str, Any]], truncated: bool) -> dict[str, Any]:
        return {
            **base,
            "candidate_entities": candidates,
            "returned_count": len(candidates),
            "truncated": truncated,
        }

    while _discovery_json_size(payload([], True)) > PUBLIC_DISCOVERY_BYTE_LIMIT and source_pages:
        source_pages.pop()
        base["source_pages"] = source_pages
    if _discovery_json_size(payload([], True)) > PUBLIC_DISCOVERY_BYTE_LIMIT:
        base = {
            key: value
            for key, value in base.items()
            if key in {"triggered", "enumeration_evidence", "requested", "reason", "total_count", "source_pages", "returned_count", "truncated"}
        }

    selected: list[dict[str, Any]] = []
    truncated = False
    for candidate in public_entities:
        if len(selected) >= PUBLIC_DISCOVERY_CANDIDATE_LIMIT:
            truncated = True
            break
        if _discovery_json_size(payload([*selected, candidate], True)) > PUBLIC_DISCOVERY_BYTE_LIMIT:
            truncated = True
            break
        selected.append(candidate)
    if public_entities and not selected:
        first = public_entities[0]
        minimal = {
            "canonical_id": first["canonical_id"],
            "evidence": first["evidence"],
        }
        if _discovery_json_size(payload([minimal], True)) <= PUBLIC_DISCOVERY_BYTE_LIMIT:
            selected.append(minimal)
        else:
            selected.append({"canonical_id": first["canonical_id"]})
        truncated = True
    if len(selected) < len(public_entities):
        truncated = True
    result = payload(selected, truncated)
    if not truncated:
        result["truncated"] = False
    return result


def _constrain_discovery_entities(
    question: str,
    discovery: dict[str, Any],
    entities: Sequence[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Apply query-level entity constraints before discovery can batch."""

    wildcards = extract_namespace_wildcards(question)
    if not wildcards:
        return _public_discovery_projection(discovery, entities), list(entities)
    prefixes = [item.prefix_segments for item in wildcards]
    constrained: list[dict[str, Any]] = []
    for entity in entities:
        identifier = entity.get("_identifier")
        if not isinstance(identifier, QualifiedIdentifier):
            continue
        name = "/".join(identifier.name_segments).casefold()
        if name in {"a", "na", "none", "null"}:
            continue
        if any(identifier.prefix_segments[: len(prefix)] == prefix for prefix in prefixes):
            constrained.append(entity)
    source_paths = {str(entity["evidence"]["path"]) for entity in constrained}
    constrained_discovery = {
        **discovery,
        "triggered": len(constrained) >= 2,
        "enumeration_evidence": len(constrained) >= 2,
        "source_pages": [path for path in discovery.get("source_pages", ()) if path in source_paths],
    }
    return _public_discovery_projection(
        constrained_discovery,
        constrained,
    ), constrained if len(constrained) >= 2 else []


def discover_catalog(
    *,
    store: RetrievalIndexStore,
    snapshot: QueryCorpusSnapshot,
    question: str,
    selected: Sequence[Mapping[str, Any]],
    context_items: Sequence[Mapping[str, Any]],
    effective_scope: str,
    project: str | None,
    filters: QueryFilters,
    cancellation: QueryCancellationContext,
    raw_store: RetrievalIndexStore | None = None,
    raw_snapshot: QueryCorpusSnapshot | None = None,
    raw_available: Literal["fresh", "stale", "missing"] = "missing",
    raw_provider: Callable[[], tuple[RetrievalIndexStore, QueryCorpusSnapshot] | None] | None = None,
) -> DiscoveryResult:
    """Run discovery against supplied snapshots and return a frozen result.

    ``store`` and ``raw_store`` are read-only adapters owned by the context;
    the owner never creates or memoizes them.  All page metadata scans use the
    supplied snapshots, so this function cannot observe a drifting store view.
    """

    discovery_requested = discovery_requested_for(question)
    discovery_source_items: list[dict[str, Any]] = []
    if not _discovery_suppressed(question):
        discovery, discovery_entities = _discover_enumerated_entities(selected, context_items)
        discovery, discovery_entities = _constrain_discovery_entities(question, discovery, discovery_entities)
    else:
        discovery = {
            "triggered": False,
            "enumeration_evidence": False,
            "candidate_entities": [],
            "source_pages": [],
        }
        discovery_entities = []
    if not discovery_entities and discovery_requested:
        metadata = metadata_from_snapshot(snapshot, cancellation)
        discovery_source_items = _discovery_source_items(
            store,
            metadata,
            question,
            scope=effective_scope,
            project=project,
            filters=filters,
            snapshot=snapshot,
            cancellation=cancellation,
        )
        discovery, discovery_entities = _discover_enumerated_entities(
            selected,
            [*context_items, *discovery_source_items],
        )
        discovery, discovery_entities = _constrain_discovery_entities(question, discovery, discovery_entities)
    if (
        not discovery_entities
        and discovery_requested
        and effective_scope in {"knowledge", "all"}
        and raw_available != "fresh"
        and raw_provider is not None
    ):
        raw_inputs = raw_provider()
        if raw_inputs is not None:
            raw_store, raw_snapshot = raw_inputs
            raw_available = "fresh"
    if not discovery_entities and discovery_requested and effective_scope in {"knowledge", "all"} and raw_available == "fresh" and raw_store is not None and raw_snapshot is not None:
        raw_metadata = metadata_from_snapshot(raw_snapshot, cancellation)
        raw_discovery_items = _discovery_source_items(
            raw_store,
            raw_metadata,
            question,
            scope="raw",
            project=project,
            filters=filters,
            snapshot=raw_snapshot,
            cancellation=cancellation,
        )
        discovery, discovery_entities = _discover_enumerated_entities(
            selected,
            [*context_items, *discovery_source_items, *raw_discovery_items],
        )
        discovery, discovery_entities = _constrain_discovery_entities(question, discovery, discovery_entities)
    discovery["requested"] = discovery_requested
    if not discovery_entities:
        discovery["reason"] = "structured_enumeration_evidence_insufficient"
    public_entities = discovery_entities
    if not public_entities:
        existing_candidates = discovery.get("candidate_entities", ())
        if isinstance(existing_candidates, Sequence) and not isinstance(existing_candidates, (str, bytes)):
            public_entities = [
                candidate
                for candidate in existing_candidates
                if isinstance(candidate, Mapping)
            ]
    discovery = _public_discovery_projection(discovery, public_entities)
    return DiscoveryResult(
        discovery=_freeze(discovery),
        entities=tuple(_freeze(entity) for entity in discovery_entities),
        source_items=tuple(_freeze(item) for item in discovery_source_items),
        requested=discovery_requested,
    )


def discovery_requested_for(question: str) -> bool:
    """Compatibility-named public predicate used by the execution context."""

    return discovery_requested(question)


__all__ = [
    "DISCOVERY_CANDIDATE_LIMIT",
    "DISCOVERY_PAGE_LIMIT",
    "DISCOVERY_SOURCE_PAGE_LIMIT",
    "PUBLIC_DISCOVERY_BYTE_LIMIT",
    "PUBLIC_DISCOVERY_CANDIDATE_LIMIT",
    "DiscoveryResult",
    "PASSAGE_PROBE_LIMIT",
    "PASSAGE_SCAN_LIMIT",
    "discover_catalog",
    "discovery_requested",
    "discovery_requested_for",
    "eligible",
    "heading",
    "matches_request",
    "metadata_from_snapshot",
]
