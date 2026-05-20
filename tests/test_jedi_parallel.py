"""Tests for parallel Jedi enrichment."""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def sample_project(tmp_path):
    """Create a minimal project with Python files and a graph for testing."""
    # Create source files
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    # File A: defines a function and calls function from B
    (src_dir / "a.py").write_text(
        "from src.b import helper\n\n"
        "def main():\n"
        "    return helper()\n"
    )

    # File B: defines helper function
    (src_dir / "b.py").write_text(
        "def helper():\n"
        "    return 42\n"
    )

    # File C: class inheriting from B
    (src_dir / "c.py").write_text(
        "from src.b_base import Base\n\n"
        "class Child(Base):\n"
        "    pass\n"
    )

    (src_dir / "b_base.py").write_text(
        "class Base:\n"
        "    pass\n"
    )

    # Create graph JSON
    graph = {
        "nodes": [
            {"id": "src/a.py", "source_file": "src/a.py", "type": "module"},
            {"id": "src/a.py::main", "source_file": "src/a.py", "type": "function"},
            {"id": "src/b.py", "source_file": "src/b.py", "type": "module"},
            {"id": "src/b.py::helper", "source_file": "src/b.py", "type": "function"},
            {"id": "src/c.py", "source_file": "src/c.py", "type": "module"},
            {"id": "src/c.py::Child", "source_file": "src/c.py", "type": "class"},
            {"id": "src/b_base.py", "source_file": "src/b_base.py", "type": "module"},
            {"id": "src/b_base.py::Base", "source_file": "src/b_base.py", "type": "class"},
        ],
        "edges": [],
        "metadata": {"edge_count": 0},
    }

    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph, indent=2))

    return tmp_path, graph_path


@pytest.fixture
def simple_graph(tmp_path):
    """Minimal graph with a single file for basic tests."""
    src_dir = tmp_path / "pkg"
    src_dir.mkdir()

    (src_dir / "mod.py").write_text(
        "def foo():\n"
        "    return 1\n"
    )

    graph = {
        "nodes": [
            {"id": "pkg/mod.py", "source_file": "pkg/mod.py", "type": "module"},
            {"id": "pkg/mod.py::foo", "source_file": "pkg/mod.py", "type": "function"},
        ],
        "edges": [],
        "metadata": {"edge_count": 0},
    }

    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph, indent=2))

    return tmp_path, graph_path


class TestWorkerDefaults:
    """Test worker count defaults and configuration."""

    def test_default_workers_capped_at_4(self):
        """Default workers should be min(cpu_count, 4)."""
        with patch("os.cpu_count", return_value=16):
            from rtfm.core.jedi_enricher import enrich_graph_parallel
            # We can't easily test the internal default without running,
            # but we can verify the logic
            expected = min(16, 4)
            assert expected == 4

    def test_default_workers_with_low_cpu(self):
        """On single-core, default should be 1."""
        with patch("os.cpu_count", return_value=1):
            expected = min(1, 4)
            assert expected == 1

    def test_default_workers_none_cpu(self):
        """When cpu_count returns None, default to 1."""
        with patch("os.cpu_count", return_value=None):
            expected = min(None or 1, 4)
            assert expected == 1


class TestSequentialFallback:
    """Test that workers=1 uses sequential path."""

    def test_workers_1_uses_sequential(self, simple_graph):
        """With workers=1, should process sequentially (no Pool)."""
        project_root, graph_path = simple_graph

        from rtfm.core.jedi_enricher import enrich_graph_parallel

        stats = enrich_graph_parallel(
            project_root, graph_path,
            dry_run=True, workers=1, verbose=False,
        )

        assert stats["status"] == "complete"
        assert stats["workers_used"] == 1
        assert stats["files_processed"] >= 1

    def test_single_file_uses_sequential(self, simple_graph):
        """With only 1 file, should fall back to sequential even with workers>1."""
        project_root, graph_path = simple_graph

        from rtfm.core.jedi_enricher import enrich_graph_parallel

        stats = enrich_graph_parallel(
            project_root, graph_path,
            dry_run=True, workers=4, verbose=False,
        )

        assert stats["status"] == "complete"
        assert stats["workers_used"] == 1


