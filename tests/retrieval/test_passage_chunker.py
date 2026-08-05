from retrieval.passage_chunker import chunk_markdown


def test_chunker_is_deterministic_and_keeps_atomic_markdown_blocks() -> None:
    document = """# Main

intro text

## API

```python
token = 'sk-abcdefghijklmno'
```

| field | value |
| --- | --- |
| custbody_id | invoice |

- first
- second
"""
    first = chunk_markdown("wiki/concepts/example.md", document, target_tokens=8, max_tokens=80)
    second = chunk_markdown("wiki/concepts/example.md", document, target_tokens=8, max_tokens=80)

    assert first == second
    assert all(chunk.text.strip() for chunk in first)
    assert any("```python" in chunk.text and "custbody_id" not in chunk.text for chunk in first)
    assert any("custbody_id" in chunk.text for chunk in first)
    assert all(chunk.passage_id and chunk.token_count for chunk in first)


def test_chunker_handles_duplicate_and_empty_headings() -> None:
    chunks = chunk_markdown("wiki/concepts/a.md", "## Same\n\nA\n\n## Same\n\nB\n\n##\n\nC\n\n" + ("中文 English custbody_field " * 500), max_tokens=120)

    assert [chunk.ordinal for chunk in chunks] == list(range(len(chunks)))
    assert len({chunk.passage_id for chunk in chunks}) == len(chunks)
    assert max(chunk.token_count for chunk in chunks) <= 120
