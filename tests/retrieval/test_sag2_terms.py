from __future__ import annotations

from retrieval.sag2_terms import TermSelector


def test_term_selector_excludes_question_stopwords_and_respects_limit() -> None:
    selector = TermSelector()
    terms = selector.select(
        "The workflow action script authenticates via token passport",
        question="How does the workflow action script work?",
        evidence_score=0.8,
        document_frequency={"workflow": 10, "action": 12, "script": 20, "authenticates": 2, "token": 5, "passport": 1},
        corpus_size=100,
        limit=3,
    )
    values = [t.term for t in terms]
    assert "how" not in values and "does" not in values and "the" not in values
    assert len(terms) <= 3
    assert terms == sorted(terms, key=lambda t: (-t.weight, t.term))


def test_term_selector_drops_df_zero_and_keeps_multiword_when_present() -> None:
    selector = TermSelector()
    terms = selector.select(
        "Email Capture Plug-in Implementation details appear here.",
        question="email capture",
        evidence_score=1.0,
        document_frequency={"email": 5, "capture": 4, "plug": 3, "implementation": 2, "details": 0},
        corpus_size=50,
        limit=20,
    )
    values = {t.term for t in terms}
    assert "details" not in values  # df=0
    assert "email" in values or "email capture" in values


def test_term_selector_weights_rise_with_evidence_and_rarity() -> None:
    selector = TermSelector()
    common = selector.select(
        "suiteql example suiteql example",
        question="suiteql",
        evidence_score=0.2,
        document_frequency={"suiteql": 80, "example": 80},
        corpus_size=100,
        limit=5,
    )
    rare = selector.select(
        "suiteql example suiteql example",
        question="suiteql",
        evidence_score=0.9,
        document_frequency={"suiteql": 2, "example": 2},
        corpus_size=100,
        limit=5,
    )
    assert rare[0].weight > common[0].weight
