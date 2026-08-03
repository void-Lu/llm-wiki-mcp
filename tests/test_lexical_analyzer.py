from netsuite_llm_wiki_mcp.lexical_analyzer import identifier_phrase_fts_query, identifier_phrase_tokens, identifier_phrases, module_qualified, qualified_code_fts_query


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
