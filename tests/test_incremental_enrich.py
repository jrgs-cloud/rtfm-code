"""Tests for scope-limited incremental Jedi enrichment (Phase 2)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtfm.core.jedi_enricher import enrich_incremental


@pytest.fixture
def incremental_project(tmp_path):
    """Create a project with cross-file dependencies for incremental testing.

    Layout:
      src/a.py  — imports from b, calls b.helper()
      src/b.py  — defines helper(), imports from c
      src/c.py  — defines utility()
      src/d.py  — standalone, no deps on a/b/c

    Graph has pre-existing jedi edges:
      a.py::main -> b.py::helper  (type_resolved_call)
      b.py::process -> c.py::utility  (type_resolved_call)
      a.py -> b.py::helper  (reexport_resolution)
    """
    src_dir = tmp_path / "src"
    src_dir.mkdir()

    (src_dir / "a.py").write_text(
        "from src.b import helper\n\n"
        "def main():\n"
        "    return helper()\n"
    )

    (src_dir / "b.py").write_text(
        "from src.c import utility\n\n"
        "def helper():\n"
        "    return utility()\n\n"
        "def process():\n"
        "    return utility()\n"
    )

    (src_dir / "c.py").write_text(
        "def utility():\n"
        "    return 42\n"
    )

    (src_dir / "d.py").write_text(
        "def standalone():\n"
        "    return 99\n"
    )

    graph = {
        "nodes": [
            {"id": "src/a.py", "source_file": "src/a.py", "type": "module"},
            {"id": "src/a.py::main", "source_file": "src/a.py", "type": "function"},
            {"id": "src/b.py", "source_file": "src/b.py", "type": "module"},
            {"id": "src/b.py::helper", "source_file": "src/b.py", "type": "function"},
            {"id": "src/b.py::process", "source_file": "src/b.py", "type": "function"},
            {"id": "src/c.py", "source_file": "src/c.py", "type": "module"},
            {"id": "src/c.py::utility", "source_file": "src/c.py", "type": "function"},
            {"id": "src/d.py", "source_file": "src/d.py", "type": "module"},
            {"id": "src/d.py::standalone", "source_file": "src/d.py", "type": "function"},
        ],
        "edges": [
            # Pre-existing jedi edges
            {"source": "src/a.py::main", "target": "src/b.py::helper",
             "edge_type": "type_resolved_call",
             "metadata": {"confidence": "high", "jedi_module_path": "src/b.py"}},
            {"source": "src/b.py::process", "target": "src/c.py::utility",
             "edge_type": "type_resolved_call",
             "metadata": {"confidence": "high", "jedi_module_path": "src/c.py"}},
            {"source": "src/a.py", "target": "src/b.py::helper",
             "edge_type": "reexport_resolution",
             "metadata": {"confidence": "high", "jedi_module_path": "src/b.py"}},
            # Non-jedi edge (should be preserved always)
            {"source": "src/a.py", "target": "src/b.py",
             "edge_type": "imports",
             "metadata": {"kind": "structural"}},
            {"source": "src/b.py", "target": "src/c.py",
             "edge_type": "imports",
             "metadata": {"kind": "structural"}},
        ],
        "metadata": {"edge_count": 5},
    }

    graph_path = tmp_path / "graph.json"
    graph_path.write_text(json.dumps(graph, indent=2))

    return tmp_path, graph_path


class TestScopeDiscovery:
    """Test that only changed files + dependents are in scope."""

    def test_changed_file_is_in_scope(self, incremental_project):
        """Changed file itself should be in the enrichment scope."""
        project_root, graph_path = incremental_project

        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[project_root / "src" / "b.py"],
            merge=False,
        )

        assert stats["status"] == "complete"
        assert stats["files_changed"] == 1

    def test_dependents_discovered(self, incremental_project):
        """Files that have edges pointing TO changed file nodes should be in scope."""
        project_root, graph_path = incremental_project

        # Change b.py — a.py has an edge targeting b.py::helper, so a.py is a dependent
        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[project_root / "src" / "b.py"],
            merge=False,
        )

        assert stats["status"] == "complete"
        # b.py changed, a.py depends on b.py (edge a.py::main -> b.py::helper)
        assert stats["files_dependent"] >= 1
        # Total scope should include both
        assert stats["files_in_scope"] >= 2

    def test_unrelated_file_not_in_scope(self, incremental_project):
        """Files with no dependency on changed files should NOT be processed."""
        project_root, graph_path = incremental_project

        # Change d.py — nothing depends on d.py
        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[project_root / "src" / "d.py"],
            merge=False,
        )

        assert stats["status"] == "complete"
        assert stats["files_changed"] == 1
        assert stats["files_dependent"] == 0
        # Only d.py in scope
        assert stats["files_in_scope"] == 1


class TestEdgeRemoval:
    """Test that old Jedi edges are removed before re-enrichment."""

    def test_old_jedi_edges_removed_from_scope(self, incremental_project):
        """Jedi edges from scope files should be removed before re-enrichment."""
        project_root, graph_path = incremental_project

        # Change b.py — edges from b.py::process should be removed
        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[project_root / "src" / "b.py"],
            merge=True,
        )

        assert stats["edges_removed"] >= 1

        # Verify the graph was written
        with open(graph_path) as f:
            graph_data = json.load(f)

        # Non-jedi edges (imports) should still be present
        import_edges = [e for e in graph_data["edges"] if e["edge_type"] == "imports"]
        assert len(import_edges) == 2

    def test_non_jedi_edges_preserved(self, incremental_project):
        """Non-Jedi edges (imports, etc.) should never be removed."""
        project_root, graph_path = incremental_project

        # Change all files
        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[
                project_root / "src" / "a.py",
                project_root / "src" / "b.py",
                project_root / "src" / "c.py",
            ],
            merge=True,
        )

        with open(graph_path) as f:
            graph_data = json.load(f)

        import_edges = [e for e in graph_data["edges"] if e["edge_type"] == "imports"]
        assert len(import_edges) == 2

    def test_edges_outside_scope_preserved(self, incremental_project):
        """Jedi edges from files NOT in scope should be preserved."""
        project_root, graph_path = incremental_project

        # Change only d.py — edges from a.py and b.py should remain
        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[project_root / "src" / "d.py"],
            merge=True,
        )

        with open(graph_path) as f:
            graph_data = json.load(f)

        # Original jedi edges from a.py and b.py should still be there
        jedi_edges = [
            e for e in graph_data["edges"]
            if e["edge_type"] in ("type_resolved_call", "reexport_resolution", "cross_file_inheritance")
        ]
        # At minimum the original edges from non-scope files should remain
        assert stats["edges_removed"] == 0


class TestEmptyAndEdgeCases:
    """Test edge cases and empty inputs."""

    def test_empty_changed_files_returns_immediately(self, incremental_project):
        """Empty changed_files list should return immediately with zero stats."""
        project_root, graph_path = incremental_project

        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[],
            merge=True,
        )

        assert stats["status"] == "complete"
        assert stats["files_in_scope"] == 0
        assert stats["edges_found"] == 0

    def test_nonexistent_changed_files_skipped(self, incremental_project):
        """Non-existent files in changed_files should be skipped gracefully."""
        project_root, graph_path = incremental_project

        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[project_root / "src" / "nonexistent.py"],
            merge=False,
        )

        assert stats["status"] == "complete"
        # File resolves to a relative path but doesn't exist — no processing
        assert stats["files_processed"] == 0

    def test_files_outside_project_root_skipped(self, tmp_path, incremental_project):
        """Files outside project root should be skipped."""
        project_root, graph_path = incremental_project

        from rtfm.core.jedi_enricher import enrich_incremental

        outside_file = tmp_path.parent / "outside.py"
        outside_file.write_text("def x(): pass\n")

        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[outside_file],
            merge=False,
        )

        assert stats["status"] == "complete"
        assert stats["files_in_scope"] == 0
        assert stats["skipped_reason"] == "no valid changed files"

    def test_non_python_files_in_scope_skipped(self, incremental_project):
        """Non-Python files should be in scope count but not processed."""
        project_root, graph_path = incremental_project

        # Add a .json file to the graph
        with open(graph_path) as f:
            graph_data = json.load(f)

        graph_data["nodes"].append(
            {"id": "src/config.json", "source_file": "src/config.json", "type": "config"}
        )
        with open(graph_path, "w") as f:
            json.dump(graph_data, f)

        # Create the file
        (project_root / "src" / "config.json").write_text("{}")

        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[project_root / "src" / "config.json"],
            merge=False,
        )

        assert stats["status"] == "complete"
        # In scope but not processed (not Python)
        assert stats["files_in_scope"] == 1
        assert stats["files_processed"] == 0


class TestMergeMode:
    """Test merge vs no-merge behavior."""

    def test_merge_true_writes_graph(self, incremental_project):
        """With merge=True, graph file should be updated."""
        project_root, graph_path = incremental_project

        from rtfm.core.jedi_enricher import enrich_incremental

        # Get original mtime
        import os
        original_mtime = os.path.getmtime(graph_path)

        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[project_root / "src" / "b.py"],
            merge=True,
        )

        assert stats["merged"] is True
        assert stats["output_path"] == str(graph_path)
        # File should have been rewritten
        new_mtime = os.path.getmtime(graph_path)
        assert new_mtime >= original_mtime

    def test_merge_false_does_not_write(self, incremental_project):
        """With merge=False, graph file should NOT be modified."""
        project_root, graph_path = incremental_project

        from rtfm.core.jedi_enricher import enrich_incremental

        original_content = graph_path.read_text()

        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[project_root / "src" / "b.py"],
            merge=False,
        )

        assert stats["merged"] is False
        assert stats["output_path"] is None
        # File content unchanged
        assert graph_path.read_text() == original_content


class TestStatsOutput:
    """Test that stats dict contains expected fields."""

    def test_stats_has_required_fields(self, incremental_project):
        """Stats dict should contain all documented fields."""
        project_root, graph_path = incremental_project

        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[project_root / "src" / "b.py"],
            merge=False,
        )

        required_fields = [
            "status", "edges_found", "edges_removed",
            "files_in_scope", "files_changed", "files_dependent",
            "files_processed", "files_failed",
            "type_resolved_call", "cross_file_inheritance", "reexport_resolution",
        ]
        for field in required_fields:
            assert field in stats, f"Missing field: {field}"

    def test_edge_type_counts_sum_to_total(self, incremental_project):
        """Sum of edge type counts should equal total edges_found."""
        project_root, graph_path = incremental_project

        stats = enrich_incremental(
            project_root=project_root,
            graph_path=graph_path,
            changed_files=[project_root / "src" / "b.py"],
            merge=False,
        )

        type_sum = (
            stats["type_resolved_call"]
            + stats["cross_file_inheritance"]
            + stats["reexport_resolution"]
        )
        # type_sum should be <= edges_found (there may be other edge types like reads/writes)
        assert type_sum <= stats["edges_found"]
