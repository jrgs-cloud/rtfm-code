"""Tests for fusion.py — Reciprocal Rank Fusion."""

from __future__ import annotations

import pytest

from rtfm.core.fusion import rrf_merge


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _node(node_id: str, source_file: str = "a.py", node_type: str = "FunctionNode", **extra):
    """Build a minimal result dict with required keys."""
    return {"node_id": node_id, "source_file": source_file, "node_type": node_type, **extra}


def _expected_score(rank: int, k: int = 60, weight: float = 1.0) -> float:
    """Compute the raw RRF contribution for one list at a given rank (unrounded).

    Callers must round to 6 dp before comparing against rrf_score, because
    rrf_merge accumulates raw floats and rounds the final sum — not each term.
    """
    return weight * (1 / (k + rank))


# ---------------------------------------------------------------------------
# RRF score calculation
# ---------------------------------------------------------------------------

class TestRRFScoreCalculation:
    def test_rank1_default_k_score(self):
        """rank-1 node with default k=60 → 1/61."""
        result = rrf_merge([_node("a")], [], k=60)
        assert result[0]["rrf_score"] == round(_expected_score(1, k=60), 6)

    def test_rank2_default_k_score(self):
        """rank-2 node with default k=60 → 1/62."""
        result = rrf_merge([_node("a"), _node("b")], [], k=60)
        assert result[1]["rrf_score"] == round(_expected_score(2, k=60), 6)

    def test_score_rounded_to_6_decimal_places(self):
        result = rrf_merge([_node("a")], [], k=60)
        score = result[0]["rrf_score"]
        assert score == round(score, 6)

    def test_node_in_both_lists_accumulates_scores(self):
        """Score = structural contribution + semantic contribution."""
        result = rrf_merge([_node("a")], [_node("a")], k=60)
        expected = round(_expected_score(1, k=60) + _expected_score(1, k=60), 6)
        assert result[0]["rrf_score"] == expected

    def test_node_in_both_lists_different_ranks(self):
        """Contributions use each list's rank independently."""
        structural = [_node("x"), _node("a")]   # a at structural rank 2
        semantic = [_node("a"), _node("x")]      # a at semantic rank 1
        result = rrf_merge(structural, semantic, k=60)
        a = next(r for r in result if r["node_id"] == "a")
        expected = round(_expected_score(2, k=60) + _expected_score(1, k=60), 6)
        assert a["rrf_score"] == expected

    def test_structural_weight_scales_contribution(self):
        result = rrf_merge([_node("a")], [], k=60, structural_weight=2.0)
        assert result[0]["rrf_score"] == round(_expected_score(1, k=60, weight=2.0), 6)

    def test_semantic_weight_scales_contribution(self):
        result = rrf_merge([], [_node("a")], k=60, semantic_weight=3.0)
        assert result[0]["rrf_score"] == round(_expected_score(1, k=60, weight=3.0), 6)

    def test_weights_affect_relative_ranking(self):
        structural = [_node("a")]
        semantic = [_node("b")]
        # Heavy structural weight → structural-only node wins
        result = rrf_merge(structural, semantic, structural_weight=10.0, semantic_weight=1.0)
        assert result[0]["node_id"] == "a"
        # Heavy semantic weight → semantic-only node wins
        result = rrf_merge(structural, semantic, structural_weight=1.0, semantic_weight=10.0)
        assert result[0]["node_id"] == "b"


# ---------------------------------------------------------------------------
# Merging structural and semantic result lists
# ---------------------------------------------------------------------------

