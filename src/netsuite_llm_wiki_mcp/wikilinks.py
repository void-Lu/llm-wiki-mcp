from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Mapping

_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
_FENCE_OPEN_RE = re.compile(r"^ {0,3}(?P<fence>`{3,}|~{3,})(?P<info>[^\r\n]*)$")


@dataclass(frozen=True)
class LinkToken:
    """A wikilink found outside Markdown code contexts."""

    target: str
    alias: str | None
    start: int
    end: int


def format_wikilink(target: str, label: str | None = None, *, in_table: bool = False) -> str:
    """Format an Obsidian wikilink, escaping alias separators inside tables."""
    if label is None or label == "":
        return f"[[{target}]]"
    separator = r"\|" if in_table else "|"
    return f"[[{target}{separator}{label}]]"


def normalize_wikilink_targets(text: str) -> str:
    """Lowercase wikilink targets outside code spans and escape table aliases."""
    return _replace_wikilink_targets(text, lambda target: target.lower())


def normalize_wikilinks(text: str, mapping: Mapping[str, str]) -> str:
    """Rewrite wikilink targets using an explicit mapping outside code contexts."""
    return _replace_wikilink_targets(text, lambda target: mapping.get(target, target))


def iter_wikilinks(text: str) -> Iterator[LinkToken]:
    """Yield wikilink tokens outside fenced blocks, inline code, and escapes."""
    excluded = _code_ranges(text)
    excluded_index = 0
    for match in _WIKILINK_RE.finditer(text):
        while excluded_index < len(excluded) and excluded[excluded_index][1] <= match.start():
            excluded_index += 1
        if (
            excluded_index < len(excluded)
            and excluded[excluded_index][0] < match.end()
            and match.start() < excluded[excluded_index][1]
        ):
            continue
        if _is_escaped(text, match.start()):
            continue
        target, alias, _separator_escaped = split_wikilink_inner(match.group(1))
        if target:
            yield LinkToken(target=target, alias=alias, start=match.start(), end=match.end())


def wikilink_targets(text: str) -> Iterator[str]:
    """Yield target portions from Obsidian wikilinks, including table-escaped aliases."""
    for token in iter_wikilinks(text):
        yield token.target


def table_wikilink_alias_pipe_lines(text: str) -> Iterator[int]:
    """Yield 1-based table row lines containing unescaped wikilink alias pipes."""
    reported: set[int] = set()
    for token in iter_wikilinks(text):
        line_number = text.count("\n", 0, token.start) + 1
        if line_number in reported or not is_markdown_table_row_at(text, token.start):
            continue
        inner = text[token.start + 2 : token.end - 2]
        if contains_unescaped_pipe(inner):
            reported.add(line_number)
            yield line_number


def is_markdown_table_row_at(text: str, position: int) -> bool:
    line_start = text.rfind("\n", 0, position) + 1
    line_end = text.find("\n", position)
    if line_end == -1:
        line_end = len(text)
    return is_markdown_table_row(text[line_start:line_end])


def is_markdown_table_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def split_wikilink_inner(inner: str) -> tuple[str, str | None, bool]:
    index = 0
    while index < len(inner):
        char = inner[index]
        if char == "\\" and index + 1 < len(inner) and inner[index + 1] == "|":
            return inner[:index], inner[index + 2 :], True
        if char == "|":
            return inner[:index], inner[index + 1 :], False
        index += 1
    return inner, None, False


def contains_unescaped_pipe(text: str) -> bool:
    index = 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            index += 2
            continue
        if text[index] == "|":
            return True
        index += 1
    return False


def _replace_wikilink_targets(text: str, replace_target: Callable[[str], str]) -> str:
    parts: list[str] = []
    cursor = 0
    for token in iter_wikilinks(text):
        parts.append(text[cursor : token.start])
        target = replace_target(token.target)
        parts.append(
            format_wikilink(
                target,
                token.alias,
                in_table=is_markdown_table_row_at(text, token.start),
            )
        )
        cursor = token.end
    parts.append(text[cursor:])
    return "".join(parts)


def _code_ranges(text: str) -> list[tuple[int, int]]:
    fenced = _fenced_code_ranges(text)
    inline: list[tuple[int, int]] = []
    cursor = 0
    for start, end in fenced:
        inline.extend(_inline_code_ranges(text, cursor, start))
        cursor = end
    inline.extend(_inline_code_ranges(text, cursor, len(text)))
    return sorted([*fenced, *inline])


def _fenced_code_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    opening_start: int | None = None
    fence_char = ""
    fence_length = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        line_without_ending = line.rstrip("\r\n")
        if opening_start is None:
            opening = _fence_open(line_without_ending)
            if opening is not None:
                fence, info, column = opening
                if fence[0] != "`" or "`" not in info:
                    opening_start = offset + column
                    fence_char = fence[0]
                    fence_length = len(fence)
        elif _is_fence_close(line_without_ending, fence_char, fence_length):
            ranges.append((opening_start, offset + len(line)))
            opening_start = None
            fence_char = ""
            fence_length = 0
        offset += len(line)
    if opening_start is not None:
        ranges.append((opening_start, len(text)))
    return ranges


def _fence_open(line: str) -> tuple[str, str, int] | None:
    match = _FENCE_OPEN_RE.match(line)
    if match is not None:
        return match.group("fence"), match.group("info"), match.start("fence")

    # Preserve the historical normalizer contract for a bare fence placed at
    # the end of a prose line. A same-line triple-backtick span still falls
    # through to the inline-code scanner.
    compatibility = re.search(r"(?<!`)(?P<fence>`{3,}|~{3,})[ \t]*$", line)
    if compatibility is None or not line[: compatibility.start()].strip():
        return None
    return compatibility.group("fence"), "", compatibility.start("fence")


def _is_fence_close(line: str, fence_char: str, fence_length: int) -> bool:
    stripped = line.lstrip(" ")
    indentation = len(line) - len(stripped)
    if indentation > 3 or not stripped.startswith(fence_char * fence_length):
        return False
    run_length = 0
    while run_length < len(stripped) and stripped[run_length] == fence_char:
        run_length += 1
    return run_length >= fence_length and stripped[run_length:].strip(" \t") == ""


def _inline_code_ranges(text: str, start: int, end: int) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    cursor = start
    while cursor < end:
        if text[cursor] != "`" or _is_escaped(text, cursor):
            cursor += 1
            continue
        run_end = cursor + 1
        while run_end < end and text[run_end] == "`":
            run_end += 1
        delimiter_length = run_end - cursor
        closing = _find_inline_close(text, run_end, end, delimiter_length)
        if closing is None:
            cursor = run_end
            continue
        closing_end = closing + delimiter_length
        ranges.append((cursor, closing_end))
        cursor = closing_end
    return ranges


def _find_inline_close(text: str, start: int, end: int, delimiter_length: int) -> int | None:
    cursor = start
    while cursor < end:
        if text[cursor] != "`" or _is_escaped(text, cursor):
            cursor += 1
            continue
        run_end = cursor + 1
        while run_end < end and text[run_end] == "`":
            run_end += 1
        if run_end - cursor == delimiter_length:
            return cursor
        cursor = run_end
    return None


def _is_escaped(text: str, position: int) -> bool:
    backslashes = 0
    cursor = position - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1
