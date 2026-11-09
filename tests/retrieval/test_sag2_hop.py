from __future__ import annotations

from retrieval.retrieval_index import PassageHit
from retrieval.sag2_evidence import HeuristicEvidenceJudge
from retrieval.sag2_hop import Sag2HopConfig, Sag2HopController
from retrieval.sag2_terms import TermSelector


def _hit(pid: str, text: str, score: float = 1.0) -> PassageHit:
    return PassageHit(
        passage_id=pid,
        page_path=f"wiki/{pid}.md",
        title=pid,
        heading_path=(),
        text=text,
        score=score,
        corpus="active",
        authority="formal",
        source_kind="wiki",
        ordinal=0,
    )


class FakeSearcher:
    def __init__(self, mapping: dict[str, list[PassageHit]]) -> None:
        self.mapping = mapping
        self.calls: list[str] = []

    def search(self, query: str, *, limit: int = 20) -> list[PassageHit]:
        self.calls.append(query)
        hits = self.mapping.get(query.casefold(), self.mapping.get(query, []))
        return hits[:limit]


class RecordingTermSelector(TermSelector):
    """Records seed texts passed to select() so tests can assert no re-expand."""

    def __init__(self) -> None:
        self.seed_texts: list[str] = []

    def select(self, seed_text: str, **kwargs):  # noqa: ANN003
        self.seed_texts.append(seed_text)
        return super().select(seed_text, **kwargs)


def test_controller_stops_at_max_hops_and_dedupes() -> None:
    # Seed includes novel "handbook" so TermSelector yields an expansion term
    # (question unigrams are skipped; adjacent bigrams alone may not match FakeSearcher).
    seed = _hit("seed", "alpha widget protocol handbook alpha widget handbook")
    expanded = _hit("exp1", "widget protocol handbook")
    searcher = FakeSearcher(
        {
            "handbook": [expanded, seed],
            "widget protocol": [expanded],
            "protocol handbook": [expanded],
            "alpha widget": [expanded],
        }
    )
    controller = Sag2HopController(
        HeuristicEvidenceJudge(),
        TermSelector(),
        searcher,
        Sag2HopConfig(initial_pool_size=10, seeds_per_hop=1, terms_per_seed=2, max_hops=2),
    )
    result = controller.run("alpha widget protocol", [seed])
    assert result.hops_run <= 2
    ids = [h.passage_id for h in result.candidates]
    assert ids.count("exp1") == 1
    assert result.stop_reason in {"max_hops", "no_new_candidates", "no_expansion_terms", "judge_budget"}


def test_controller_stops_when_no_expansion_terms() -> None:
    seed = _hit("only", "the and or")  # stopwords only after filters
    searcher = FakeSearcher({})
    controller = Sag2HopController(
        HeuristicEvidenceJudge(),
        TermSelector(),
        searcher,
        Sag2HopConfig(max_hops=2, seeds_per_hop=3, terms_per_seed=5),
    )
    result = controller.run("the and or", [seed])
    assert searcher.calls == []
    assert result.hops_run == 0 or result.stop_reason == "no_expansion_terms"


def test_controller_does_not_reexpand_same_seed() -> None:
    seed = _hit("seed", "beta gateway service beta gateway")
    neighbor = _hit("n1", "gateway service runbook")
    searcher = FakeSearcher(
        {
            "gateway": [neighbor],
            "service": [neighbor],
            "beta": [neighbor],
            "gateway service": [neighbor],
            "service beta": [neighbor],
            "beta gateway": [neighbor],
            "service runbook": [_hit("n2", "runbook appendix")],
            "runbook": [_hit("n2", "runbook appendix")],
        }
    )
    terms = RecordingTermSelector()
    controller = Sag2HopController(
        HeuristicEvidenceJudge(),
        terms,
        searcher,
        Sag2HopConfig(max_hops=2, seeds_per_hop=1, terms_per_seed=3),
    )
    result = controller.run("beta gateway", [seed])
    assert result.hops_run <= 2
    assert "seed" in {h.passage_id for h in result.candidates}
    # Same seed text must not be selected twice across hops.
    assert terms.seed_texts.count(seed.text) <= 1
