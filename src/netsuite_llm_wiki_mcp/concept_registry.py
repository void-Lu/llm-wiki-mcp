"""Canonical concept registry rebuilt from concept Markdown frontmatter."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from netsuite_llm_wiki_mcp.wiki_io import read_markdown_page
from netsuite_llm_wiki_mcp.wikilinks import wikilink_targets


def normalize_alias(value: str) -> str:
    return re.sub(r"[\W_]+", "", value.casefold(), flags=re.UNICODE)


def canonical_id(title: str) -> str:
    normalized = normalize_alias(title) or "concept"
    return f"concept_{normalized[:48]}_{hashlib.sha256(title.casefold().encode()).hexdigest()[:10]}"


@dataclass(frozen=True)
class ConceptRecord:
    concept_id: str
    path: str
    title: str
    aliases: tuple[str, ...]
    domain: str
    sources: tuple[str, ...]
    lifecycle: str


class ConceptRegistry:
    def __init__(self, vault_root: str | Path):
        self.root = Path(vault_root).expanduser().resolve()
        self.records: list[ConceptRecord] = []
        self.rebuild()

    def rebuild(self) -> list[ConceptRecord]:
        records: list[ConceptRecord] = []
        for path in sorted((self.root / "wiki" / "concepts").rglob("*.md")) if (self.root / "wiki" / "concepts").exists() else []:
            page = read_markdown_page(path, self.root)
            fm = page.frontmatter
            if fm.get("type") != "concept" or fm.get("redirect_to"):
                continue
            aliases = tuple(str(item) for item in fm.get("aliases", []) if isinstance(item, str))
            sources = tuple(str(item) for item in fm.get("sources", []) if isinstance(item, str))
            records.append(ConceptRecord(str(fm.get("concept_id") or canonical_id(page.title)), page.relative_path.as_posix(), page.title, aliases, str(fm.get("domain") or "general"), sources, str(fm.get("lifecycle") or "active")))
        self.records = records
        return records

    def resolve(self, candidate: str) -> dict[str, Any]:
        normalized = normalize_alias(candidate)
        exact = [record for record in self.records if candidate == record.concept_id]
        aliases = [record for record in self.records if normalized in {normalize_alias(record.title), *(normalize_alias(alias) for alias in record.aliases)}]
        matches = exact or aliases
        if matches:
            return {"action": "existing", "record": matches[0], "matches": matches, "evidence": {"canonical_or_alias": [record.path for record in matches], "fts": [], "wikilinks": [], "embedding": self._embedding_status()}}
        fuzzy = [record for record in self.records if normalized and normalized in normalize_alias(record.title)]
        fts = self._fts_candidates(candidate)
        links = self._wikilink_candidates(candidate)
        paths = {record.path for record in fuzzy} | set(fts) | set(links)
        matches = [record for record in self.records if record.path in paths]
        return {"action": "candidate", "matches": matches, "evidence": {"canonical_or_alias": [], "fts": fts, "wikilinks": links, "embedding": self._embedding_status()}}

    def _fts_candidates(self, candidate: str) -> list[str]:
        """Use an already-built FTS projection only; concept resolution never builds it."""
        try:
            from netsuite_llm_wiki_mcp.retrieval_index import RetrievalIndexStore

            store = RetrievalIndexStore(self.root)
            return sorted({hit.page_path for hit in store.search_fts(candidate, limit=8, page_type="concept")})
        except Exception:
            return []

    def _wikilink_candidates(self, candidate: str) -> list[str]:
        normalized = normalize_alias(candidate)
        if not normalized:
            return []
        record_by_target: dict[str, str] = {}
        for record in self.records:
            record_by_target[normalize_alias(record.title)] = record.path
            record_by_target[normalize_alias(Path(record.path).stem)] = record.path
            for alias in record.aliases:
                record_by_target[normalize_alias(alias)] = record.path
        found: set[str] = set()
        for page in (self.root / "wiki").rglob("*.md") if (self.root / "wiki").exists() else []:
            text = page.read_text(encoding="utf-8", errors="ignore")
            for target in wikilink_targets(text):
                key = normalize_alias(Path(target.split("#", 1)[0]).stem)
                if key == normalized and key in record_by_target:
                    found.add(record_by_target[key])
        return sorted(found)

    @staticmethod
    def _embedding_status() -> dict[str, object]:
        return {"available": False, "reason": "core_profile_does_not_load_embedding_model"}

    @staticmethod
    def promotion(sources: Iterable[str], *, query_frequency: int = 0, has_chat_source: bool = False, threshold: int = 2) -> str:
        if has_chat_source:
            return "review_required"
        if len(set(sources)) >= threshold or query_frequency >= threshold:
            return "promote"
        return "review_required"

    def redirect_frontmatter(self, old_id: str, replacement: ConceptRecord) -> dict[str, Any]:
        return {"type": "concept", "concept_id": old_id, "redirect_to": replacement.path, "replaced_by": replacement.concept_id, "aliases": [old_id], "generated": True, "maintenance": "auto", "lifecycle": "superseded"}