class TestMerging:
    def test_structural_only_nodes_present(self):
        result = rrf_merge([_node("s1"), _node("s2")], [])
        ids = {r["node_id"] for r in result}
        assert ids == {"s1", "s2"}

    def test_semantic_only_nodes_present(self):
        result = rrf_merge([], [_node("v1"), _node("v2")])
        ids = {r["node_id"] for r in result}
        assert ids == {"v1", "v2"}

    def test_nodes_from_both_lists_all_present(self):
        structural = [_node("s1"), _node("shared")]
        semantic = [_node("shared"), _node("v1")]
        result = rrf_merge(structural, semantic)
        ids = {r["node_id"] for r in result}
        assert ids == {"s1", "shared", "v1"}

    def test_result_carries_source_file_and_node_type(self):
        result = rrf_merge([_node("a", source_file="src/foo.py", node_type="ClassNode")], [])
        assert result[0]["source_file"] == "src/foo.py"
        assert result[0]["node_type"] == "ClassNode"

    def test_chunk_preview_carried_forward(self):
        result = rrf_merge([_node("a", chunk_preview="def foo(): ...")], [])
        assert result[0].get("chunk_preview") == "def foo(): ..."

    def test_chunk_preview_absent_when_not_in_source(self):
        result = rrf_merge([_node("a")], [])
        assert "chunk_preview" not in result[0]

    def test_structural_metadata_takes_priority_for_shared_node(self):
        """When a node appears in both lists, structural item's metadata is used."""
        structural = [_node("shared", source_file="struct.py", node_type="FunctionNode")]
        semantic = [_node("shared", source_file="semantic.py", node_type="ModuleNode")]
        result = rrf_merge(structural, semantic)
        shared = next(r for r in result if r["node_id"] == "shared")
        assert shared["source_file"] == "struct.py"
        assert shared["node_type"] == "FunctionNode"

    def test_items_missing_node_id_key_are_skipped(self):
        structural = [{"source_file": "x.py"}, _node("valid")]
        result = rrf_merge(structural, [])
        assert len(result) == 1
        assert result[0]["node_id"] == "valid"

    def test_items_with_empty_node_id_are_skipped(self):
        structural = [{"node_id": "", "source_file": "x.py"}, _node("valid")]
        result = rrf_merge(structural, [])
        assert len(result) == 1
        assert result[0]["node_id"] == "valid"

    def test_result_entries_have_all_required_fields(self):
        result = rrf_merge([_node("a")], [])
        r = result[0]
        for field in ("node_id", "source_file", "node_type", "rrf_score", "structural_rank", "semantic_rank"):
            assert field in r, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

class TestDeduplication:
    def test_shared_node_appears_exactly_once(self):
        result = rrf_merge([_node("dup")], [_node("dup")])
        assert len(result) == 1

    def test_all_duplicates_each_appears_once(self):
        nodes = [_node(f"n{i}") for i in range(5)]
        result = rrf_merge(nodes, list(nodes))
        ids = [r["node_id"] for r in result]
        assert len(ids) == len(set(ids))

    def test_duplicate_node_has_both_ranks_populated(self):
        structural = [_node("shared"), _node("s_only")]
        semantic = [_node("v_only"), _node("shared")]
        result = rrf_merge(structural, semantic)
        shared = next(r for r in result if r["node_id"] == "shared")
        assert shared["structural_rank"] == 1
        assert shared["semantic_rank"] == 2

    def test_structural_only_node_has_none_semantic_rank(self):
        result = rrf_merge([_node("s")], [])
        assert result[0]["structural_rank"] == 1
        assert result[0]["semantic_rank"] is None

    def test_semantic_only_node_has_none_structural_rank(self):
        result = rrf_merge([], [_node("v")])
        assert result[0]["structural_rank"] is None
        assert result[0]["semantic_rank"] == 1

    def test_duplicate_node_scores_higher_than_single_list_node(self):
        """A node in both lists must outscore a node in only one list."""
        structural = [_node("a"), _node("both")]
        semantic = [_node("both"), _node("b")]
        result = rrf_merge(structural, semantic)
        assert result[0]["node_id"] == "both"


# ---------------------------------------------------------------------------
# Ranking order
# ---------------------------------------------------------------------------

