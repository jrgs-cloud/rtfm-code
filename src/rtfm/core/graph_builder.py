"""Graph assembly, Leiden clustering, and serialization."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any

from ._backend import BACKEND, GraphProtocol, create_graph
from .types import (
    ET_CONTAINS,
    EdgeDict,
    ExtractionResult,
    NodeDict,
    make_node,
    NT_PACKAGE,
)


def merge_results(
    results: list[ExtractionResult],
) -> tuple[list[NodeDict], list[EdgeDict]]:
    """Deduplicate nodes by ID across all extraction results.

    Later occurrences of the same node ID overwrite earlier ones.
    Edges are concatenated without deduplication (multiple edges
    between the same pair with different types are valid).
    """
    seen: dict[str, NodeDict] = {}
    edges: list[EdgeDict] = []
    for result in results:
        for node in result.nodes:
            seen[node["id"]] = node
        edges.extend(result.edges)
    return list(seen.values()), edges


def inject_packages(
    nodes: list[NodeDict],
    edges: list[EdgeDict],
    project_root: str = "",
) -> tuple[list[NodeDict], list[EdgeDict]]:
    """Add PackageNode hierarchy from directory structure.

    Walks all node source_files, creates a PackageNode for each directory
    that contains at least one node, and emits 'contains' edges from
    package -> child nodes and package -> child packages.

    This gives the graph a filesystem-derived hierarchy so any directory
    can be queried as an index of its contents.
    """
    if not nodes:
        return nodes, edges

    # Derive project root from common path prefix if not provided
    source_files = [n["source_file"] for n in nodes if n.get("source_file")]
    if not project_root and source_files:
        from os.path import commonpath
        try:
            project_root = commonpath(source_files)
            # If commonpath returns a file, go up to its directory
            if not project_root.endswith("/"):
                project_root = str(PurePosixPath(project_root).parent)
        except ValueError:
            project_root = ""

    if not project_root:
        return nodes, edges

    # Map: relative directory -> list of node IDs in that directory
    dir_children: dict[str, list[str]] = defaultdict(list)
    root_prefix = project_root.rstrip("/") + "/" if project_root else ""
    for n in nodes:
        src = n.get("source_file", "")
        if not src:
            continue
        # Handle both absolute and relative source_file paths
        if src.startswith("/"):
            # Absolute path — must be under project_root
            if not src.startswith(root_prefix) and src != project_root:
                continue
            rel = str(PurePosixPath(src).relative_to(project_root))
        else:
            # Already relative to project_root
            rel = src
        parent_dir = str(PurePosixPath(rel).parent)
        if parent_dir == ".":
            parent_dir = ""
        dir_children[parent_dir].append(n["id"])

    if not dir_children:
        return nodes, edges

    # Collect all directory paths that need PackageNodes
    all_dirs: set[str] = set()
    for d in dir_children:
        # Walk up to root, creating intermediate packages
        parts = PurePosixPath(d).parts if d else ()
        for i in range(len(parts)):
            all_dirs.add(str(PurePosixPath(*parts[: i + 1])))
    if "" in dir_children:
        all_dirs.add("")  # project root itself

    # Create PackageNodes
    existing_ids = {n["id"] for n in nodes}
    new_nodes: list[NodeDict] = []
    new_edges: list[EdgeDict] = []

    for d in sorted(all_dirs):
        pkg_id = f"package::{d}" if d else "package::root"
        if pkg_id in existing_ids:
            continue
        display_name = d.split("/")[-1] if d else "project-root"
        child_count = len(dir_children.get(d, []))
        sub_pkgs = [sd for sd in all_dirs if sd != d and str(PurePosixPath(sd).parent) == (d or ".")]

        new_nodes.append(make_node(
            id=pkg_id,
            node_type=NT_PACKAGE,
            source_file=f"{project_root}/{d}" if d else project_root,
            checksum=f"pkg:{d or 'root'}",
            name=display_name,
            child_count=child_count,
            sub_package_count=len(sub_pkgs),
        ))

        # Package -> direct child nodes
        for child_id in dir_children.get(d, []):
            new_edges.append(EdgeDict(
                source=pkg_id,
                target=child_id,
                edge_type=ET_CONTAINS,
                metadata={},
            ))

        # Package -> child packages
        for sub in sub_pkgs:
            sub_id = f"package::{sub}"
            new_edges.append(EdgeDict(
                source=pkg_id,
                target=sub_id,
                edge_type=ET_CONTAINS,
                metadata={},
            ))

    return nodes + new_nodes, edges + new_edges


def build_graph(
    nodes: list[NodeDict],
    edges: list[EdgeDict],
) -> GraphProtocol:
    """Create a graph from node and edge dicts using the available backend."""
    g = create_graph(directed=True)

    if not nodes:
        return g

    id_to_idx: dict[str, int] = {}
    for i, node in enumerate(nodes):
        id_to_idx[node["id"]] = i

    g.add_vertices(len(nodes))
    g.vs["node_id"] = [n.get("id", n.get("node_id", "")) for n in nodes]
    g.vs["node_type"] = [n.get("node_type", "") for n in nodes]
    g.vs["cluster_id"] = [n.get("cluster_id", 0) for n in nodes]
    g.vs["source_file"] = [n.get("source_file", "") for n in nodes]
    g.vs["last_updated"] = [n.get("last_updated", "") for n in nodes]
    g.vs["checksum"] = [n.get("checksum", "") for n in nodes]
    g.vs["attrs"] = [n.get("attrs", {}) for n in nodes]

    valid_edges: list[tuple[int, int]] = []
    edge_types: list[str] = []
    edge_metadata: list[dict[str, Any]] = []

    for edge in edges:
        src_idx = id_to_idx.get(edge["source"])
        tgt_idx = id_to_idx.get(edge["target"])
        if src_idx is not None and tgt_idx is not None:
            valid_edges.append((src_idx, tgt_idx))
            edge_types.append(edge["edge_type"])
            edge_metadata.append(edge["metadata"])

    if valid_edges:
        g.add_edges(valid_edges)
        g.es["edge_type"] = edge_types
        g.es["metadata"] = edge_metadata

    return g


def run_leiden(graph: GraphProtocol) -> GraphProtocol:
    """Run community detection and assign cluster_id to each vertex.

    Uses Leiden (igraph) or Louvain (networkx) depending on backend.

    Strategy for sparse graphs (avg degree < 2):
    1. Run clustering on the connected subgraph only.
    2. Assign isolated nodes (degree 0) to clusters by source-file directory
       affinity — nodes from the same directory land in the same cluster.
    """
    if graph.vcount() == 0:
        return graph

    undirected = graph.as_undirected(mode="collapse")

    # Separate connected vs isolated vertices
    degrees = undirected.degree()
    connected_indices = [i for i, d in enumerate(degrees) if d > 0]
    isolated_indices = [i for i, d in enumerate(degrees) if d == 0]

    # Run clustering only on the connected subgraph
    if connected_indices:
        subgraph = undirected.subgraph(connected_indices)

        partition = subgraph.community_leiden(
            objective_function="modularity",
            resolution=1.0,
        )

        # Map subgraph cluster IDs back to the full graph
        for sub_idx, cluster in enumerate(partition.membership):
            orig_idx = connected_indices[sub_idx]
            graph.vs[orig_idx]["cluster_id"] = cluster
        next_cluster = max(partition.membership) + 1
    else:
        next_cluster = 0

    # Assign isolated nodes by directory affinity
    if isolated_indices:
        dir_to_cluster: dict[str, int] = {}
        for idx in isolated_indices:
            source = graph.vs[idx]["source_file"]
            dir_key = str(PurePosixPath(source).parent) if source else "__orphan__"
            if dir_key not in dir_to_cluster:
                dir_to_cluster[dir_key] = next_cluster
                next_cluster += 1
            graph.vs[idx]["cluster_id"] = dir_to_cluster[dir_key]

    return graph


def serialize(graph: GraphProtocol, output_path: str, project_root: str = "") -> None:
    """Write graph to JSON with sorted keys for deterministic diffs.

    If project_root is provided, source_file paths are relativized.
    """
    # Normalize root for stripping
    root = project_root.rstrip("/") + "/" if project_root else ""

    def _rel(path: str) -> str:
        if root and path and path.startswith(root):
            return path[len(root):]
        return path

    nodes_out: list[dict[str, Any]] = []
    for v in graph.vs:
        node: dict[str, Any] = {
            "id": _rel(v["node_id"]),
            "node_type": v["node_type"],
            "cluster_id": v["cluster_id"],
            "source_file": _rel(v["source_file"]),
            "last_updated": v["last_updated"],
            "checksum": v["checksum"],
        }
        node.update(v["attrs"])
        nodes_out.append(node)

    edges_out: list[dict[str, Any]] = []
    for e in graph.es:
        edges_out.append({
            "source": _rel(graph.vs[e.source]["node_id"]),
            "target": _rel(graph.vs[e.target]["node_id"]),
            "edge_type": e["edge_type"],
            "metadata": e["metadata"],
        })

    cluster_ids = {v["cluster_id"] for v in graph.vs} if graph.vcount() > 0 else set()

    data = {
        "edges": edges_out,
        "metadata": {
            "built_at": datetime.now(timezone.utc).isoformat(),
            "cluster_count": len(cluster_ids),
            "edge_count": graph.ecount(),
            "node_count": graph.vcount(),
        },
        "nodes": nodes_out,
    }

    from pathlib import Path
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w") as f:
        json.dump(data, f, indent=2, sort_keys=True)
