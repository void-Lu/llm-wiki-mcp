from retrieval.lexical_analyzer import (
    extract_namespace_wildcards,
    extract_qualified_identifiers,
    fts_query,
    has_qualified_identifier,
    identifier_phrase_fts_query,
    identifier_phrase_tokens,
    identifier_phrases,
    parse_qualified_identifier,
    parse_namespace_wildcard,
    qualified_code_fts_query,
)


def test_qualified_code_fts_query_keeps_parsed_segments_and_verbatim_phrase() -> None:
    query = qualified_code_fts_query("N/record")
    assert '"n" AND "record"' in query
    assert '"n record"' in query


def test_qualified_code_fts_query_preserves_list_record_as_a_phrase() -> None:
    query = qualified_code_fts_query("List/Record")
    assert '"list" AND "record"' in query
    assert '"list record"' in query


def test_qualified_code_fts_query_or_combines_multiple_matches() -> None:
    query = qualified_code_fts_query("N/record and N/search")
    assert '"n" AND "record"' in query
    assert '"n record"' in query
    assert '"n search"' in query


def test_identifier_phrase_fts_query_strict_and_over_latin_tokens() -> None:
    query = identifier_phrase_fts_query("配置netsuite系统内ai connector的mcp工具的完整步骤是什么")
    assert query == '"netsuite" AND "ai" AND "connector" AND "mcp"'


def test_identifier_phrase_fts_query_requires_a_multiword_english_run() -> None:
    assert identifier_phrase_fts_query("自定义 List/Record 字段关联 Subsidiary 标准记录 typeId 是多少") == ""
    assert identifier_phrase_fts_query("N/record模块有哪些方法？") == ""
    assert identifier_phrase_fts_query("纯中文问题") == ""


def test_identifier_phrase_tokens_drop_single_letter_namespace_segments() -> None:
    assert identifier_phrase_tokens("N/record module methods") == ["record", "module", "methods"]


def test_identifier_phrases_extract_only_space_separated_runs() -> None:
    assert identifier_phrases("配置netsuite系统内ai connector的mcp工具的完整步骤") == ["ai connector"]
    assert identifier_phrases("自定义 List/Record 字段关联 Subsidiary 标准记录 typeId 是多少") == []


def test_qualified_identifier_aliases_share_one_canonical_id() -> None:
    forms = ("N/auth", "N auth", "Nauth", "N-auth", "N_auth", "n/AUTH")
    parsed = [parse_qualified_identifier(form) for form in forms]

    assert {item.canonical_id for item in parsed} == {"n/auth"}
    assert {"N/auth", "N auth", "Nauth", "N-auth", "N_auth"} <= set(parsed[0].aliases)


def test_qualified_identifier_parser_is_not_net_suite_specific() -> None:
    assert parse_qualified_identifier("Foo/Bar").canonical_id == "foo/bar"
    assert parse_qualified_identifier("FooBar").canonical_id == "foo/bar"
    query = qualified_code_fts_query("Foo/Bar and Baz/qux")
    assert '"foo" AND "bar"' in query
    assert '"baz" AND "qux"' in query


def test_namespace_wildcard_is_discovery_intent_and_not_a_prefix_fts_term() -> None:
    wildcard = parse_namespace_wildcard("N / *")

    assert wildcard.canonical_prefix == "n"
    assert extract_namespace_wildcards("n/* and Foo / *") == [
        parse_namespace_wildcard("n/*"),
        parse_namespace_wildcard("Foo / *"),
    ]
    assert '"n"' not in fts_query("N/* modules")


def test_automatic_compact_extraction_rejects_net_suite_but_keeps_nauth() -> None:
    assert [item.canonical_id for item in extract_qualified_identifiers("NetSuite") ] == []
    assert qualified_code_fts_query("NetSuite") == ""
    assert has_qualified_identifier("NetSuite") is False
    assert [item.canonical_id for item in extract_qualified_identifiers("Nauth") ] == ["n/auth"]
    assert '"n" AND "auth"' in qualified_code_fts_query("nauth")
    assert extract_qualified_identifiers("Suitelet script") == []


def test_qualified_identifier_supports_nested_slash_segments() -> None:
    parsed = parse_qualified_identifier("N/crypto/certificate")

    assert parsed.canonical_id == "n/crypto/certificate"
    assert parsed.name_segments == ("crypto", "certificate")
    assert [item.canonical_id for item in extract_qualified_identifiers("N/ui/serverWidget") ] == [
        "n/ui/serverwidget"
    ]
    query = qualified_code_fts_query("N/crypto/certificate")
    assert '"n" AND "crypto" AND "certificate"' in query
    assert '"n crypto certificate"' in query
