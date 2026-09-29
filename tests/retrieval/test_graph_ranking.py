"""Graph ranking behaviour on the graph_v1 fixture vault."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from retrieval.query_pipeline import run_query_v2
from retrieval.retrieval_index import RetrievalIndexStore

FIXTURE_VAULT = Path(__file__).resolve().parents[1] / "fixtures" / "retrieval" / "graph_v1" / "vault"


@pytest.fixture()
def vault(tmp_path: Path) -> Path:
    root = tmp_path / "vault"
    shutil.copytree(FIXTURE_VAULT, root)
    store = RetrievalIndexStore(root)
    store.build(store.iter_vault_pages())
    return root


def _graph_scores(payload: dict) -> dict[str, float]:
    return {item["path"]: item["scores"]["graph"] for item in [*payload["results"], *payload["additional_results"]]}


def test_relaxed_recovery_receives_graph_evidence(vault: Path) -> None:
    question = "which team is on call for the accounting journal exporter"
    on = run_query_v2(vault, question, retrieval_mode="lexical", debug=True)
    off = run_query_v2(vault, question, retrieval_mode="lexical", debug=True, graph_expansion=False)

    assert on["pipeline"]["lexical"]["mode"] == "relaxed"
    assert on["pipeline"]["counters"]["fts_hits"] == 0
    assert any(score > 0 for score in _graph_scores(on).values())
    assert all(score == 0 for score in _graph_scores(off).values())


def test_relaxed_graph_expansion_respects_request_filters(vault: Path) -> None:
    question = "which team is on call for the accounting journal exporter"
    unfiltered = run_query_v2(vault, question, retrieval_mode="lexical", debug=True)
    payload = run_query_v2(vault, question, retrieval_mode="lexical", project="ledger", debug=True)

    # Without a filter the graph reaches other projects' pages ...
    assert any(path.startswith("wiki/projects/harbor/") and score > 0 for path, score in _graph_scores(unfiltered).items())
    # ... but the project filter bounds relaxed graph expansion as well.
    assert payload["pipeline"]["lexical"]["mode"] == "relaxed"
    scores = _graph_scores(payload)
    assert any(score > 0 for score in scores.values())
    assert not any(path.startswith("wiki/projects/") and not path.startswith("wiki/projects/ledger/") for path in scores)
