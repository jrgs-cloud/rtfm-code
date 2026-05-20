"""Graph backend abstraction — igraph (fast) or networkx (portable).

Provides a unified interface so graph_builder, graph_store, and graph_analysis
work identically regardless of which backend is available.

Backend selection: try igraph first (faster), fall back to networkx.
"""

from __future__ import annotations

from typing import Any, Protocol

try:
    import igraph as ig
    BACKEND = "igraph"
except ImportError:
    ig = None  # type: ignore[assignment]
    BACKEND = "networkx"

import networkx as nx  # always available — it's the required dependency


# ---------------------------------------------------------------------------
# Protocol — the interface both backends satisfy
# ---------------------------------------------------------------------------


class GraphProtocol(Protocol):
    """Minimal graph interface used by graph_analysis and graph_store."""

    vs: Any
    es: Any

    def vcount(self) -> int: ...
    def ecount(self) -> int: ...
    def degree(self, vertices: Any = None, mode: str = "all") -> Any: ...
    def neighbors(self, vertex: int, mode: str = "all") -> list[int]: ...
    def as_undirected(self, mode: str = "collapse") -> "GraphProtocol": ...
    def subgraph(self, vertices: list[int]) -> "GraphProtocol": ...
    def add_vertices(self, n: int) -> None: ...
    def add_edges(self, edges: list[tuple[int, int]]) -> None: ...
    def community_leiden(self, objective_function: str = "modularity", resolution: float = 1.0) -> Any: ...


# ---------------------------------------------------------------------------
# NetworkX adapter — mimics igraph's .vs/.es/.vcount()/.ecount() interface
# ---------------------------------------------------------------------------


class _VertexView:
    """Mimics igraph's VertexSeq (graph.vs) for networkx."""

    def __init__(self, g: "NxGraph"):
        self._g = g

    def __getitem__(self, key):
        if isinstance(key, int):
            return _Vertex(self._g, key)
        # Bulk attribute access: graph.vs["node_id"] -> list
        if isinstance(key, str):
            return [self._g._nodes[i].get(key) for i in range(len(self._g._nodes))]
        raise KeyError(key)

    def __setitem__(self, key: str, values: list):
        """Bulk attribute set: graph.vs["attr"] = [...]"""
        for i, val in enumerate(values):
            self._g._nodes[i][key] = val

    def __iter__(self):
        for i in range(len(self._g._nodes)):
            yield _Vertex(self._g, i)

    def __len__(self):
        return len(self._g._nodes)

    def select(self, **kwargs):
        """Mimics igraph's vs.select() for filtered vertex queries."""
        results = []
        for i in range(len(self._g._nodes)):
            match = True
            for attr, val in kwargs.items():
                if self._g._nodes[i].get(attr) != val:
                    match = False
                    break
            if match:
                results.append(_Vertex(self._g, i))
        return results


class _Vertex:
    """Mimics igraph.Vertex."""

    def __init__(self, g: "NxGraph", idx: int):
        self._g = g
        self._idx = idx

    def __getitem__(self, key: str):
        return self._g._nodes[self._idx].get(key)

    def __setitem__(self, key: str, value):
        self._g._nodes[self._idx][key] = value

    def get(self, key: str, default=None):
        return self._g._nodes[self._idx].get(key, default)

    @property
    def index(self) -> int:
        return self._idx


class _EdgeSeq:
    """Mimics igraph's EdgeSeq (graph.es) for networkx."""

    def __init__(self, g: "NxGraph"):
        self._g = g

    def __getitem__(self, key):
        if isinstance(key, int):
            return _Edge(self._g, key)
        # Bulk attribute access: graph.es["edge_type"] -> list
        if isinstance(key, str):
            return [self._g._edges[i].get(key) for i in range(len(self._g._edges))]
        raise KeyError(key)

    def __setitem__(self, key: str, values: list):
        """Bulk attribute set: graph.es["attr"] = [...]"""
        for i, val in enumerate(values):
            self._g._edges[i][key] = val

    def __iter__(self):
        for i in range(len(self._g._edges)):
            yield _Edge(self._g, i)

    def __len__(self):
        return len(self._g._edges)

    def select(self, **kwargs):
        """Mimics igraph's es.select() for filtered edge queries."""
        between = kwargs.get("_between")
        if between:
            sources, targets = between
            results = []
            for i, e in enumerate(self._g._edges):
                if e["_source"] in sources and e["_target"] in targets:
                    results.append(_Edge(self._g, i))
            return results

        # Filter by _source or _target
        results = []
        for i, e in enumerate(self._g._edges):
            match = True
            if "_source" in kwargs and e["_source"] != kwargs["_source"]:
                match = False
            if "_target" in kwargs and e["_target"] != kwargs["_target"]:
                match = False
            if match:
                results.append(_Edge(self._g, i))
        return results


class _Edge:
    """Mimics igraph.Edge."""

    def __init__(self, g: "NxGraph", idx: int):
        self._g = g
        self._idx = idx

    def __getitem__(self, key: str):
        return self._g._edges[self._idx].get(key)

    @property
    def source(self) -> int:
        return self._g._edges[self._idx]["_source"]

    @property
    def target(self) -> int:
        return self._g._edges[self._idx]["_target"]


