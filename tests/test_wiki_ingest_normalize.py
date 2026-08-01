from __future__ import annotations

from netsuite_llm_wiki_mcp.wikilinks import normalize_wikilink_targets


def test_basic_lowercase():
    assert normalize_wikilink_targets("[[User-Event-Script]]") == "[[user-event-script]]"


def test_preserves_alias():
    assert normalize_wikilink_targets("[[RESTlet|REST API]]") == "[[restlet|REST API]]"


def test_path_link():
    assert normalize_wikilink_targets("[[path/To/Target]]") == "[[path/to/target]]"


def test_skips_fenced_code():
    text = "```\n[[Upper]]\n```"
    assert normalize_wikilink_targets(text) == text


def test_skips_inline_code():
    text = "`[[Upper]]`"
    assert normalize_wikilink_targets(text) == text


def test_mixed():
    text = "See [[Suitelet]] and `[[Suitelet]]` and ```\n[[Suitelet]]\n```"
    result = normalize_wikilink_targets(text)
    assert "[[suitelet]]" in result
    assert "`[[Suitelet]]`" in result
    assert "```\n[[Suitelet]]\n```" in result


def test_already_lowercase_unchanged():
    text = "[[user-event-script]]"
    assert normalize_wikilink_targets(text) == text


def test_multiple_links_in_one_line():
    text = "See [[Alpha]] and [[Beta|Display]] for details."
    result = normalize_wikilink_targets(text)
    assert "[[alpha]]" in result
    assert "[[beta|Display]]" in result

def test_escapes_alias_separator_inside_markdown_table_rows():
    text = "| Example | Description |\n|---|---|\n| [[RESTlet|REST API]] | details |"

    result = normalize_wikilink_targets(text)

    assert "| [[restlet\\|REST API]] | details |" in result


def test_preserves_escaped_alias_separator_inside_markdown_table_rows():
    text = "| Example | Description |\n|---|---|\n| [[RESTlet\\|REST API]] | details |"

    result = normalize_wikilink_targets(text)

    assert "| [[restlet\\|REST API]] | details |" in result
