"""评测 runtime snapshot、统一 query interface 与真实 adapter seam 测试。"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

import retrieval.retrieval_eval as retrieval_eval
from retrieval.retrieval_eval import (
    EngineQueryAdapter,
    EvaluationQueryRequest,
    EvaluationRuntimeSnapshot,
    McpEntryAdapter,
    McpQueryAdapter,
    RetrievalEvalCase,
    RetrievalEvalError,
)
from runtime.runtime_config import QualityGateSettings


def _request(*, retrieval_mode: str = "lexical") -> EvaluationQueryRequest:
    case = RetrievalEvalCase("case", "query", (), {}, False, "en", (), "")
    return EvaluationQueryRequest(
        case=case,
        top_k=10,
        include_context_pack=False,
        retrieval_mode=retrieval_mode,  # type: ignore[arg-type]
        vector_config=None,
        query_version="v2",
        scope="knowledge",
    )


def test_engine_adapter_owns_query_envelope_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def execute(_root: Path, _request: EvaluationQueryRequest, **kwargs: object) -> dict[str, object]:
        captured.update(kwargs)
        return {"ok": True, "results": [], "pipeline": {}, "budget": {}}

    monkeypatch.setattr(
        retrieval_eval,
        "_execute_engine_query",
        execute,
    )

    runtime = EvaluationRuntimeSnapshot.lexical_only(
        Path("vault"),
        quality_gate=QualityGateSettings(mode="shadow", artifact_path="reports/calibration.json"),
    )
    result = EngineQueryAdapter(runtime).run(_request())

    assert result == {"ok": True, "results": [], "pipeline": {}, "budget": {}}
    assert captured["quality_gate"] == runtime.settings.quality_gate

    monkeypatch.setattr(retrieval_eval, "_execute_engine_query", lambda _root, _request, **_kwargs: {"results": {}})
    with pytest.raises(RetrievalEvalError, match="results must be a list"):
        EngineQueryAdapter(runtime).run(_request())


def test_mcp_adapter_rejects_malformed_envelope_before_service_consumes_it(tmp_path: Path) -> None:
    @contextmanager
    def snapshot(_resolution: object):
        yield

    adapter = McpEntryAdapter(
        resolve=lambda _root: object(),
        snapshot=snapshot,
        query=lambda **_kwargs: {"ok": True, "results": [], "pipeline": "malformed"},
    )
    runtime = EvaluationRuntimeSnapshot.__new__(EvaluationRuntimeSnapshot)
    object.__setattr__(runtime, "root", tmp_path)
    object.__setattr__(runtime, "logical_name", "test")
    object.__setattr__(runtime, "settings", object())
    object.__setattr__(runtime, "adapter", adapter)
    object.__setattr__(runtime, "mcp_resolution", object())

    with pytest.raises(RetrievalEvalError, match="pipeline must be an object"):
        McpQueryAdapter(runtime).run(_request())

    with pytest.raises(RetrievalEvalError, match="lexical-only"):
        McpQueryAdapter(runtime).run(_request(retrieval_mode="hybrid"))
