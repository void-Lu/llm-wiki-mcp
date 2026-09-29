"""Persisted graph edges must reproduce the former per-query graph exactly.

``_legacy_build_graph`` is a verbatim copy of the pre-edge-table
``build_graph`` (body parsing with ``Path.resolve`` on every query).  It is kept
here only as the reference for the identity checks below.
"""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from retrieval import query_pipeline
from retrieval.graph_edges import WIKILINK, PageLink, extract_page_links, resolve_wikilink
from retrieval.graph_retrieval import Graph, QueryCandidate, _as_list, _path_type, build_graph
from retrieval.query_cancellation import QueryCancellationContext
from retrieval.query_pipeline import run_query_v2
from retrieval.query_shared import QueryFilters, snapshot_page_eligible
from retrieval.query_snapshot import QueryCorpusSnapshot
from retrieval.retrieval_index import RETRIEVAL_SCHEMA_VERSION, RetrievalIndexStore
from wiki.wikilinks import wikilink_targets

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "retrieval"


# --- reference implementation (pre edge table) ------------------------------


def _legacy_build_graph(root: Path, candidates: list[QueryCandidate] | None = None, **_ignored: Any) -> Graph:
    if candidates is None:
        candidates = []
    wiki_candidates = [candidate for candidate in candidates if candidate.rel.startswith("wiki/")]
    by_rel = {candidate.rel: candidate.path for candidate in wiki_candidates}
    by_candidate = {candidate.rel: candidate for candidate in wiki_candidates}
    by_stem: dict[str, list[str]] = {}
    for rel, path in by_rel.items():
        by_stem.setdefault(path.stem.casefold(), []).append(rel)
    neighbors: dict[str, set[str]] = {rel: set() for rel in by_rel}
    sources: dict[str, set[str]] = {}
    types: dict[str, str] = {}
    for rel, path in by_rel.items():
        candidate = by_candidate[rel]
        sources[rel] = {str(item) for item in _as_list(candidate.frontmatter.get("sources"))}
        types[rel] = str(candidate.frontmatter.get("type") or _path_type(rel))
        for target in _legacy_wikilink_targets(candidate.body, path, root, by_rel, by_stem):
            neighbors[rel].add(target)
            neighbors.setdefault(target, set()).add(rel)
    return Graph(neighbors=neighbors, sources=sources, types=types)


def _legacy_wikilink_targets(body: str, path: Path, root: Path, by_rel: dict[str, Path], by_stem: dict[str, list[str]]) -> list[str]:
    targets = []
    for target in wikilink_targets(body):
        target_path = Path(target)
        candidates: list[Path] = []
        if target_path.suffix != ".md":
            target_path = target_path.with_suffix(".md")
        candidates.extend([(path.parent / target_path).resolve(), (root / "wiki" / target_path).resolve(), (root / target_path).resolve()])
        matched = ""
        for candidate in candidates:
            try:
                rel = candidate.relative_to(root).as_posix()
            except ValueError:
                continue
            if rel in by_rel:
                matched = rel
                break
        if not matched:
            stem_matches = by_stem.get(Path(target).stem.casefold(), [])
            if len(stem_matches) == 1:
                matched = stem_matches[0]
        if matched:
            targets.append(matched)
    return targets


# --- vaults ------------------------------------------------------------------


