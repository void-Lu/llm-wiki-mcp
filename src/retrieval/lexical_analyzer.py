"""Deterministic lexical normalization shared by FTS and retrieval callers."""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_LATIN_OR_CODE = re.compile(r"[a-z0-9_]+")
_CJK_RUN = re.compile(r"[一-鿿]+")
_QUALIFIED_IDENTIFIER = re.compile(
    r"(?<![a-z0-9_])(?P<prefix>[a-z][a-z0-9.]*)"
    r"(?P<separator>/)"
    r"(?P<name>[a-z][a-z0-9_-]*(?:/[a-z][a-z0-9_-]*)*)(?![a-z0-9_/])",
    re.I,
)
_QUALIFIED_BOUNDARY_IDENTIFIER = re.compile(
    r"(?<![a-z0-9_])(?P<prefix>[a-z][a-z0-9.]*)"
    r"(?P<separator>[-_]|[ \t]+)"
    r"(?P<name>[a-z][a-z0-9_-]*)(?![a-z0-9_/])",
    re.I,
)
_QUALIFIED_IDENTIFIER_PATTERNS = (_QUALIFIED_IDENTIFIER, _QUALIFIED_BOUNDARY_IDENTIFIER)
_COMPACT_QUALIFIED_IDENTIFIER = re.compile(
    # Automatic compact extraction is intentionally narrower than the public
    # parser.  A one-letter namespace followed by a lower-case entity is the
    # only unambiguous compact form; otherwise ordinary CamelCase (notably
    # ``NetSuite``) would become a false qualified-code query.
    r"(?<![A-Za-z0-9_])(?P<value>[A-Z][a-z][a-z0-9_]*)(?![A-Za-z0-9_])"
)
_NAMESPACE_WILDCARD = re.compile(
    r"(?<![a-z0-9_])(?P<prefix>[a-z][a-z0-9.]*)\s*/\s*\*(?![a-z0-9_])",
    re.I,
)
_MULTIWORD_RUN = re.compile(r"[a-z0-9_]+(?:\s+[a-z0-9_]+)+", re.I)
_STOPWORDS = frozenset({"a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "in", "is", "of", "on", "or", "the", "to", "with"})
_RAW_PREFIX_MIN_LENGTH = 3


@dataclass(frozen=True)
class QualifiedIdentifier:
    """Query-time representation of a namespace-qualified entity.

    The raw document and its index remain untouched.  All boundary spellings
    are derived here so callers can search ``N/auth`` and ``Nauth`` as the same
    entity while preserving separators inside the entity name itself.
    """

    prefix_segments: tuple[str, ...]
    name_segments: tuple[str, ...]
    canonical_id: str
    aliases: tuple[str, ...]
    raw_forms: tuple[str, ...]


@dataclass(frozen=True)
class NamespaceWildcard:
    """Query-time discovery intent for every entity in one namespace."""

    prefix_segments: tuple[str, ...]
    canonical_prefix: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class LexicalQueryPlan:
    """Language-separated lexical signals used by recovery/discovery."""

    latin_terms: tuple[str, ...]
    cjk_terms: tuple[str, ...]
    qualified_identifiers: tuple[QualifiedIdentifier, ...]
    namespace_wildcards: tuple[NamespaceWildcard, ...]

    @property
    def discovery_intent(self) -> bool:
        return bool(self.namespace_wildcards)


def _namespace_wildcard_from_parts(prefix: str, raw: str) -> NamespaceWildcard:
    segments = tuple(part.casefold() for part in prefix.split(".") if part)
    if not segments:
        raise ValueError("namespace wildcard requires a prefix")
    canonical = ".".join(segments)
    forms = (
        f"{prefix}/*",
        f"{prefix} / *",
        f"{prefix.casefold()}/*",
        canonical,
    )
    return NamespaceWildcard(segments, canonical, tuple(dict.fromkeys((raw.strip(), *forms))))


def parse_namespace_wildcard(value: str) -> NamespaceWildcard:
    """Parse ``prefix/*`` without turning the prefix into an FTS token."""

    raw = value.strip().strip("`[](){}<>").rstrip(".,:;!?\u3002\uff1f\uff01")
    match = _NAMESPACE_WILDCARD.fullmatch(raw)
    if not match:
        raise ValueError(f"invalid namespace wildcard: {value!r}")
    return _namespace_wildcard_from_parts(match.group("prefix"), raw)


def extract_namespace_wildcards(text: str) -> list[NamespaceWildcard]:
    """Extract wildcard discovery intents while preserving query order."""

    values: list[NamespaceWildcard] = []
    seen: set[str] = set()
    for match in _NAMESPACE_WILDCARD.finditer(text):
        wildcard = _namespace_wildcard_from_parts(match.group("prefix"), match.group(0))
        if wildcard.canonical_prefix not in seen:
            seen.add(wildcard.canonical_prefix)
            values.append(wildcard)
    return values


def is_safe_compact_qualified_identifier(value: str) -> bool:
    """Return whether a complete token can use the compact alias form.

    The first letter is deliberately namespace-agnostic.  Restricting this
    helper to ``N`` would turn a generic identifier contract into a
    SuiteScript-specific branch.  Internal CamelCase is excluded, so
    ``NetSuite`` cannot become ``net/suite``.
    """

    candidate = value.strip()
    return re.fullmatch(r"[A-Za-z][a-z][a-z0-9_]*", candidate) is not None


def plan_query(text: str) -> LexicalQueryPlan:
    """Build separated Latin/CJK and qualified/discovery query signals."""

    lowered = text.casefold()
    latin_terms = tuple(value for value in _LATIN_OR_CODE.findall(lowered) if value not in _STOPWORDS)
    cjk_terms: list[str] = []
    for run in _CJK_RUN.findall(lowered):
        cjk_terms.extend(run if len(run) == 1 else (run[index : index + 2] for index in range(len(run) - 1)))
    return LexicalQueryPlan(
        latin_terms=latin_terms,
        cjk_terms=tuple(cjk_terms),
        qualified_identifiers=tuple(extract_qualified_identifiers(text)),
        namespace_wildcards=tuple(extract_namespace_wildcards(text)),
    )


def _qualified_identifier_from_parts(prefix: str, name: str, raw: str) -> QualifiedIdentifier:
    prefix_segments = tuple(part.casefold() for part in prefix.split(".") if part)
    name_segments = tuple(part.casefold() for part in name.split("/") if part)
    if not prefix_segments or not name_segments:
        raise ValueError("qualified identifier requires a prefix and a name")
    normalized_prefix = ".".join(prefix_segments)
    normalized_name = "/".join(name_segments)
    canonical_id = f"{normalized_prefix}/{normalized_name}"
    display_prefix = ".".join(part for part in prefix.split(".") if part)
    display_name = "/".join(part for part in name.split("/") if part)
    boundary_forms = (
        f"{display_prefix}/{display_name}",
        f"{display_prefix} {display_name}",
        f"{display_prefix}{display_name}",
        f"{display_prefix}-{display_name}",
        f"{display_prefix}_{display_name}",
    )
    aliases: list[str] = []
    for value in (raw.strip(), *boundary_forms, *(item.casefold() for item in boundary_forms), canonical_id):
        if value and value not in aliases:
            aliases.append(value)
    return QualifiedIdentifier(prefix_segments, name_segments, canonical_id, tuple(aliases), tuple(aliases))


def parse_qualified_identifier(value: str) -> QualifiedIdentifier:
    """Parse one qualified identifier and normalize only its boundary.

    Explicit slash, whitespace, hyphen, and underscore forms are losslessly
    parsed.  A compact form uses a camel-case boundary when present, otherwise
    the first character is the namespace boundary (the ``Nauth`` form).
    """

    raw = value.strip().strip("`*_[](){}<>").rstrip(".,:;!?\u3002\uff1f\uff01")
    match = next(
        (
            matched
            for pattern in _QUALIFIED_IDENTIFIER_PATTERNS
            if (matched := pattern.fullmatch(raw)) is not None
        ),
        None,
    )
    if match:
        return _qualified_identifier_from_parts(match.group("prefix"), match.group("name"), raw)
    if not raw or any(character.isspace() for character in raw) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", raw):
        raise ValueError(f"invalid qualified identifier: {value!r}")
    boundary = next((match.start() for match in re.finditer(r"(?<=[a-z0-9])(?=[A-Z])", raw)), None)
    if boundary is None:
        if len(raw) < 3:
            raise ValueError(f"invalid compact qualified identifier: {value!r}")
        boundary = 1
    return _qualified_identifier_from_parts(raw[:boundary], raw[boundary:], raw)


def extract_qualified_identifiers(text: str) -> list[QualifiedIdentifier]:
    """Extract deterministic qualified identifiers from a query or passage."""

    values: list[QualifiedIdentifier] = []
    seen: set[str] = set()
    matches = sorted(
        (
            match
            for pattern in _QUALIFIED_IDENTIFIER_PATTERNS
            for match in pattern.finditer(text)
        ),
        key=lambda match: (match.start(), match.end()),
    )
    for match in matches:
        if match.group("separator").isspace() and len(match.group("prefix")) != 1:
            continue
        try:
            identifier = _qualified_identifier_from_parts(match.group("prefix"), match.group("name"), match.group(0))
        except ValueError:
            continue
        if identifier.canonical_id not in seen:
            seen.add(identifier.canonical_id)
            values.append(identifier)
    # Once an explicit boundary form exists in a passage, ordinary title-case
    # words such as ``Authentication`` must not be split as ``a/uthentication``.
    # A compact alias is accepted only when it is the complete query/fragment;
    # doing this for every CamelCase or title-case word inside prose would make
    # ``Suitelet script`` and similar natural-language questions look like
    # qualified-code queries.
    stripped = text.strip()
    if not re.search(r"[A-Za-z0-9]/[A-Za-z0-9]|[A-Za-z0-9][-_][A-Za-z0-9]", text) and _COMPACT_QUALIFIED_IDENTIFIER.fullmatch(stripped):
        try:
            identifier = parse_qualified_identifier(stripped)
        except ValueError:
            identifier = None
        if identifier is not None and identifier.canonical_id not in seen:
            seen.add(identifier.canonical_id)
            values.append(identifier)
    return values


def qualified_identifier_aliases(value: str | QualifiedIdentifier) -> tuple[str, ...]:
    """Return query-time aliases for one qualified identifier."""

    return value.aliases if isinstance(value, QualifiedIdentifier) else parse_qualified_identifier(value).aliases


def qualified_identifier_fts_query(value: str | QualifiedIdentifier) -> str:
    """Build an OR query for one identifier's boundary and compact aliases."""

    identifier = value if isinstance(value, QualifiedIdentifier) else parse_qualified_identifier(value)
    segments = [*identifier.prefix_segments, *identifier.name_segments]
    segment_query = _fts_expression(segments, "AND")
    phrase_query = f'"{" ".join(segments)}"'
    compact = "".join(segments).replace(chr(34), chr(34) * 2)
    return f'({segment_query} OR {phrase_query} OR "{compact}")'


def edit_distance(left: str, right: str) -> int:
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for index, left_char in enumerate(left, 1):
        current = [index]
        for inner, right_char in enumerate(right, 1):
            current.append(
                min(
                    previous[inner] + 1,
                    current[inner - 1] + 1,
                    previous[inner - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]


def tokens(text: str) -> list[str]:
    """Return stable English/code tokens and CJK unigrams/bigrams.

    SQLite's default tokenizer does not provide a portable Chinese analyser.
    Pre-tokenising keeps FTS behaviour stable across all supported platforms.
    """

    # Namespace wildcards are a discovery directive, not a namespace FTS
    # term.  Removing the whole expression prevents ``N/*`` from becoming a
    # broad query for the ordinary token ``n``.
    lowered = _NAMESPACE_WILDCARD.sub(" ", text.casefold())
    latin = [value for value in _LATIN_OR_CODE.findall(lowered) if value not in _STOPWORDS]
    cjk: list[str] = []
    for run in _CJK_RUN.findall(lowered):
        cjk.append(run) if len(run) == 1 else cjk.extend(run[index : index + 2] for index in range(len(run) - 1))
    return latin + cjk


def normalize(text: str | Iterable[str]) -> str:
    values = tokens(text) if isinstance(text, str) else [part for value in text for part in tokens(value)]
    return " ".join(values)


def _fts_expression(values: Iterable[str], operator: str) -> str:
    return f" {operator} ".join(f'"{value.replace(chr(34), chr(34) * 2)}"' for value in values)


def fts_query(text: str) -> str:
    """Produce a safe, precise AND query for the pre-tokenised FTS column."""

    return _fts_expression(tokens(text), "AND")


def relaxed_fts_query(text: str) -> str:
    """Produce a bounded OR recovery query after a natural-language miss."""

    return _fts_expression(tokens(text), "OR")


def raw_prefix_fts_query(text: str) -> str:
    """Produce a bounded prefix query for raw-source recovery.

    Raw fallback is allowed one extra lexical recovery pass for English and
    code tokens.  Prefix matching is intentionally anchored at the beginning
    of a token; arbitrary-substring wildcards (for example ``*term*``) would
    make common fragments recall unrelated source passages.  CJK tokens keep
    their exact pre-tokenised form and short Latin tokens are exact-only.
    """

    values = tokens(text)
    clauses: list[str] = []
    for value in values:
        escaped = value.replace(chr(34), chr(34) * 2)
        if re.fullmatch(r"[a-z0-9_]+", value) and len(value) >= _RAW_PREFIX_MIN_LENGTH:
            clauses.append(f'"{escaped}"*')
        else:
            # Short Latin tokens and CJK tokens remain exact.  Dropping a
            # short token would silently turn the bounded AND recovery pass
            # into a much broader query than the caller asked for.
            clauses.append(f'"{escaped}"')
    return " AND ".join(clauses)


def expanded_relaxed_fts_query(text: str, extra_terms: Iterable[str]) -> str:
    """OR query including query-expansion terms (abbreviations, near-miss
    variants) so a relaxed recovery can reach documents whose vocabulary
    differs from the user's phrasing."""

    values = tokens(text)
    for term in extra_terms:
        if term not in values:
            values.append(term)
    return _fts_expression(values, "OR")


def expanded_identifier_phrase_fts_query(
    text: str,
    term_variants: dict[str, list[str]],
) -> str:
    """AND query whose clauses are OR-groups per original term.

    ``identifier_phrase`` normally ANDs the strict phrase tokens, which misses
    documents that spell a concept differently (``chatbox`` vs ``ChatBot``) or
    abbreviate it (``sl`` vs ``Suitelet``).  Each original token becomes an
    OR-group of itself plus its variants, so the phrase keeps its precision
    while each component may match a variant spelling.
    """

    base = identifier_phrase_tokens(text)
    if not base:
        return ""
    clauses = []
    for token in base:
        group = [token, *[v for v in term_variants.get(token, ()) if v != token]]
        clauses.append("(" + _fts_expression(group, "OR") + ")")
    return " AND ".join(clauses)


def qualified_code_fts_query(text: str) -> str:
    """Extract qualified identifiers and union one precise query per entity."""

    identifiers = extract_qualified_identifiers(text)
    if not identifiers and is_safe_compact_qualified_identifier(text):
        try:
            identifiers = [parse_qualified_identifier(text)]
        except ValueError:
            identifiers = []
    return " OR ".join(qualified_identifier_fts_query(identifier) for identifier in identifiers)


def has_qualified_identifier(text: str) -> bool:
    """Return whether a query contains a supported qualified-id spelling."""

    if extract_qualified_identifiers(text):
        return True
    return is_safe_compact_qualified_identifier(text)


def identifier_phrases(text: str) -> list[str]:
    """Return space-separated English runs such as ``ai connector``.

    A genuine multi-word English run signals a product or feature name (for
    example ``NetSuite AI Connector``).  Slash-qualified identifiers such as
    ``List/Record`` and ``N/record`` do not form a run and keep their own
    dedicated handling.
    """

    return [match.casefold() for match in _MULTIWORD_RUN.findall(text)]


def identifier_phrase_tokens(text: str) -> list[str]:
    """Return Latin content tokens only when the text has a multi-word run.

    Without a run the Latin words are incidental vocabulary mixed into a
    Chinese question; with a run they name a precise identifier and deserve a
    strict AND lookup instead of being diluted by relaxed bigram noise.
    Single-letter tokens (for example the ``N`` in ``N/record``) are dropped
    because they carry no identifier signal of their own.
    """

    if not _MULTIWORD_RUN.search(text):
        return []
    return [value for value in tokens(text) if re.fullmatch(r"[a-z0-9_]+", value) and len(value) > 1]


def identifier_phrase_fts_query(text: str) -> str:
    """Produce a precise AND query over the identifier phrase's Latin tokens."""

    values = identifier_phrase_tokens(text)
    return _fts_expression(values, "AND") if len(values) >= 2 else ""
