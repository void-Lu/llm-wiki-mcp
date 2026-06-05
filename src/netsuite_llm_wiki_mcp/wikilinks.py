from __future__ import annotations

import re
from collections.abc import Iterator

_CODE_OR_WIKILINK_RE = re.compile(
    r"```.*?```"
    r"|`[^`]+`"
    r"|\[\[([^\]]+)\]\]",
    re.DOTALL,
)
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def format_wikilink(target: str, label: str | None = None, *, in_table: bool = False) -> str:
    """Format an Obsidian wikilink, escaping alias separators inside tables."""
    if label is None or label == "":
        return f"[[{target}]]"
    separator = r"\|" if in_table else "|"
    return f"[[{target}{separator}{label}]]"


def normalize_wikilink_targets(text: str) -> str:
    """Lowercase wikilink targets outside code spans and escape table aliases."""
    def _replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        if inner is None:
            return match.group(0)
        target, label, _separator_escaped = split_wikilink_inner(inner)
        if not target:
            return match.group(0)
        return format_wikilink(target.lower(), label, in_table=is_markdown_table_row_at(text, match.start()))

    return _CODE_OR_WIKILINK_RE.sub(_replace, text)


def wikilink_targets(text: str) -> Iterator[str]:
    """Yield target portions from Obsidian wikilinks, including table-escaped aliases."""
    for match in _WIKILINK_RE.finditer(text):
        target, _label, _separator_escaped = split_wikilink_inner(match.group(1))
        if target:
            yield target


def table_wikilink_alias_pipe_lines(text: str) -> Iterator[int]:
    """Yield 1-based table row lines containing unescaped wikilink alias pipes."""
    for line_number, line in enumerate(text.splitlines(), 1):
        if not is_markdown_table_row(line):
            continue
        for match in _WIKILINK_RE.finditer(line):
            if contains_unescaped_pipe(match.group(1)):
                yield line_number
                break


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