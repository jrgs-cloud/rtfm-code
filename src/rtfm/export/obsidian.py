"""Obsidian vault export — generates markdown with wikilinks for graph exploration."""

from __future__ import annotations

import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..core.graph_store import load_or_rebuild


def _sanitize_filename(node_id: str) -> str:
    name = node_id.replace("/", "__").replace("::", "__")
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    if len(name) > 120:
        name = name[:120]
    return name.strip("_.- ")


def _build_frontmatter(
    node_type: str, cluster_id: int, in_degree: int, out_degree: int
) -> str:
    lines = [
        "---",
        f"node_type: {node_type}",
        f"cluster_id: {cluster_id}",
        f"in_degree: {in_degree}",
        f"out_degree: {out_degree}",
        "---",
    ]
    return "\n".join(lines)


def _build_relationships(node_idx: int, graph: Any) -> str:
    outgoing = graph.es.select(_source=node_idx)
    incoming = graph.es.select(_target=node_idx)

    if not outgoing and not incoming:
        return ""

    lines = ["\n## Relationships\n"]
    for e in sorted(outgoing, key=lambda x: (x["edge_type"], graph.vs[x.target]["node_id"])):
        target_name = _sanitize_filename(graph.vs[e.target]["node_id"])
        lines.append(f"- {e['edge_type']} [[{target_name}]]")
    for e in sorted(incoming, key=lambda x: (x["edge_type"], graph.vs[x.source]["node_id"])):
        source_name = _sanitize_filename(graph.vs[e.source]["node_id"])
        lines.append(f"- {e['edge_type']} (from [[{source_name}]])")

    return "\n".join(lines)


def _get_semantic_neighbors(
    index_path: Path, node_ids: list[str], top_k: int = 3
) -> dict[str, list[dict]]:
    """Find semantically similar nodes from the vector index (top-k per node)."""
    try:
        import lancedb
    except ImportError:
        return {}

    db_path = str(index_path)
    try:
        db = lancedb.connect(db_path)
        table = db.open_table("chunks")
    except Exception:
        return {}

    all_data = table.to_arrow()
    node_id_col = all_data.column("node_id")
    vector_col = all_data.column("vector")

    id_to_vector: dict[str, list[float]] = {}
    for i in range(all_data.num_rows):
        nid = node_id_col[i].as_py()
        if nid in node_ids:
            id_to_vector[nid] = vector_col[i].as_py()

    neighbors: dict[str, list[dict]] = {}
    for nid, vector in id_to_vector.items():
        results = table.search(vector).limit(top_k + 1).to_arrow()
        similar = []
        for i in range(results.num_rows):
            result_id = results.column("node_id")[i].as_py()
            if result_id == nid:
                continue
            distance = results.column("_distance")[i].as_py()
            similar.append({"node_id": result_id, "score": 1 - distance})
        neighbors[nid] = similar[:top_k]

    return neighbors


def _build_similar_section(
    node_idx: int, similar_nodes: list[dict], graph: Any
) -> str:
    if not similar_nodes:
        return ""

    outgoing = graph.es.select(_source=node_idx)
    incoming = graph.es.select(_target=node_idx)
    connected_ids: set[str] = set()
    for e in outgoing:
        connected_ids.add(graph.vs[e.target]["node_id"])
    for e in incoming:
        connected_ids.add(graph.vs[e.source]["node_id"])

    unconnected = [n for n in similar_nodes if n["node_id"] not in connected_ids]
    if not unconnected:
        return ""

    lines = ["\n## Similar\n"]
    for n in unconnected[:3]:
        name = _sanitize_filename(n["node_id"])
        score = n.get("score", 0)
        lines.append(f"- [[{name}]] (similarity: {score:.2f})")

    return "\n".join(lines)