class NxGraph:
    """NetworkX-backed graph with igraph-compatible interface."""

    def __init__(self, directed: bool = True):
        self._nx = nx.DiGraph() if directed else nx.Graph()
        self._nodes: list[dict[str, Any]] = []
        self._edges: list[dict[str, Any]] = []
        self._directed = directed
        self.vs = _VertexView(self)
        self.es = _EdgeSeq(self)
        self._backend = "networkx"

    def add_vertices(self, n: int) -> None:
        """Add n new vertices to the graph."""
        for i in range(len(self._nodes), len(self._nodes) + n):
            self._nodes.append({})
            self._nx.add_node(i)

    def add_edges(self, edges: list[tuple[int, int]]) -> None:
        """Add edges as (source_idx, target_idx) tuples."""
        start = len(self._edges)
        for i, (src, tgt) in enumerate(edges):
            self._edges.append({"_source": src, "_target": tgt})
            self._nx.add_edge(src, tgt, _edge_idx=start + i)

    def vcount(self) -> int:
        """Return the number of vertices."""
        return len(self._nodes)

    def ecount(self) -> int:
        """Return the number of edges."""
        return len(self._edges)

    def degree(self, vertices=None, mode="all") -> list[int] | int:
        """Return vertex degree(s). Mode: 'in', 'out', or 'all'."""
        if vertices is not None:
            if isinstance(vertices, int):
                if mode == "in":
                    return self._nx.in_degree(vertices)
                elif mode == "out":
                    return self._nx.out_degree(vertices)
                return self._nx.degree(vertices)
        # Return degrees for all vertices
        result = []
        for i in range(self.vcount()):
            if mode == "in":
                result.append(self._nx.in_degree(i))
            elif mode == "out":
                result.append(self._nx.out_degree(i))
            else:
                result.append(self._nx.degree(i))
        return result

    def neighbors(self, vertex: int, mode: str = "all") -> list[int]:
        """Return neighbor vertex indices. Mode: 'in', 'out', or 'all'."""
        if mode == "out":
            return list(self._nx.successors(vertex))
        elif mode == "in":
            return list(self._nx.predecessors(vertex))
        else:
            return list(set(self._nx.successors(vertex)) | set(self._nx.predecessors(vertex)))

    def incident(self, vertex: int, mode: str = "all") -> list[int]:
        """Return edge indices incident to vertex."""
        result = []
        for i, e in enumerate(self._edges):
            if mode == "out" and e["_source"] == vertex:
                result.append(i)
            elif mode == "in" and e["_target"] == vertex:
                result.append(i)
            elif mode == "all" and (e["_source"] == vertex or e["_target"] == vertex):
                result.append(i)
        return result

    def as_undirected(self, mode: str = "collapse") -> "NxGraph":
        """Return undirected copy."""
        ug = NxGraph(directed=False)
        ug._nodes = [dict(n) for n in self._nodes]
        ug._nx = self._nx.to_undirected()
        # Edges in undirected — deduplicate
        seen: set[tuple[int, int]] = set()
        for e in self._edges:
            pair = (min(e["_source"], e["_target"]), max(e["_source"], e["_target"]))
            if pair not in seen:
                seen.add(pair)
                ug._edges.append(dict(e))
        return ug

    def subgraph(self, vertices: list[int]) -> "NxGraph":
        """Return induced subgraph."""
        sg = NxGraph(directed=self._directed)
        old_to_new = {old: new for new, old in enumerate(vertices)}
        sg._nodes = [dict(self._nodes[v]) for v in vertices]
        for v in range(len(vertices)):
            sg._nx.add_node(v)
        for e in self._edges:
            if e["_source"] in old_to_new and e["_target"] in old_to_new:
                new_src = old_to_new[e["_source"]]
                new_tgt = old_to_new[e["_target"]]
                new_edge = dict(e)
                new_edge["_source"] = new_src
                new_edge["_target"] = new_tgt
                sg._edges.append(new_edge)
                sg._nx.add_edge(new_src, new_tgt)
        sg.vs = _VertexView(sg)
        sg.es = _EdgeSeq(sg)
        return sg

    def community_leiden(self, objective_function: str = "modularity", resolution: float = 1.0):
        """Louvain community detection (networkx fallback for Leiden)."""
        from networkx.algorithms.community import louvain_communities
        undirected = self._nx.to_undirected() if self._directed else self._nx
        communities = louvain_communities(undirected, resolution=resolution, seed=42)
        # Build membership list matching igraph's partition.membership format
        membership = [0] * self.vcount()
        for cluster_id, community in enumerate(communities):
            for node in community:
                membership[node] = cluster_id
        return _Partition(membership)


class _Partition:
    """Mimics igraph's VertexClustering result."""

    def __init__(self, membership: list[int]):
        self.membership = membership


# ---------------------------------------------------------------------------
# Public API — use these instead of importing igraph directly
# ---------------------------------------------------------------------------


def create_graph(directed: bool = True):
    """Create a new graph using the available backend."""
    if BACKEND == "igraph":
        return ig.Graph(directed=directed)
    return NxGraph(directed=directed)


def get_backend() -> str:
    """Return which backend is active."""
    return BACKEND
