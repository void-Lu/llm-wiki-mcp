from __future__ import annotations

from wiki.wikilinks import (
    iter_wikilinks,
    normalize_wikilink_targets,
    table_wikilink_alias_pipe_lines,
    wikilink_targets,
)


def test_iter_wikilinks_ignores_code_contexts_and_escaped_links() -> None:
    markdown = r"""
正文 [[Real/Page|真实页面]]

`[[inline-code]]`

``code with ` and [[double-inline]]``

```python
value = "[[backtick-fence]]"
```

~~~~
[[tilde-fence]]
~~~~

> 引用 [[Quoted]]
- 列表 [[Listed]]
\[[escaped]]
"""

    tokens = list(iter_wikilinks(markdown))

    assert [(token.target, token.alias) for token in tokens] == [
        ("Real/Page", "真实页面"),
        ("Quoted", None),
        ("Listed", None),
    ]
    assert list(wikilink_targets(markdown)) == ["Real/Page", "Quoted", "Listed"]
    assert all(markdown[token.start : token.end].startswith("[[") for token in tokens)


def test_fence_close_must_not_be_shorter_than_opening_fence() -> None:
    markdown = """
````text
[[inside]]
```
[[still-inside]]
````
[[outside]]
"""

    assert list(wikilink_targets(markdown)) == ["outside"]


def test_unclosed_inline_backticks_remain_literal_markdown() -> None:
    markdown = "Unclosed ` delimiter keeps [[ordinary-link]] visible"

    assert list(wikilink_targets(markdown)) == ["ordinary-link"]


def test_prose_line_bare_fence_compatibility_does_not_break_inline_spans() -> None:
    markdown = (
        "[[before]] and ```\n"
        "[[inside-fence]]\n"
        "```\n"
        "Before ```[[inside-inline]]``` after [[outside]]."
    )

    assert list(wikilink_targets(markdown)) == ["before", "outside"]


def test_normalize_uses_same_tokens_and_preserves_table_alias_behavior() -> None:
    markdown = (
        "| Link | Example |\n"
        "|---|---|\n"
        "| [[Mixed/Target|Label]] | `[[INLINE/TARGET]]` |\n"
        "\n"
        "```\n[[FENCED/TARGET]]\n```\n"
        "\\[[ESCAPED/TARGET]] and [[PLAIN/TARGET]]\n"
    )

    normalized = normalize_wikilink_targets(markdown)

    assert "[[mixed/target\\|Label]]" in normalized
    assert "`[[INLINE/TARGET]]`" in normalized
    assert "[[FENCED/TARGET]]" in normalized
    assert "\\[[ESCAPED/TARGET]]" in normalized
    assert "[[plain/target]]" in normalized
    assert list(table_wikilink_alias_pipe_lines(normalized)) == []


def test_table_alias_lint_ignores_code_contexts() -> None:
    markdown = """
| Link | Description |
|---|---|
| `[[inline|alias]]` | example |

```
| [[fenced|alias]] | example |
```

| [[real|alias]] | real |
"""

    assert list(table_wikilink_alias_pipe_lines(markdown)) == [10]
