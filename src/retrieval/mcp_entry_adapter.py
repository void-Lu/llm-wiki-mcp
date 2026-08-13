"""MCP entry-point adapter seam for retrieval evaluation."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class McpEntryAdapter:
    """The minimal MCP entry-point surface required by retrieval evaluation."""

    resolve: Callable[[str], Any]
    snapshot: Callable[[Any], AbstractContextManager[None]]
    query: Callable[..., Any] | None = None


def default_mcp_entry_adapter() -> McpEntryAdapter:
    """Build the real adapter while keeping the server import at this seam."""

    import app.server as server_module

    return McpEntryAdapter(
        resolve=lambda root: server_module.resolve_tool_vault(vault_root=root),
        snapshot=server_module.tool_runtime_snapshot,
        query=server_module.wiki_query,
    )


__all__ = ["McpEntryAdapter", "default_mcp_entry_adapter"]
