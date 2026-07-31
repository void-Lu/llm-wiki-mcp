from typing import cast

from netsuite_llm_wiki_mcp.context_packer import ContextPassage, pack_context


def test_context_pack_contains_each_body_once_and_honours_limit() -> None:
    packed = pack_context([
        ContextPassage("a", "wiki/concepts/a.md", "A", "alpha beta gamma", 1.0, "formal_knowledge"),
        ContextPassage("b", "wiki/concepts/a.md", "A", "gamma delta", 0.9, "formal_knowledge"),
    ], hard_limit=4, intent="concept")
    passages = cast(list[dict[str, object]], packed["passages"])
    assert len(passages) == 1
    assert cast(str, passages[0]["content"]).count("gamma") == 1
    budget = cast(dict[str, int], packed["budget"])
    assert budget["used"] <= budget["total"]