def export_vault(
    graph_json: Path,
    output_dir: Path,
    *,
    graph_pickle: Path | None = None,
    index_path: Path | None = None,
    include_semantic: bool = False,
    max_nodes: int = 500,
) -> dict[str, Any]:
    """Export the graph as an Obsidian-compatible markdown vault with wikilinks.

    Each node becomes a markdown file with YAML frontmatter and [[wikilinks]]
    to connected nodes. Optionally includes semantic similarity links.

    Args:
        graph_json: Path to rtfm-graph.json.
        output_dir: Directory to write markdown files into.
        graph_pickle: Optional pickle for faster graph loading.
        index_path: Path to LanceDB index (required if include_semantic=True).
        include_semantic: Whether to add "Similar" sections via vector search.
        max_nodes: Maximum nodes to export (prevents huge vaults).

    Returns:
        Dict with export stats (nodes_exported, files_written, output_dir).
    """
    if include_semantic and index_path is None:
        raise ValueError("--include-semantic requires --index-path")

    if graph_pickle is None:
        graph_pickle = graph_json.with_suffix(".pkl")

    graph, node_index = load_or_rebuild(graph_json, graph_pickle)

    total_nodes = graph.vcount()
    truncated = total_nodes > max_nodes
    if truncated:
        print(
            f"Warning: graph has {total_nodes} nodes, truncating to {max_nodes}",
            file=sys.stderr,
        )

    export_indices = list(range(min(total_nodes, max_nodes)))

    # Semantic neighbors
    semantic_neighbors: dict[str, list[dict]] = {}
    if include_semantic and index_path:
        node_ids = [graph.vs[i]["node_id"] for i in export_indices]
        semantic_neighbors = _get_semantic_neighbors(index_path, node_ids)

    # Clean and recreate output
    if output_dir.is_dir():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    clusters_dir = output_dir / "_clusters"
    clusters_dir.mkdir()

    cluster_members: dict[int, list[int]] = defaultdict(list)
    semantic_links_added = 0

    for idx in export_indices:
        v = graph.vs[idx]
        node_id = v["node_id"]
        node_type = v["node_type"]
        cluster_id = v["cluster_id"] or 0

        in_degree = graph.degree(idx, mode="in")
        out_degree = graph.degree(idx, mode="out")

        cluster_members[cluster_id].append(idx)

        filename = _sanitize_filename(node_id) + ".md"
        fm = _build_frontmatter(node_type, cluster_id, in_degree, out_degree)
        rels = _build_relationships(idx, graph)

        similar_section = ""
        if include_semantic and node_id in semantic_neighbors:
            similar_section = _build_similar_section(
                idx, semantic_neighbors[node_id], graph
            )
            if similar_section:
                semantic_links_added += 1

        content = fm + "\n" + rels + similar_section + "\n"
        filepath = output_dir / filename
        filepath.write_text(content)

    # Cluster indexes
    for cid, members in sorted(cluster_members.items()):
        member_ids = sorted(graph.vs[i]["node_id"] for i in members)
        cluster_file = clusters_dir / f"cluster-{cid}.md"
        lines = [
            "---",
            f"cluster_id: {cid}",
            f"member_count: {len(members)}",
            "---",
            "",
            f"# Cluster {cid}",
            "",
            "## Members",
            "",
        ]
        for mid in member_ids:
            name = _sanitize_filename(mid)
            lines.append(f"- [[{name}]]")
        cluster_file.write_text("\n".join(lines) + "\n")

    # _index.md
    index_lines = [
        "---",
        f"total_nodes: {len(export_indices)}",
        f"total_clusters: {len(cluster_members)}",
        f"truncated: {str(truncated).lower()}",
        "---",
        "",
        "# Code Graph Vault",
        "",
        f"Nodes: {len(export_indices)}",
        f"Clusters: {len(cluster_members)}",
        f"Semantic links: {semantic_links_added}",
        "",
        "## Clusters",
        "",
    ]
    for cid in sorted(cluster_members.keys()):
        index_lines.append(f"- [[_clusters/cluster-{cid}]] ({len(cluster_members[cid])} nodes)")
    (output_dir / "_index.md").write_text("\n".join(index_lines) + "\n")

    return {
        "notes_created": len(export_indices),
        "clusters_created": len(cluster_members),
        "semantic_links_added": semantic_links_added,
        "vault_path": str(output_dir),
        "truncated": truncated,
    }
