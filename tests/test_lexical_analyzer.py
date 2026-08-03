from netsuite_llm_wiki_mcp.lexical_analyzer import module_qualified, qualified_code_fts_query


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


def test_module_qualified_requires_single_letter_namespace_prefixes() -> None:
    assert module_qualified("N/record模块有哪些方法？") is True
    assert module_qualified("N/record module methods") is True
    assert module_qualified("List/Record字段") is False
    assert module_qualified("自定义 List/Record 字段关联 Subsidiary") is False
    assert module_qualified("没有斜杠标识") is False
