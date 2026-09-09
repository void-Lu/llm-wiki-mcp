"""Heading-aware, deterministic Markdown passage chunking."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Iterable

from retrieval.token_units import count_passage_tokens, passage_token_units

CHUNK_SCHEMA_VERSION = 2
DEFAULT_TARGET_TOKENS = 450
DEFAULT_MIN_TOKENS = 100
DEFAULT_MAX_TOKENS = 600
DEFAULT_OVERLAP_TOKENS = 64
_HEADING = re.compile(r"^(#{1,6})(?:\s+(.*?))?\s*#*\s*$")


@dataclass(frozen=True)
class PassageChunk:
    passage_id: str
    page_path: str
    heading_path: tuple[str, ...]
    heading_anchor: str
    ordinal: int
    text: str
    token_count: int
    content_hash: str
    chunk_schema_version: int = CHUNK_SCHEMA_VERSION


def estimate_passage_tokens(text: str) -> int:
    """Count passage/chunk units used by chunk limits and index rows."""

    return count_passage_tokens(text)


def chunk_markdown(page_path: str, body: str, *, target_tokens: int = DEFAULT_TARGET_TOKENS, max_tokens: int = DEFAULT_MAX_TOKENS, overlap_tokens: int = DEFAULT_OVERLAP_TOKENS) -> list[PassageChunk]:
    """Split Markdown by heading and block boundaries without empty passages."""

    blocks = list(_blocks(body))
    heading_stack: list[str] = []
    grouped: list[tuple[tuple[str, ...], str]] = []
    current: list[str] = []
    current_heading: tuple[str, ...] = ()
    for heading, block in blocks:
        if heading is not None:
            if current:
                grouped.extend(_split_group(current_heading, "\n\n".join(current), target_tokens, max_tokens, overlap_tokens))
                current = []
            level, title = heading
            heading_stack = heading_stack[: level - 1] + [title]
            current_heading = tuple(heading_stack)
        elif block.strip():
            current.append(block.strip())
    if current:
        grouped.extend(_split_group(current_heading, "\n\n".join(current), target_tokens, max_tokens, overlap_tokens))
    result: list[PassageChunk] = []
    for ordinal, (heading_path, text) in enumerate(grouped):
        clean = text.strip()
        if not clean:
            continue
        content_hash = hashlib.sha256(clean.encode("utf-8")).hexdigest()
        anchor = _anchor(heading_path)
        identity = f"v{CHUNK_SCHEMA_VERSION}\0{page_path}\0{anchor}\0{ordinal}\0{content_hash}"
        result.append(PassageChunk(hashlib.sha256(identity.encode("utf-8")).hexdigest(), page_path, heading_path, anchor, ordinal, clean, estimate_passage_tokens(clean), content_hash))
    return result


def _blocks(body: str) -> Iterable[tuple[tuple[int, str] | None, str]]:
    lines = body.splitlines()
    index = 0
    paragraph: list[str] = []

    def flush() -> Iterable[tuple[tuple[int, str] | None, str]]:
        nonlocal paragraph
        if paragraph:
            yield None, "\n".join(paragraph)
            paragraph = []

    while index < len(lines):
        line = lines[index]
        matched = _HEADING.match(line)
        if matched:
            yield from flush()
            yield (len(matched.group(1)), (matched.group(2) or "").strip()), ""
            index += 1
            continue
        if line.startswith(("```", "~~~")):
            yield from flush()
            fence = line[:3]
            block = [line]
            index += 1
            while index < len(lines):
                block.append(lines[index])
                if lines[index].startswith(fence):
                    index += 1
                    break
                index += 1
            yield None, "\n".join(block)
            continue
        if line.lstrip().startswith(("- ", "* ", "+ ")) or re.match(r"\s*\d+[.)]\s+", line):
            yield from flush()
            block = [line]
            index += 1
            while index < len(lines) and (lines[index].lstrip().startswith(("- ", "* ", "+ ")) or re.match(r"\s*\d+[.)]\s+", lines[index])):
                block.append(lines[index]); index += 1
            yield None, "\n".join(block)
            continue
        if "|" in line and index + 1 < len(lines) and re.match(r"^\s*\|?\s*:?-+", lines[index + 1]):
            yield from flush()
            block = [line, lines[index + 1]]; index += 2
            while index < len(lines) and "|" in lines[index] and lines[index].strip():
                block.append(lines[index]); index += 1
            yield None, "\n".join(block)
            continue
        if not line.strip():
            yield from flush(); index += 1; continue
        paragraph.append(line); index += 1
    yield from flush()


def _split_group(heading: tuple[str, ...], text: str, target: int, maximum: int, overlap: int) -> list[tuple[tuple[str, ...], str]]:
    units = [part.strip() for part in text.split("\n\n") if part.strip()]
    result: list[tuple[tuple[str, ...], str]] = []
    current: list[str] = []
    for unit in units:
        unit_tokens = passage_token_units(unit)
        if len(unit_tokens) > maximum:
            if current:
                result.append((heading, "\n\n".join(current))); current = []
            for offset in range(0, len(unit_tokens), max(1, maximum - overlap)):
                words = unit_tokens[offset : offset + maximum]
                if words:
                    result.append((heading, " ".join(words)))
            continue
        proposal = "\n\n".join([*current, unit])
        if current and estimate_passage_tokens(proposal) > target:
            result.append((heading, "\n\n".join(current)))
            tail = passage_token_units(current[-1])[-overlap:]
            current = [(" ".join(tail) + "\n\n" + unit).strip()] if tail else [unit]
        else:
            current.append(unit)
    if current:
        result.append((heading, "\n\n".join(current)))
    return result


def _anchor(headings: tuple[str, ...]) -> str:
    value = "-".join(headings).casefold()
    return re.sub(r"[^\w一-鿿-]+", "-", value).strip("-") or "document"
