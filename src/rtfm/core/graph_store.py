"""Graph store — loading, persistence, and query helpers."""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import Any

from ._backend import GraphProtocol, create_graph


# ---------------------------------------------------------------------------
# Loading / persistence
# ---------------------------------------------------------------------------


def _parse_json_graph(json_path: Path) -> tuple[GraphProtocol, dict[str, int]]:
    """Parse a graph JSON file into a graph object and node index."""
    with open(json_path) as f:
        data = json.load(f)

    nodes = data.get("nodes", [])
    edges = data.get("edges", [])

    if not nodes:
        raise ValueError(f"No nodes found in {json_path}")

    g = create_graph(directed=True)
    id_to_idx: dict[str, int] = {}

    g.add_vertices(len(nodes))
    for i, node in enumerate(nodes):
        nid = node.pop("id")
        ntype = node.pop("node_type")
        cluster = node.pop("cluster_id", 0)
        source = node.pop("source_file", "")
        updated = node.pop("last_updated", "")
        checksum = node.pop("checksum", "")

        g.vs[i]["node_id"] = nid
        g.vs[i]["node_type"] = ntype
        g.vs[i]["cluster_id"] = cluster
        g.vs[i]["source_file"] = source
        g.vs[i]["last_updated"] = updated
        g.vs[i]["checksum"] = checksum
        g.vs[i]["attrs"] = node
        id_to_idx[nid] = i

    valid_edges: list[tuple[int, int]] = []
    edge_types: list[str] = []
    edge_metadata: list[dict] = []

    for edge in edges:
        src = id_to_idx.get(edge["source"])
        tgt = id_to_idx.get(edge["target"])
        if src is not None and tgt is not None:
            valid_edges.append((src, tgt))
            edge_types.append(edge["edge_type"])
            edge_metadata.append(edge.get("metadata", {}))

    if valid_edges:
        g.add_edges(valid_edges)
        g.es["edge_type"] = edge_types
        g.es["metadata"] = edge_metadata

    return g, id_to_idx


def _rebuild_node_index(g: GraphProtocol) -> dict[str, int]:
    return {g.vs[i]["node_id"]: i for i in range(g.vcount())}


def load_pickle(pickle_path: Path) -> tuple[GraphProtocol, dict[str, int]]:
    """Load a graph from a pickle file."""
    with open(pickle_path, "rb") as f:
        g = pickle.load(f)
    return g, _rebuild_node_index(g)


def build_pickle(json_path: Path, pickle_path: Path) -> tuple[GraphProtocol, dict[str, int]]:
    """Parse JSON graph and write a pickle cache."""
    g, node_index = _parse_json_graph(json_path)
    pickle_path.parent.mkdir(parents=True, exist_ok=True)
    with open(pickle_path, "wb") as f:
        pickle.dump(g, f, protocol=pickle.HIGHEST_PROTOCOL)
    return g, node_index


def load_or_rebuild(json_path: Path, pickle_path: Path) -> tuple[GraphProtocol, dict[str, int]]:
    """Load from pickle if fresh, otherwise rebuild from JSON."""
    if pickle_path.is_file() and json_path.is_file():
        if pickle_path.stat().st_mtime >= json_path.stat().st_mtime:
            return load_pickle(pickle_path)
    if not json_path.is_file():
        raise FileNotFoundError(f"Graph JSON not found: {json_path}")
    return build_pickle(json_path, pickle_path)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def kb_miss(message: str = "No results found") -> dict[str, Any]:
    """Return a standard 'no results' response."""
    return {"results": [], "result_count": 0, "kb_miss": True, "message": message}


def matches_project(v: Any, project: str) -> bool:
    """Check if a vertex belongs to a given project by attrs or source_file."""
    if v["attrs"].get("project") == project:
        return True
    sf = v["source_file"]
    if f"projects/{project}" in sf or f"projects\\{project}" in sf:
        return True
    return False


def _get_edge_type_between(
    graph: GraphProtocol, idx1: int, idx2: int, direction: str
) -> str:
    """Get the edge type between two vertices."""
    if direction == "in":
        edges = graph.es.select(_between=([idx2], [idx1]))
    elif direction == "out":
        edges = graph.es.select(_between=([idx1], [idx2]))
    else:
        edges = graph.es.select(_between=([idx1], [idx2]))
        if not edges:
            edges = graph.es.select(_between=([idx2], [idx1]))
    return edges[0]["edge_type"] if edges else ""


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------


def find_vertex(
    node_index: dict[str, int], graph: GraphProtocol, node_id: str
) -> Any | None:
    """Find a vertex by exact node_id."""
    idx = node_index.get(node_id)
    if idx is not None:
        return graph.vs[idx]
    return None


def find_by_name(
    graph: GraphProtocol, node_index: dict[str, int], query: str
) -> Any | None:
    """Find a vertex by name — exact match, then substring match."""
    v = find_vertex(node_index, graph, query)
    if v is not None:
        return v

    query_lower = query.lower()

    for v in graph.vs:
        if query_lower in v["node_id"].lower():
            return v

    for v in graph.vs:
        for val in v["attrs"].values():
            if isinstance(val, str) and query_lower == val.lower():
                return v

    for v in graph.vs:
        for val in v["attrs"].values():
            if isinstance(val, str) and query_lower in val.lower():
                return v

    return None


def search_nodes(
    graph: GraphProtocol,
    node_index: dict[str, int],
    query: str,
    max_results: int = 10,
    project: str | None = None,
) -> list[Any]:
    """Search nodes by text match across node_id, source_file, and attrs."""
    query_lower = query.lower()
    matches: list[Any] = []

    for v in graph.vs:
        if project and not matches_project(v, project):
            continue

        searchable = v["node_id"].lower() + " " + v["source_file"].lower()
        attrs = v["attrs"]
        for val in attrs.values():
            if isinstance(val, str) and val:
                searchable += " " + val.lower()

        if query_lower in searchable:
            matches.append(v)
            if len(matches) >= max_results:
                break

    return matches


def vertex_to_dict(v: Any) -> dict[str, Any]:
    """Convert a vertex to a plain dict."""
    d: dict[str, Any] = {
        "node_id": v["node_id"],
        "node_type": v["node_type"],
        "cluster_id": v["cluster_id"],
        "source_file": v["source_file"],
    }
    d.update(v["attrs"])
    return d


def edge_to_dict(graph: GraphProtocol, e: Any) -> dict[str, Any]:
    """Convert an edge to a plain dict."""
    return {
        "source": graph.vs[e.source]["node_id"],
        "target": graph.vs[e.target]["node_id"],
        "edge_type": e["edge_type"],
        "metadata": e["metadata"],
    }
