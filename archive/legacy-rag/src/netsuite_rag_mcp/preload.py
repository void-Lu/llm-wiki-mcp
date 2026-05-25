from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from netsuite_rag_mcp.config import load_config
from netsuite_rag_mcp.runtime_config import resolve_runtime_config
from netsuite_rag_mcp.vector_store import SentenceTransformerEmbedder


def preload_embedding_model(vault_root: str | Path | None = None) -> dict[str, Any]:
    runtime = resolve_runtime_config(vault_root_arg=vault_root)
    config = load_config(runtime.vault_root, runtime_config=runtime)
    config.embedding_cache_path.mkdir(parents=True, exist_ok=True)
    SentenceTransformerEmbedder(config.embedding_model, cache_folder=config.embedding_cache_path)
    return {
        "model": config.embedding_model,
        "cache_path": str(config.embedding_cache_path),
        "status": "ready",
    }


def main() -> None:
    print(json.dumps(preload_embedding_model(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()