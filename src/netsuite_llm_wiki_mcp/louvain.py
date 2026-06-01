"""Louvain community detection for undirected graphs.

Pure-Python implementation — no external dependencies.
Optimized for wiki-scale graphs (hundreds to low thousands of nodes).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass


@dataclass
class LouvainResult:
    community_map: dict[str, int]
    communities: list[set[str]]
    modularity: float


def louvain(
    nodes: set[str],
    edges: list[tuple[str, str]],
    resolution: float = 1.0,
    max_iterations: int = 50,
) -> LouvainResult:
    """Run Louvain community detection on an undirected unweighted graph."""
    if len(nodes) < 2:
        community_map = {n: 0 for n in nodes}
        return LouvainResult(
            community_map=community_map,
            communities=[set(nodes)] if nodes else [],
            modularity=0.0,
        )

    adj: dict[str, dict[str, float]] = {n: {} for n in nodes}
    for u, v in edges:
        if u not in nodes or v not in nodes or u == v:
            continue
        adj[u][v] = adj[u].get(v, 0.0) + 1.0
        adj[v][u] = adj[v].get(u, 0.0) + 1.0

    node_list = sorted(nodes)
    node_to_idx = {n: i for i, n in enumerate(node_list)}
    n = len(node_list)

    idx_adj: list[dict[int, float]] = [{}] * n
    for i, node in enumerate(node_list):
        idx_adj[i] = {node_to_idx[nb]: w for nb, w in adj[node].items()}

    community = list(range(n))
    community = _phase1(idx_adj, community, n, resolution, max_iterations)

    prev_num_communities = n
    while True:
        unique = sorted(set(community))
        if len(unique) >= prev_num_communities:
            break
        prev_num_communities = len(unique)

        comm_remap = {c: i for i, c in enumerate(unique)}
        community = [comm_remap[c] for c in community]
        num_super = len(unique)

        super_adj, super_members = _aggregate(idx_adj, community, num_super)
        super_community = list(range(num_super))
        super_community = _phase1(super_adj, super_community, num_super, resolution, max_iterations)

        new_community = [0] * n
        for i in range(n):
            super_node = community[i]
            new_community[i] = super_community[super_node]
        community = new_community

    unique = sorted(set(community))
    comm_remap = {c: i for i, c in enumerate(unique)}
    community = [comm_remap[c] for c in community]

    community_map = {node_list[i]: community[i] for i in range(n)}
    communities: list[set[str]] = [set() for _ in range(len(unique))]
    for node, comm_id in community_map.items():
        communities[comm_id].add(node)

    mod = _modularity(idx_adj, community, n, resolution)

    return LouvainResult(
        community_map=community_map,
        communities=communities,
        modularity=mod,
    )


def _phase1(
    adj: list[dict[int, float]],
    community: list[int],
    n: int,
    resolution: float,
    max_iterations: int,
) -> list[int]:
    """Local moving phase: greedily move nodes to maximize modularity."""
    m2 = sum(sum(neighbors.values()) for neighbors in adj)
    if m2 == 0:
        return community

    degree = [sum(neighbors.values()) for neighbors in adj]

    comm_tot: dict[int, float] = defaultdict(float)
    for i in range(n):
        comm_tot[community[i]] += degree[i]

    for _ in range(max_iterations):
        improved = False
        for i in range(n):
            old_comm = community[i]
            ki = degree[i]

            neighbor_comms: dict[int, float] = defaultdict(float)
            for j, w in adj[i].items():
                neighbor_comms[community[j]] += w

            comm_tot[old_comm] -= ki
            ki_in_old = neighbor_comms.get(old_comm, 0.0)

            best_comm = old_comm
            best_gain = 0.0

            for c, ki_in_c in neighbor_comms.items():
                gain = ki_in_c / m2 - resolution * comm_tot[c] * ki / (m2 * m2)
                old_gain = ki_in_old / m2 - resolution * comm_tot[old_comm] * ki / (m2 * m2)
                delta = gain - old_gain
                if delta > best_gain:
                    best_gain = delta
                    best_comm = c

            community[i] = best_comm
            comm_tot[best_comm] += ki
            if best_comm != old_comm:
                improved = True

        if not improved:
            break

    return community


def _aggregate(
    adj: list[dict[int, float]],
    community: list[int],
    num_communities: int,
) -> tuple[list[dict[int, float]], list[list[int]]]:
    """Aggregate graph: communities become super-nodes. Internal edges become self-loops."""
    super_adj: list[dict[int, float]] = [{} for _ in range(num_communities)]
    members: list[list[int]] = [[] for _ in range(num_communities)]

    for i, c in enumerate(community):
        members[c].append(i)

    for i in range(len(adj)):
        ci = community[i]
        for j, w in adj[i].items():
            cj = community[j]
            super_adj[ci][cj] = super_adj[ci].get(cj, 0.0) + w

    return super_adj, members


def _modularity(
    adj: list[dict[int, float]],
    community: list[int],
    n: int,
    resolution: float,
) -> float:
    """Compute modularity Q for the given partition.

    Q = Σ_c [L_c/m - (d_c/(2m))²]
    where L_c = internal edge weight in community c, d_c = sum of degrees in c, m = total edge weight.
    """
    m2 = sum(sum(neighbors.values()) for neighbors in adj)
    if m2 == 0:
        return 0.0
    m = m2 / 2.0

    degree = [sum(neighbors.values()) for neighbors in adj]

    comm_internal: dict[int, float] = defaultdict(float)
    comm_degree: dict[int, float] = defaultdict(float)
    for i in range(n):
        c = community[i]
        comm_degree[c] += degree[i]
        for j, w in adj[i].items():
            if community[j] == c:
                comm_internal[c] += w

    q = 0.0
    for c in set(community):
        lc = comm_internal[c] / 2.0
        dc = comm_degree[c]
        q += lc / m - resolution * (dc / (2.0 * m)) ** 2
    return q


def community_cohesion(
    community_members: set[str],
    edges: list[tuple[str, str]],
) -> float:
    """Compute cohesion = internal_edges / possible_edges for a community."""
    size = len(community_members)
    if size < 2:
        return 1.0
    possible = size * (size - 1) / 2
    internal = sum(
        1 for u, v in edges
        if u in community_members and v in community_members and u != v
    )
    return internal / possible
