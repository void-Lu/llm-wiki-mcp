# tests/retrieval/test_sag2_evidence.py
from __future__ import annotations

import socket
from typing import Sequence

import pytest

from retrieval.sag2_evidence import HeuristicEvidenceJudge, LocalLlmEvidenceJudge


def test_heuristic_score_in_unit_interval_and_ranks_overlap() -> None:
    judge = HeuristicEvidenceJudge()
    q = "email capture plug-in implementation"
    high = judge.score(q, "Developing an Email Capture Plug-in Implementation requires ...")
    low = judge.score(q, "Unrelated NetSuite preference page about themes.")
    assert 0.0 <= low <= high <= 1.0
    assert high > low


def test_heuristic_uses_context_bonus_without_exceeding_one() -> None:
    judge = HeuristicEvidenceJudge()
    q = "workflow action script"
    base = judge.score(q, "A workflow action script runs on demand.")
    with_ctx = judge.score(
        q,
        "A workflow action script runs on demand.",
        context=["workflow action script entry points"],
    )
    assert 0.0 <= base <= with_ctx <= 1.0


def test_local_llm_wrapper_falls_back_when_scorer_missing() -> None:
    fallback = HeuristicEvidenceJudge()
    judge = LocalLlmEvidenceJudge(fallback=fallback, scorer=None)
    score = judge.score("auth login", "N/auth login methods")
    assert 0.0 <= score <= 1.0


def test_local_llm_wrapper_falls_back_when_scorer_raises() -> None:
    fallback = HeuristicEvidenceJudge()

    def boom(question: str, passage_text: str, context: Sequence[str]) -> float:
        raise RuntimeError("local model unavailable")

    judge = LocalLlmEvidenceJudge(fallback=fallback, scorer=boom)
    assert judge.score("x", "x related text") == fallback.score("x", "x related text")


def test_judge_path_makes_no_network_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def deny(*_a, **_k):  # noqa: ANN001
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "create_connection", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    judge = HeuristicEvidenceJudge()
    assert judge.score("suiteql", "SuiteQL query examples") >= 0.0
