"""Reciprocal Rank Fusion (RRF) for merging structural and semantic results."""

from __future__ import annotations

from typing import Any


def rrf_merge(
    structural_results: list[dict[str, Any]],
    semantic_results: list[dict[str, Any]],
    k: int = 60,
    structural_weight: float = 1.0,
    semantic_weight: float = 1.0,
    top_k: int = 10,
) -> list[dict[str, Any]]:
    """Merge two ranked result lists using Reciprocal Rank Fusion.

    RRF_score(d) = sum( weight_i / (k + rank_i(d)) ) for each ranking i where d appears.

    Args:
        structural_results: Ranked results from graph query (must have "node_id" key)
        semantic_results: Ranked results from vector search (must have "node_id" key)
        k: RRF constant (default 60, prevents high-ranked items from dominating)
        structural_weight: Multiplier for structural ranking contribution
        semantic_weight: Multiplier for semantic ranking contribution
        top_k: Maximum results to return

    Returns:
        Merged results sorted by RRF score descending, each with rrf_score,
        structural_rank, and semantic_rank fields.
    """
    scores: dict[str, float] = {}
    structural_ranks: dict[str, int] = {}
    semantic_ranks: dict[str, int] = {}
    node_data: dict[str, dict[str, Any]] = {}

    for rank, item in enumerate(structural_results, 1):
        nid = item.get("node_id", "")
        if not nid:
            continue
        scores[nid] = scores.get(nid, 0) + structural_weight * (1 / (k + rank))
        structural_ranks[nid] = rank
        node_data[nid] = item

    for rank, item in enumerate(semantic_results, 1):
        nid = item.get("node_id", "")
        if not nid:
            continue
        scores[nid] = scores.get(nid, 0) + semantic_weight * (1 / (k + rank))
        semantic_ranks[nid] = rank
        if nid not in node_data:
            node_data[nid] = item

    # Sort by RRF score descending
    sorted_ids = sorted(scores.keys(), key=lambda nid: scores[nid], reverse=True)

    results: list[dict[str, Any]] = []
    for nid in sorted_ids[:top_k]:
        entry = {
            "node_id": nid,
            "source_file": node_data[nid].get("source_file", ""),
            "node_type": node_data[nid].get("node_type", ""),
            "rrf_score": round(scores[nid], 6),
            "structural_rank": structural_ranks.get(nid),
            "semantic_rank": semantic_ranks.get(nid),
        }
        # Carry forward chunk_preview if available
        preview = node_data[nid].get("chunk_preview")
        if preview:
            entry["chunk_preview"] = preview
        results.append(entry)

    return results