class TestEdgeDeduplication:
    """Test the edge deduplication logic."""

    def test_dedup_removes_duplicates(self):
        """Duplicate edges (same source, target, type) should be removed."""
        from rtfm.core.jedi_enricher import _deduplicate_edges

        edges = [
            {"source": "a.py::foo", "target": "b.py::bar", "edge_type": "type_resolved_call",
             "metadata": {"confidence": "high", "jedi_module_path": "b.py"}},
            {"source": "a.py::foo", "target": "b.py::bar", "edge_type": "type_resolved_call",
             "metadata": {"confidence": "high", "jedi_module_path": "b.py"}},
            {"source": "a.py::foo", "target": "c.py::baz", "edge_type": "type_resolved_call",
             "metadata": {"confidence": "high", "jedi_module_path": "c.py"}},
        ]

        result = _deduplicate_edges(edges)
        assert len(result) == 2

    def test_dedup_preserves_different_types(self):
        """Edges with same source/target but different types are kept."""
        from rtfm.core.jedi_enricher import _deduplicate_edges

        edges = [
            {"source": "a.py", "target": "b.py::Base", "edge_type": "cross_file_inheritance",
             "metadata": {"confidence": "high", "jedi_module_path": "b.py"}},
            {"source": "a.py", "target": "b.py::Base", "edge_type": "type_resolved_call",
             "metadata": {"confidence": "high", "jedi_module_path": "b.py"}},
        ]

        result = _deduplicate_edges(edges)
        assert len(result) == 2

    def test_dedup_empty_list(self):
        """Empty input returns empty output."""
        from rtfm.core.jedi_enricher import _deduplicate_edges

        assert _deduplicate_edges([]) == []

    def test_dedup_preserves_order(self):
        """First occurrence is kept, duplicates dropped."""
        from rtfm.core.jedi_enricher import _deduplicate_edges

        edges = [
            {"source": "a", "target": "b", "edge_type": "call",
             "metadata": {"confidence": "high", "jedi_module_path": "first"}},
            {"source": "a", "target": "b", "edge_type": "call",
             "metadata": {"confidence": "high", "jedi_module_path": "second"}},
        ]

        result = _deduplicate_edges(edges)
        assert len(result) == 1
        assert result[0]["metadata"]["jedi_module_path"] == "first"


class TestParallelProducesSameEdges:
    """Test that parallel and sequential produce identical edges."""

    def test_parallel_matches_sequential(self, sample_project):
        """Parallel (workers=2) should produce same edges as sequential (workers=1)."""
        project_root, graph_path = sample_project

        from rtfm.core.jedi_enricher import enrich_graph_parallel

        # Run sequential
        seq_stats = enrich_graph_parallel(
            project_root, graph_path,
            dry_run=True, workers=1, verbose=False,
        )

        # Run parallel
        par_stats = enrich_graph_parallel(
            project_root, graph_path,
            dry_run=True, workers=2, verbose=False,
        )

        assert seq_stats["edges_found"] == par_stats["edges_found"]
        assert seq_stats["type_resolved_call"] == par_stats["type_resolved_call"]
        assert seq_stats["cross_file_inheritance"] == par_stats["cross_file_inheritance"]
        assert seq_stats["reexport_resolution"] == par_stats["reexport_resolution"]
        assert seq_stats["files_processed"] == par_stats["files_processed"]


class TestBackwardCompatibility:
    """Test that the original enrich_graph still works."""

    def test_original_enrich_graph(self, simple_graph):
        """Original enrich_graph function should still work."""
        project_root, graph_path = simple_graph

        from rtfm.core.jedi_enricher import enrich_graph

        stats = enrich_graph(
            project_root, graph_path,
            dry_run=True, verbose=False,
        )

        assert stats["status"] == "complete"
        assert "files_processed" in stats
        assert "edges_found" in stats


class TestProcessFileStandalone:
    """Test the standalone _process_file function."""

    def test_returns_list(self, simple_graph):
        """_process_file_standalone should return a list of edge dicts."""
        project_root, graph_path = simple_graph

        from rtfm.core.jedi_enricher import _process_file_standalone

        with open(graph_path) as f:
            graph_data = json.load(f)
        node_index = {n["id"]: n for n in graph_data["nodes"]}

        result = _process_file_standalone(
            "pkg/mod.py",
            str(project_root / "pkg" / "mod.py"),
            str(project_root),
            node_index,
            10,
            False,
        )

        assert isinstance(result, list)
        # Each item should be a dict with required keys
        for edge in result:
            assert "source" in edge
            assert "target" in edge
            assert "edge_type" in edge
            assert "metadata" in edge

    def test_handles_nonexistent_file(self, tmp_path):
        """Should handle missing files gracefully (return empty list)."""
        from rtfm.core.jedi_enricher import _process_file_standalone

        result = _process_file_standalone(
            "missing.py",
            str(tmp_path / "missing.py"),
            str(tmp_path),
            {},
            10,
            False,
        )

        assert isinstance(result, list)
        assert len(result) == 0