def _page(path: Path, frontmatter: dict[str, Any], body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in frontmatter.items():
        lines.append(f"{key}: {json.dumps(value, ensure_ascii=False)}")
    lines.extend(["---", "", body])
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _synthetic_vault(root: Path) -> Path:
    """Edge cases for link resolution, multi-chunk bodies and filters."""

    wiki = root / "wiki"
    for name in ("alpha", "beta"):
        raw = root / "raw" / "sources" / "doc" / name / "a.md"
        raw.parent.mkdir(parents=True, exist_ok=True)
        raw.write_text("raw\n", encoding="utf-8")
    long_body = "\n\n".join(
        f"## Section {index}\n\n" + " ".join(f"filler{index}word{word}" for word in range(60)) + f" see [[node-{index % 7}]] and [[Hub]]."
        for index in range(24)
    )
    specs: list[tuple[str, dict[str, Any], str]] = [
        ("concepts/hub.md", {"title": "Hub", "type": "concept"}, "Hub links [[node-0]], [[node-1]] and [[concepts/node-2]]."),
        ("concepts/long-page.md", {"title": "Long Page", "type": "concept", "sources": ["raw/sources/doc/alpha/a.md", "raw/sources/doc/beta/a.md"]}, long_body),
        ("concepts/code.md", {"title": "Code Page", "type": "concept"}, "Real [[node-3]].\n\n```\n[[node-4]] inside fence\n```\n\nInline `[[node-5]]` and escaped \\[[node-6]]."),
        ("concepts/paths.md", {"title": "Paths", "type": "concept", "sources": "raw/sources/doc/alpha/a.md"}, "[[../entities/node-0|Alias]] [[wiki/entities/node-1.md]] [[entities/node-2#Heading]] [[./node-3]] [[../../outside]] [[entities/node-2]] [[missing-page]] [[index]]"),
        ("concepts/ambiguous.md", {"title": "Ambiguous", "type": "concept"}, "Duplicated stem [[Twin]] and case [[NODE-4]]."),
        ("concepts/sub/twin.md", {"title": "Twin A", "type": "concept"}, "Twin A mentions [[hub]]."),
        ("entities/twin.md", {"title": "Twin B", "type": "entity"}, "Twin B mentions [[hub]]."),
        ("concepts/index.md", {"title": "Index"}, "Index lists [[hub]]."),
        # Shadows ``wiki/entities/node-2.md`` for page-relative resolution
        # from ``wiki/concepts/`` and makes the ``node-2`` stem ambiguous.
        ("concepts/entities/node-2.md", {"title": "Shadow Node", "type": "concept", "tags": ["odd"]}, "Shadow of node two, see [[hub]]."),
        ("projects/alpha/specs/alpha-spec.md", {"title": "Alpha Spec", "type": "spec", "project": "alpha", "sources": ["raw/sources/doc/alpha/a.md"], "related_objects": ["wiki/concepts/hub.md"], "applies_to": "alpha", "derived_from": ["wiki/entities/node-1.md"]}, "Alpha spec uses [[node-5]] and [[beta-spec]]."),
        ("projects/beta/specs/beta-spec.md", {"title": "Beta Spec", "type": "spec", "project": "beta", "sources": ["raw/sources/doc/beta/a.md", "raw/sources/doc/alpha/a.md"]}, "Beta spec uses [[node-6]] and [[alpha-spec]]."),
    ]
    for index in range(7):
        specs.append(
            (
                f"entities/node-{index}.md",
                {"title": f"Node {index}", "type": "entity", "tags": ["even" if index % 2 == 0 else "odd"], "sources": ["raw/sources/doc/alpha/a.md"] if index < 3 else []},
                f"Node {index} body about widget {index}. Next [[node-{(index + 1) % 7}]].",
            )
        )
    for rel, frontmatter, body in specs:
        _page(wiki / rel, frontmatter, body)
    return root


def _vault(kind: str, tmp_path: Path) -> Path:
    target = tmp_path / kind
    if kind == "graph_v1":
        shutil.copytree(FIXTURES / "graph_v1" / "vault", target)
    elif kind == "ci":
        shutil.copytree(FIXTURES / "vault", target)
    else:
        _synthetic_vault(target)
    store = RetrievalIndexStore(target)
    store.build(store.iter_vault_pages())
    return target


def _questions(kind: str) -> list[str]:
    if kind == "graph_v1":
        path = FIXTURES / "graph_v1" / "cases.jsonl"
    elif kind == "ci":
        path = FIXTURES / "fixture.jsonl"
    else:
        return ["hub", "widget 3", "long page section", "twin", "alpha spec", "beta spec node", "paths alias", "code page"]
    return [json.loads(line)["query"] for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _candidates(root: Path, store: RetrievalIndexStore, filters: QueryFilters, project: str | None) -> tuple[list[QueryCandidate], dict[str, str]]:
    snapshot = QueryCorpusSnapshot.capture(store, cancellation=QueryCancellationContext.unbounded())
    metadata = {path: dict(frontmatter) for path, frontmatter in snapshot.metadata.items()}
    candidates: list[QueryCandidate] = []
    hashes: dict[str, str] = {}
    for page in snapshot.pages:
        path = str(page["path"])
        if not path.startswith("wiki/") or not snapshot_page_eligible(page, metadata, scope="knowledge", project=project, filters=filters):
            continue
        hashes[path] = str(page["content_hash"])
        candidates.append(QueryCandidate(path=root / path, rel=path, title=str(page["title"]), body=str(page["body"]), frontmatter=dict(metadata[path])))
    return candidates, hashes


_VOLATILE_KEY = re.compile(r"(_ms$|latency|elapsed|duration|timing|correlation)")


def _stable(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _stable(item) for key, item in value.items() if not _VOLATILE_KEY.search(str(key))}
    if isinstance(value, list):
        return [_stable(item) for item in value]
    return value


VAULTS = ("graph_v1", "ci", "synthetic")


@pytest.mark.parametrize("kind", VAULTS)
@pytest.mark.parametrize(
    ("project", "filters"),
    [
        (None, QueryFilters()),
        ("alpha", QueryFilters()),
        ("harbor", QueryFilters()),
        (None, QueryFilters(type="entity")),
        (None, QueryFilters(tags=("even",))),
    ],
)
def test_persisted_edges_rebuild_the_legacy_graph(kind: str, project: str | None, filters: QueryFilters, tmp_path: Path) -> None:
    root = _vault(kind, tmp_path)
    store = RetrievalIndexStore(root)
    candidates, hashes = _candidates(root, store, filters, project)
    stored = store.graph_links((WIKILINK,))
    edges = {path: links for path, (source_hash, links) in stored.items() if hashes.get(path) == source_hash}

    assert set(hashes) <= set(edges)
    legacy = _legacy_build_graph(root, candidates)
    assert build_graph(root, candidates, edges=edges) == legacy
    assert build_graph(root, candidates) == legacy


@pytest.mark.parametrize("kind", VAULTS)
def test_query_rankings_and_scores_match_the_legacy_graph(kind: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _vault(kind, tmp_path)
    questions = _questions(kind)
    current = [_stable(run_query_v2(root, question, retrieval_mode="lexical", debug=True, top_k=10)) for question in questions]
    monkeypatch.setattr(query_pipeline, "build_graph", _legacy_build_graph)
    legacy = [_stable(run_query_v2(root, question, retrieval_mode="lexical", debug=True, top_k=10)) for question in questions]

    assert current == legacy
    if kind != "ci":
        # The CI smoke fixture has no graph-reachable gold; the others must
        # actually exercise graph scoring for the comparison to mean anything.
        assert any(result.get("scores", {}).get("graph", 0) > 0 for payload in current for result in payload["results"])


def test_index_records_every_edge_kind_without_scoring_typed_relations(tmp_path: Path) -> None:
    root = _vault("synthetic", tmp_path)
    store = RetrievalIndexStore(root)
    links = store.graph_links()
    alpha = links["wiki/projects/alpha/specs/alpha-spec.md"][1]

    assert [(link.kind, link.dst) for link in alpha if link.kind != WIKILINK] == [
        ("applies_to", "alpha"),
        ("derived_from", "wiki/entities/node-1.md"),
        ("related_objects", "wiki/concepts/hub.md"),
        ("source", "raw/sources/doc/alpha/a.md"),
    ]
    assert [link.dst for link in links["wiki/projects/beta/specs/beta-spec.md"][1] if link.kind == "source"] == [
        "raw/sources/doc/beta/a.md",
        "raw/sources/doc/alpha/a.md",
    ]
    code_targets = [link.dst for link in links["wiki/concepts/code.md"][1] if link.kind == WIKILINK]
    assert code_targets == ["node-3"]
    paths = {link.dst: link.dst_paths for link in links["wiki/concepts/paths.md"][1] if link.kind == WIKILINK}
    assert paths["../entities/node-0|Alias".split("|")[0]] == ("wiki/entities/node-0.md", "entities/node-0.md")
    assert paths["../../outside"] == ("outside.md",)
    assert all(not path.startswith("..") for values in paths.values() for path in values)


def test_edges_follow_incremental_update_rename_and_delete(tmp_path: Path) -> None:
    root = _vault("synthetic", tmp_path)
    store = RetrievalIndexStore(root)
    hub = root / "wiki" / "concepts" / "hub.md"
    hub.write_text(hub.read_text(encoding="utf-8").replace("[[node-1]]", "[[node-6]]"), encoding="utf-8")

    assert store.update_page_from_file(hub)["ok"] is True
    source_hash, links = store.graph_links((WIKILINK,))["wiki/concepts/hub.md"]
    assert [link.dst for link in links] == ["node-0", "node-6", "concepts/node-2"]
    with sqlite3.connect(store.path) as connection:
        page_hash = connection.execute("SELECT redacted_content_hash FROM pages WHERE path = ?", ("wiki/concepts/hub.md",)).fetchone()[0]
    assert source_hash == page_hash

    renamed = root / "wiki" / "concepts" / "hub-renamed.md"
    hub.rename(renamed)
    from retrieval.retrieval_index import page_from_file

    page = page_from_file(root, renamed, scope="active")
    assert page is not None
    assert store.rename_page("wiki/concepts/hub.md", page)["ok"] is True
    after_rename = store.graph_links()
    assert "wiki/concepts/hub.md" not in after_rename
    assert [link.dst for link in after_rename["wiki/concepts/hub-renamed.md"][1]] == ["node-0", "node-6", "concepts/node-2"]

    assert store.delete_page("wiki/concepts/hub-renamed.md")["ok"] is True
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM links WHERE src = ?", ("wiki/concepts/hub-renamed.md",)).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM links WHERE src NOT IN (SELECT path FROM pages)").fetchone()[0] == 0


def test_stale_edges_fall_back_to_the_snapshot_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _vault("synthetic", tmp_path)
    store = RetrievalIndexStore(root)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE links SET source_hash = 'stale', dst_paths = '', dst_stem = 'nothing'")
    expected = [_stable(run_query_v2(root, question, retrieval_mode="lexical", debug=True)) for question in _questions("synthetic")]
    monkeypatch.setattr(query_pipeline, "build_graph", _legacy_build_graph)
    legacy = [_stable(run_query_v2(root, question, retrieval_mode="lexical", debug=True)) for question in _questions("synthetic")]
    assert expected == legacy


def test_previous_schema_requires_an_explicit_rebuild(tmp_path: Path) -> None:
    root = _vault("synthetic", tmp_path)
    store = RetrievalIndexStore(root)
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE links")
        connection.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(RETRIEVAL_SCHEMA_VERSION - 1),))

    assert store.status()["code"] == "index_incompatible"
    update = store.update_page_from_file(root / "wiki" / "concepts" / "hub.md")
    assert update["state"] == "rebuild_required"
    assert update["repair_action"] == "rebuild_retrieval_index"
    assert store.build(store.iter_vault_pages())["ok"] is True
    assert store.graph_links((WIKILINK,))["wiki/concepts/hub.md"][1]


