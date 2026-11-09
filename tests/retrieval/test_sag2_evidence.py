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
    assert (
        judge.score(
            "suiteql 工时",
            "SuiteQL 工时 examples",
            title="SuiteQL",
            page_path="wiki/concepts/suiteql.md",
        )
        >= 0.0
    )


def test_title_bonus_outranks_body_only_weak_overlap() -> None:
    judge = HeuristicEvidenceJudge()
    q = "email capture"
    # Body mentions only one query token weakly; title carries both.
    titled = judge.score(
        q,
        "capture settings live here.",
        title="Email Capture Plug-in",
        page_path="wiki/concepts/email-capture.md",
    )
    body_only = judge.score(
        q,
        "capture settings live here.",
        title="Unrelated Page",
        page_path="wiki/concepts/other.md",
    )
    assert 0.0 <= body_only < titled <= 1.0


def test_cjk_span_bonus_outranks_latin_only_body() -> None:
    judge = HeuristicEvidenceJudge()
    q = "如何配置工时填写"
    with_cjk = judge.score(
        q,
        "本文说明如何配置工时填写流程。",
        title="工时",
        page_path="wiki/concepts/timebill.md",
    )
    latinish = judge.score(
        q,
        "configuration documentation without the chinese span.",
        title="Config",
        page_path="wiki/concepts/config.md",
    )
    assert 0.0 <= latinish < with_cjk <= 1.0


def test_identifier_bonus_rewards_snake_and_camel_hits() -> None:
    judge = HeuristicEvidenceJudge()
    q = "call saveTimeBill then run_query_v2"
    hit = judge.score(
        q,
        "Handlers invoke saveTimeBill before indexing.",
        title="run_query_v2 notes",
        page_path="wiki/concepts/run_query_v2.md",
    )
    miss = judge.score(
        q,
        "Handlers invoke the nightly job before indexing.",
        title="Nightly job",
        page_path="wiki/concepts/nightly.md",
    )
    assert 0.0 <= miss < hit <= 1.0


def test_bonuses_still_clamp_to_unit_interval() -> None:
    judge = HeuristicEvidenceJudge()
    q = "EmailCapture saveTimeBill 工时填写 run_query_v2"
    score = judge.score(
        q,
        "EmailCapture saveTimeBill 工时填写 run_query_v2 " * 3,
        title="EmailCapture 工时填写 run_query_v2",
        page_path="wiki/concepts/EmailCapture-run_query_v2.md",
        context=["EmailCapture saveTimeBill 工时填写"],
    )
    assert 0.0 <= score <= 1.0


def test_local_llm_fallback_forwards_title_path() -> None:
    fallback = HeuristicEvidenceJudge()
    judge = LocalLlmEvidenceJudge(fallback=fallback, scorer=None)
    q = "email capture"
    direct = fallback.score(
        q, "capture settings", title="Email Capture", page_path="wiki/concepts/email-capture.md"
    )
    wrapped = judge.score(
        q, "capture settings", title="Email Capture", page_path="wiki/concepts/email-capture.md"
    )
    assert wrapped == direct
