from pathlib import Path

from netsuite_rag_mcp.preload import preload_embedding_model
from netsuite_rag_mcp.runtime_config import resolve_runtime_config


def test_preload_embedding_model_uses_configured_model_and_cache(monkeypatch, tmp_path: Path):
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "rag").mkdir()
    (vault / "rag" / "sources.yaml").write_text(
        "\n".join(
            [
                "vault_root: .",
                "embedding_model: BAAI/bge-m3",
                "embedding_cache_path: .models",
            ]
        ),
        encoding="utf-8",
    )
    calls = {}

    class StubEmbedder:
        def __init__(self, model_name: str, cache_folder: Path):
            calls["model_name"] = model_name
            calls["cache_folder"] = cache_folder

    monkeypatch.setattr("netsuite_rag_mcp.preload.SentenceTransformerEmbedder", StubEmbedder)

    result = preload_embedding_model(vault)
    runtime = resolve_runtime_config(vault_root_arg=vault)

    assert calls["model_name"] == "BAAI/bge-m3"
    assert calls["cache_folder"] == runtime.embedding_cache_path
    assert result == {
        "model": "BAAI/bge-m3",
        "cache_path": str(runtime.embedding_cache_path),
        "status": "ready",
    }
    # vault-local: .models is inside vault
    assert runtime.embedding_cache_path == vault / ".models"