def test_unaddressable_wikilink_targets_do_not_break_extraction(tmp_path: Path) -> None:
    links = extract_page_links("wiki/concepts/a.md", "[[.]] and [[b]]", {}, root=tmp_path)
    assert links == [PageLink(WIKILINK, ".", (), ""), PageLink(WIKILINK, "b", ("wiki/concepts/b.md", "wiki/b.md", "b.md"), "b")]


def test_lexical_resolution_matches_path_resolve_for_tricky_targets(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    rels = ["wiki/concepts/b.md", "wiki/concepts/sub/c.md", "wiki/entities/b2.md", "wiki/b3.md", "wiki/concepts/b.txt.md"]
    candidates = [QueryCandidate(path=root / rel, rel=rel, title=rel, body="") for rel in rels]
    by_rel = {candidate.rel: candidate.path for candidate in candidates}
    by_stem: dict[str, list[str]] = {}
    for rel, path in by_rel.items():
        by_stem.setdefault(path.stem.casefold(), []).append(rel)
    targets = [
        "b", "./b", "b.md", "B", "concepts/b", "wiki/concepts/b", "../concepts/b", "sub//c", "sub/./c", "sub/../b",
        "../../b3", "../../../outside", f"../../../{root.name}/wiki/concepts/b", str(root / "wiki" / "concepts" / "b"),
        "/elsewhere/b", "b.txt", "b.MD", "../entities/b2", "entities/b2", "b3", "c",
    ]
    body = " ".join(f"[[{target}]]" for target in targets)
    for rel in ("wiki/concepts/a.md", "wiki/concepts/sub/deep.md", "wiki/entities/e.md"):
        legacy = _legacy_wikilink_targets(body, root / rel, root, by_rel, by_stem)
        current = [
            target
            for link in extract_page_links(rel, body, {}, root=root)
            if link.kind == WIKILINK
            for target in [resolve_wikilink(link, by_rel, by_stem)]
            if target
        ]
        assert current == legacy, rel
        assert legacy  # the comparison must resolve something



def test_formal_delete_operation_removes_page_edges(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from wiki.atomic_file import sha256_file
    from wiki.page_mutation import DELETED_PAGE_HASH, PageMutationCoordinator
    from wiki.wiki_paths import create_wiki_root

    create_wiki_root(tmp_path)
    for name, body in (("page", "Links [[other]]."), ("other", "Links [[page]].")):
        path = tmp_path / "wiki" / "concepts" / "general" / f"{name}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"---\ntype: concept\ntitle: {name}\ngenerated: false\nsources: [raw/sources/doc/x/a.md]\n---\n\n# {name}\n\n{body}\n", encoding="utf-8")
    store = RetrievalIndexStore(tmp_path)
    store.build(store.iter_vault_pages())
    assert "wiki/concepts/general/page.md" in store.graph_links()
    monkeypatch.setattr("wiki.page_mutation_adapters.refresh_overview", lambda root, *, changed_path, created=None: {"ok": True})
    page = tmp_path / "wiki" / "concepts" / "general" / "page.md"
    page_hash = sha256_file(page)

    result = PageMutationCoordinator(tmp_path).write_and_project(
        operation_kind="delete",
        page_path="wiki/concepts/general/page.md",
        base_hash=page_hash,
        text="",
        intended_hash=DELETED_PAGE_HASH,
        expected_hash=page_hash,
    )

    assert result.ok is True
    links = store.graph_links()
    assert "wiki/concepts/general/page.md" not in links
    assert [link.dst for link in links["wiki/concepts/general/other.md"][1] if link.kind == WIKILINK] == ["page"]
