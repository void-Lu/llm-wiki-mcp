from netsuite_llm_wiki_mcp.content_redaction import REDACTION_POLICY_VERSION, redact_for_index


def test_index_redaction_is_deterministic_and_keeps_auditable_hashes() -> None:
    source = "token=sk-abcdefghijklmno and email=person@example.com"
    first = redact_for_index(source)
    second = redact_for_index(source)

    assert first == second
    assert first.policy_version == REDACTION_POLICY_VERSION
    assert "sk-abcdefghijklmno" not in first.text
    assert "person@example.com" not in first.text
    assert first.original_hash != first.redacted_hash
