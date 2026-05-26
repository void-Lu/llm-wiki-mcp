"""Tests for the Louvain community detection algorithm."""

from __future__ import annotations

import pytest

from netsuite_llm_wiki_mcp.louvain import LouvainResult, community_cohesion, louvain


def test_louvain_empty_graph():
    result = louvain(set(), [])
    assert result.communities == []
    assert result.modularity == 0.0


def test_louvain_single_node():
    result = louvain({"a"}, [])
    assert result.community_map == {"a": 0}
    assert result.communities == [{"a"}]
    assert result.modularity == 0.0


def test_louvain_two_disconnected_nodes():
    result = louvain({"a", "b"}, [])
    assert len(result.communities) == 2
    assert result.community_map["a"] != result.community_map["b"]


def test_louvain_two_connected_nodes():
    result = louvain({"a", "b"}, [("a", "b")])
    assert len(result.communities) == 1
    assert result.community_map["a"] == result.community_map["b"]


def test_louvain_two_cliques():
    """Two fully-connected cliques joined by a single bridge edge."""
    nodes = {f"a{i}" for i in range(4)} | {f"b{i}" for i in range(4)}
    edges = []
    for i in range(4):
        for j in range(i + 1, 4):
            edges.append((f"a{i}", f"a{j}"))
            edges.append((f"b{i}", f"b{j}"))
    edges.append(("a0", "b0"))

    result = louvain(nodes, edges)
    assert result.modularity > 0
    assert len(result.communities) >= 2

    a_comm = result.community_map["a0"]
    b_comm = result.community_map["b1"]
    assert a_comm != b_comm
    for i in range(4):
        assert result.community_map[f"a{i}"] == a_comm
        assert result.community_map[f"b{i}"] == b_comm


def test_louvain_three_clusters():
    """Three triangles connected by single edges."""
    nodes = set()
    edges = []
    for prefix in ("x", "y", "z"):
        for i in range(3):
            nodes.add(f"{prefix}{i}")
        edges.extend([
            (f"{prefix}0", f"{prefix}1"),
            (f"{prefix}1", f"{prefix}2"),
            (f"{prefix}2", f"{prefix}0"),
        ])
    edges.append(("x0", "y0"))
    edges.append(("y0", "z0"))

    result = louvain(nodes, edges)
    assert result.modularity > 0
    assert len(result.communities) >= 2


def test_louvain_all_nodes_assigned():
    nodes = {"a", "b", "c", "d", "e"}
    edges = [("a", "b"), ("b", "c"), ("d", "e")]
    result = louvain(nodes, edges)
    assert set(result.community_map.keys()) == nodes
    all_members = set()
    for comm in result.communities:
        all_members.update(comm)
    assert all_members == nodes


def test_louvain_self_loops_ignored():
    nodes = {"a", "b", "c"}
    edges = [("a", "b"), ("b", "c"), ("a", "a")]
    result = louvain(nodes, edges)
    assert set(result.community_map.keys()) == nodes


def test_louvain_duplicate_edges():
    nodes = {"a", "b", "c"}
    edges = [("a", "b"), ("a", "b"), ("b", "c")]
    result = louvain(nodes, edges)
    assert set(result.community_map.keys()) == nodes


def test_community_cohesion_full_clique():
    members = {"a", "b", "c"}
    edges = [("a", "b"), ("b", "c"), ("a", "c")]
    assert community_cohesion(members, edges) == 1.0


def test_community_cohesion_no_internal_edges():
    members = {"a", "b", "c"}
    edges = [("a", "x"), ("b", "y")]
    assert community_cohesion(members, edges) == 0.0


def test_community_cohesion_partial():
    members = {"a", "b", "c"}
    edges = [("a", "b")]
    cohesion = community_cohesion(members, edges)
    assert 0.0 < cohesion < 1.0
    assert abs(cohesion - 1.0 / 3.0) < 0.01


def test_community_cohesion_single_node():
    assert community_cohesion({"a"}, []) == 1.0