class TestRankingOrder:
    def test_rank1_node_appears_first(self):
        result = rrf_merge([_node("first"), _node("second")], [])
        assert result[0]["node_id"] == "first"

    def test_scores_are_strictly_non_increasing(self):
        structural = [_node(f"s{i}") for i in range(5)]
        semantic = [_node(f"v{i}") for i in range(5)]
        result = rrf_merge(structural, semantic)
        scores = [r["rrf_score"] for r in result]
        assert scores == sorted(scores, reverse=True)

    def test_top_k_limits_output_count(self):
        structural = [_node(f"n{i}") for i in range(20)]
        result = rrf_merge(structural, [], top_k=5)
        assert len(result) == 5

    def test_top_k_returns_highest_scoring_nodes(self):
        """top_k=1 must return the rank-1 node, not any other."""
        structural = [_node("best"), _node("second"), _node("third")]
        result = rrf_merge(structural, [], top_k=1)
        assert result[0]["node_id"] == "best"

    def test_default_top_k_is_10(self):
        structural = [_node(f"n{i}") for i in range(15)]
        result = rrf_merge(structural, [])
        assert len(result) == 10

    def test_node_in_both_lists_outranks_node_in_one_list(self):
        structural = [_node("both"), _node("struct_only")]
        semantic = [_node("both"), _node("sem_only")]
        result = rrf_merge(structural, semantic)
        assert result[0]["node_id"] == "both"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_both_lists_empty_returns_empty(self):
        assert rrf_merge([], []) == []

    def test_structural_empty_semantic_populated(self):
        semantic = [_node("v1"), _node("v2")]
        result = rrf_merge([], semantic)
        assert len(result) == 2
        assert result[0]["node_id"] == "v1"

    def test_semantic_empty_structural_populated(self):
        structural = [_node("s1"), _node("s2")]
        result = rrf_merge(structural, [])
        assert len(result) == 2
        assert result[0]["node_id"] == "s1"

    def test_single_item_each_list_no_overlap(self):
        result = rrf_merge([_node("s")], [_node("v")])
        assert len(result) == 2

    def test_single_item_each_list_full_overlap(self):
        result = rrf_merge([_node("x")], [_node("x")])
        assert len(result) == 1

    def test_top_k_larger_than_result_count_returns_all(self):
        result = rrf_merge([_node("a"), _node("b")], [], top_k=100)
        assert len(result) == 2

    def test_top_k_zero_returns_empty(self):
        result = rrf_merge([_node("a")], [_node("b")], top_k=0)
        assert result == []

    def test_all_items_missing_node_id_returns_empty(self):
        structural = [{"source_file": "x.py"}, {"source_file": "y.py"}]
        result = rrf_merge(structural, [])
        assert result == []

    def test_single_list_single_item_complete_output(self):
        result = rrf_merge([_node("a", source_file="f.py", node_type="ClassNode")], [])
        r = result[0]
        assert r["node_id"] == "a"
        assert r["source_file"] == "f.py"
        assert r["node_type"] == "ClassNode"
        assert isinstance(r["rrf_score"], float)
        assert r["structural_rank"] == 1
        assert r["semantic_rank"] is None

    def test_large_structural_list_no_semantic(self):
        structural = [_node(f"n{i}") for i in range(100)]
        result = rrf_merge(structural, [], top_k=10)
        assert len(result) == 10
        assert result[0]["node_id"] == "n0"


# ---------------------------------------------------------------------------
# k parameter effect on scoring
# ---------------------------------------------------------------------------

class TestKParameter:
    def test_smaller_k_produces_higher_score_at_rank1(self):
        """Lower k → larger 1/(k+rank) → higher score."""
        result_small = rrf_merge([_node("a")], [], k=1)
        result_large = rrf_merge([_node("a")], [], k=60)
        assert result_small[0]["rrf_score"] > result_large[0]["rrf_score"]

    def test_larger_k_compresses_score_gap_between_ranks(self):
        """With large k, rank-1 and rank-2 scores converge."""
        def score_gap(k: int) -> float:
            result = rrf_merge([_node("first"), _node("second")], [], k=k)
            return result[0]["rrf_score"] - result[1]["rrf_score"]

        assert score_gap(1) > score_gap(60) > score_gap(600)

    def test_k_does_not_change_relative_order_within_single_list(self):
        """Changing k must not reorder results from a single ranked list."""
        nodes = [_node(f"n{i}") for i in range(5)]
        expected_order = [f"n{i}" for i in range(5)]
        for k in (1, 10, 60, 600):
            result = rrf_merge(nodes, [], k=k)
            ids = [r["node_id"] for r in result]
            assert ids == expected_order, f"Order changed at k={k}"

    def test_k_1_rank1_score_equals_half(self):
        """k=1, rank=1 → 1/(1+1) = 0.5."""
        result = rrf_merge([_node("a")], [], k=1)
        assert result[0]["rrf_score"] == round(1 / 2, 6)

    def test_k_affects_both_lists_uniformly(self):
        """Node at rank 1 in both lists with k=10 → 2 * (1/11)."""
        result = rrf_merge([_node("a")], [_node("a")], k=10)
        expected = round(2 * (1 / 11), 6)
        assert result[0]["rrf_score"] == expected

    def test_k_60_rank1_score_matches_formula(self):
        """Explicit formula check: 1/(60+1) = 1/61."""
        result = rrf_merge([_node("a")], [], k=60)
        assert result[0]["rrf_score"] == round(1 / 61, 6)
