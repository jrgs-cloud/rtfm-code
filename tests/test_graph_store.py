"""Tests for graph_store.py — loading, persistence, query helpers."""

from __future__ import annotations

import json
import pickle
from pathlib import Path

import pytest

from rtfm.core.graph_store import (
    _parse_json_graph,
    _rebuild_node_index,
    build_pickle,
    edge_to_dict,
    find_by_name,
    find_vertex,
    kb_miss,
    load_or_rebuild,
    load_pickle,
    matches_project,
    search_nodes,
    vertex_to_dict,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_graph_json(tmp_path):
    """Create a minimal graph JSON file for testing."""
    data = {
        "nodes": [
            {
                "id": "src/app.py",
                "node_type": "ModuleNode",
                "cluster_id": 1,
                "source_file": "src/app.py",
                "last_updated": "2026-01-01T00:00:00",
                "checksum": "abc123",
            },
            {
                "id": "src/app.py::main",
                "node_type": "FunctionNode",
                "cluster_id": 1,
                "source_file": "src/app.py",
                "last_updated": "2026-01-01T00:00:00",
                "checksum": "abc123",
                "name": "main",
            },
            {
                "id": "src/handler.py",
                "node_type": "ModuleNode",
                "cluster_id": 2,
                "source_file": "src/handler.py",
                "last_updated": "2026-01-01T00:00:00",
                "checksum": "def456",
                "project": "my-project",
            },
        ],
        "edges": [
            {
                "source": "src/app.py",
                "target": "src/handler.py",
                "edge_type": "imports",
                "metadata": {"line": 3},
            },
            {
                "source": "src/app.py::main",
                "target": "src/handler.py",
                "edge_type": "calls",
                "metadata": {},
            },
            {
                "source": "nonexistent",
                "target": "src/app.py",
                "edge_type": "imports",
                "metadata": {},
            },
        ],
    }
    json_path = tmp_path / "graph.json"
    json_path.write_text(json.dumps(data))
    return json_path


@pytest.fixture
def loaded_graph(sample_graph_json):
    """Parse the sample graph JSON and return (graph, node_index)."""
    return _parse_json_graph(sample_graph_json)


# ---------------------------------------------------------------------------
# _parse_json_graph
# ---------------------------------------------------------------------------


class TestParseJsonGraph:
    def test_parses_nodes(self, loaded_graph):
        g, node_index = loaded_graph
        assert g.vcount() == 3
        assert "src/app.py" in node_index
        assert "src/app.py::main" in node_index
        assert "src/handler.py" in node_index

    def test_parses_valid_edges_only(self, loaded_graph):
        g, _ = loaded_graph
        # 3 edges in JSON, but one has nonexistent source — only 2 valid
        assert g.ecount() == 2

    def test_edge_types_assigned(self, loaded_graph):
        g, _ = loaded_graph
        types = g.es["edge_type"]
        assert "imports" in types
        assert "calls" in types

    def test_vertex_attributes(self, loaded_graph):
        g, node_index = loaded_graph
        idx = node_index["src/app.py"]
        assert g.vs[idx]["node_id"] == "src/app.py"
        assert g.vs[idx]["node_type"] == "ModuleNode"
        assert g.vs[idx]["cluster_id"] == 1

    def test_empty_nodes_raises(self, tmp_path):
        json_path = tmp_path / "empty.json"
        json_path.write_text(json.dumps({"nodes": [], "edges": []}))
        with pytest.raises(ValueError, match="No nodes found"):
            _parse_json_graph(json_path)


# ---------------------------------------------------------------------------
# Pickle persistence
# ---------------------------------------------------------------------------


class TestPicklePersistence:
    def test_build_pickle_creates_file(self, sample_graph_json, tmp_path):
        pickle_path = tmp_path / "cache" / "graph.pkl"
        g, node_index = build_pickle(sample_graph_json, pickle_path)
        assert pickle_path.is_file()
        assert g.vcount() == 3
        assert len(node_index) == 3

    def test_load_pickle_roundtrip(self, sample_graph_json, tmp_path):
        pickle_path = tmp_path / "graph.pkl"
        build_pickle(sample_graph_json, pickle_path)
        g, node_index = load_pickle(pickle_path)
        assert g.vcount() == 3
        assert "src/app.py" in node_index

    def test_load_or_rebuild_uses_pickle_when_fresh(self, sample_graph_json, tmp_path):
        pickle_path = tmp_path / "graph.pkl"
        build_pickle(sample_graph_json, pickle_path)
        # Pickle is newer than JSON — should use pickle
        g, node_index = load_or_rebuild(sample_graph_json, pickle_path)
        assert g.vcount() == 3

    def test_load_or_rebuild_rebuilds_when_stale(self, sample_graph_json, tmp_path):
        pickle_path = tmp_path / "graph.pkl"
        build_pickle(sample_graph_json, pickle_path)
        # Touch JSON to make it newer
        import os
        import time

        time.sleep(0.01)
        os.utime(sample_graph_json, None)
        g, node_index = load_or_rebuild(sample_graph_json, pickle_path)
        assert g.vcount() == 3

    def test_load_or_rebuild_raises_when_no_json(self, tmp_path):
        json_path = tmp_path / "missing.json"
        pickle_path = tmp_path / "missing.pkl"
        with pytest.raises(FileNotFoundError, match="Graph JSON not found"):
            load_or_rebuild(json_path, pickle_path)


# ---------------------------------------------------------------------------
# _rebuild_node_index
# ---------------------------------------------------------------------------


class TestRebuildNodeIndex:
    def test_rebuilds_from_graph(self, loaded_graph):
        g, original_index = loaded_graph
        rebuilt = _rebuild_node_index(g)
        assert rebuilt == original_index


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_kb_miss_default(self):
        result = kb_miss()
        assert result["results"] == []
        assert result["result_count"] == 0
        assert result["kb_miss"] is True
        assert result["message"] == "No results found"

    def test_kb_miss_custom_message(self):
        result = kb_miss("Custom error")
        assert result["message"] == "Custom error"

    def test_matches_project_by_attrs(self, loaded_graph):
        g, node_index = loaded_graph
        idx = node_index["src/handler.py"]
        v = g.vs[idx]
        assert matches_project(v, "my-project") is True
        assert matches_project(v, "other-project") is False

    def test_matches_project_by_source_file(self, loaded_graph):
        """Vertex with projects/X in source_file matches project X."""
        g, node_index = loaded_graph
        # Modify source_file to test path matching
        idx = node_index["src/app.py"]
        g.vs[idx]["source_file"] = "projects/test-proj/src/app.py"
        v = g.vs[idx]
        assert matches_project(v, "test-proj") is True


# ---------------------------------------------------------------------------
# Query functions
# ---------------------------------------------------------------------------


class TestQueryFunctions:
    def test_find_vertex_exact(self, loaded_graph):
        g, node_index = loaded_graph
        v = find_vertex(node_index, g, "src/app.py")
        assert v is not None
        assert v["node_id"] == "src/app.py"

    def test_find_vertex_missing(self, loaded_graph):
        g, node_index = loaded_graph
        v = find_vertex(node_index, g, "nonexistent")
        assert v is None

    def test_find_by_name_exact(self, loaded_graph):
        g, node_index = loaded_graph
        v = find_by_name(g, node_index, "src/app.py")
        assert v is not None
        assert v["node_id"] == "src/app.py"

    def test_find_by_name_substring(self, loaded_graph):
        g, node_index = loaded_graph
        v = find_by_name(g, node_index, "handler")
        assert v is not None
        assert v["node_id"] == "src/handler.py"

    def test_find_by_name_attr_match(self, loaded_graph):
        g, node_index = loaded_graph
        v = find_by_name(g, node_index, "main")
        assert v is not None
        assert v["node_id"] == "src/app.py::main"

    def test_find_by_name_not_found(self, loaded_graph):
        g, node_index = loaded_graph
        v = find_by_name(g, node_index, "zzz_nonexistent_zzz")
        assert v is None

    def test_search_nodes_basic(self, loaded_graph):
        g, node_index = loaded_graph
        results = search_nodes(g, node_index, "app")
        assert len(results) >= 1
        ids = [v["node_id"] for v in results]
        assert "src/app.py" in ids

    def test_search_nodes_max_results(self, loaded_graph):
        g, node_index = loaded_graph
        results = search_nodes(g, node_index, "src", max_results=1)
        assert len(results) == 1

    def test_search_nodes_project_filter(self, loaded_graph):
        g, node_index = loaded_graph
        results = search_nodes(g, node_index, "src", project="my-project")
        ids = [v["node_id"] for v in results]
        assert "src/handler.py" in ids
        assert "src/app.py" not in ids

    def test_vertex_to_dict(self, loaded_graph):
        g, node_index = loaded_graph
        v = g.vs[node_index["src/app.py::main"]]
        d = vertex_to_dict(v)
        assert d["node_id"] == "src/app.py::main"
        assert d["node_type"] == "FunctionNode"
        assert d["cluster_id"] == 1
        assert d["source_file"] == "src/app.py"
        assert d["name"] == "main"

    def test_edge_to_dict(self, loaded_graph):
        g, _ = loaded_graph
        e = g.es[0]
        d = edge_to_dict(g, e)
        assert d["edge_type"] == "imports"
        assert d["source"] == "src/app.py"
        assert d["target"] == "src/handler.py"
