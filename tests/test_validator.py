"""Tests for runtime validator — graph vs coverage comparison."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from rtfm.core.validator import (
    validate,
    _load_coverage_json,
    _load_coverage_python,
    _numbits_to_lines,
    _relativize_paths,
)


@pytest.fixture
def graph_json(tmp_path):
    """Create a graph JSON with nodes and edges."""
    nodes = [
        {"id": "src/app.py", "node_type": "ModuleNode", "source_file": "src/app.py",
         "cluster_id": 0, "last_updated": "", "checksum": ""},
        {"id": "src/app.py::handle", "node_type": "FunctionNode", "source_file": "src/app.py",
         "cluster_id": 0, "last_updated": "", "checksum": ""},
        {"id": "src/utils.py", "node_type": "ModuleNode", "source_file": "src/utils.py",
         "cluster_id": 0, "last_updated": "", "checksum": ""},
        {"id": "src/utils.py::helper", "node_type": "FunctionNode", "source_file": "src/utils.py",
         "cluster_id": 0, "last_updated": "", "checksum": ""},
        {"id": "src/cache.py", "node_type": "ModuleNode", "source_file": "src/cache.py",
         "cluster_id": 1, "last_updated": "", "checksum": ""},
        {"id": "src/cache.py::invalidate", "node_type": "FunctionNode", "source_file": "src/cache.py",
         "cluster_id": 1, "last_updated": "", "checksum": ""},
    ]
    edges = [
        {"source": "src/app.py::handle", "target": "src/utils.py::helper",
         "edge_type": "calls", "metadata": {}},
        {"source": "src/app.py::handle", "target": "src/cache.py::invalidate",
         "edge_type": "calls", "metadata": {}},
        {"source": "src/utils.py::helper", "target": "src/cache.py::invalidate",
         "edge_type": "calls", "metadata": {}},
        {"source": "src/app.py", "target": "src/utils.py",
         "edge_type": "imports", "metadata": {}},
        # Structural edge (should be skipped)
        {"source": "src/app.py", "target": "src/app.py::handle",
         "edge_type": "contains", "metadata": {}},
    ]
    data = {"nodes": nodes, "edges": edges, "metadata": {"node_count": 6, "edge_count": 5}}
    graph_path = tmp_path / "rtfm-graph.json"
    graph_path.write_text(json.dumps(data))
    return graph_path


@pytest.fixture
def coverage_json(tmp_path):
    """Create an Istanbul-format coverage JSON."""
    data = {
        "src/app.py": {
            "statementMap": {
                "0": {"start": {"line": 1}, "end": {"line": 1}},
                "1": {"start": {"line": 5}, "end": {"line": 10}},
            },
            "s": {"0": 1, "1": 3},
        },
        "src/utils.py": {
            "statementMap": {
                "0": {"start": {"line": 1}, "end": {"line": 1}},
                "1": {"start": {"line": 3}, "end": {"line": 5}},
            },
            "s": {"0": 1, "1": 2},
        },
    }
    cov_path = tmp_path / "coverage-final.json"
    cov_path.write_text(json.dumps(data))
    return cov_path


@pytest.fixture
def coverage_sqlite(tmp_path):
    """Create a coverage.py SQLite database."""
    cov_path = tmp_path / ".coverage"
    conn = sqlite3.connect(str(cov_path))
    conn.execute("CREATE TABLE file (id INTEGER PRIMARY KEY, path TEXT)")
    conn.execute("CREATE TABLE line_bits (file_id INTEGER, numbits BLOB)")
    conn.execute("INSERT INTO file VALUES (1, '/project/src/app.py')")
    conn.execute("INSERT INTO file VALUES (2, '/project/src/utils.py')")
    # Lines 1-8 covered for app.py
    numbits_app = bytes([0xFF])  # bits 0-7 set
    # Lines 1-4 covered for utils.py
    numbits_utils = bytes([0x0F])  # bits 0-3 set
    conn.execute("INSERT INTO line_bits VALUES (1, ?)", (numbits_app,))
    conn.execute("INSERT INTO line_bits VALUES (2, ?)", (numbits_utils,))
    conn.commit()
    conn.close()
    return cov_path


class TestValidate:
    def test_validates_edges_with_coverage(self, graph_json, coverage_json, tmp_path):
        """Edges between covered files are validated."""
        report = validate(graph_json, coverage_json)
        # app.py and utils.py are covered, cache.py is not
        # app→utils edges are validated, app→cache and utils→cache are unvalidated
        assert report["validated_edges"] >= 1
        assert report["coverage_ratio"] > 0

    def test_identifies_phantom_edges(self, graph_json, tmp_path):
        """Edges where neither file has coverage are phantoms."""
        # Empty coverage — no files covered
        empty_cov = tmp_path / "empty.json"
        empty_cov.write_text(json.dumps({}))
        report = validate(graph_json, empty_cov)
        assert report["phantom_edges"] > 0

    def test_skips_structural_edges(self, graph_json, coverage_json):
        """Contains/documents edges are not counted."""
        report = validate(graph_json, coverage_json)
        total = report["validated_edges"] + report["unvalidated_edges"] + report["phantom_edges"]
        # 4 runtime edges (3 calls + 1 imports), 1 contains skipped
        assert total == 4

    def test_finds_blind_spots(self, tmp_path):
        """Files with coverage but no graph nodes are blind spots."""
        # Graph with only app.py
        nodes = [{"id": "src/app.py", "node_type": "ModuleNode", "source_file": "src/app.py",
                  "cluster_id": 0, "last_updated": "", "checksum": ""}]
        edges = []
        graph_path = tmp_path / "g.json"
        graph_path.write_text(json.dumps({"nodes": nodes, "edges": edges}))

        # Coverage includes extra file
        cov = tmp_path / "cov.json"
        cov.write_text(json.dumps({
            "src/app.py": {"statementMap": {"0": {"start": {"line": 1}, "end": {"line": 1}}}, "s": {"0": 1}},
            "src/secret.py": {"statementMap": {"0": {"start": {"line": 1}, "end": {"line": 1}}}, "s": {"0": 1}},
        }))

        report = validate(graph_path, cov)
        blind_files = [b["file"] for b in report["blind_spots"]]
        assert "src/secret.py" in blind_files

    def test_missing_graph_raises(self, tmp_path, coverage_json):
        """Raises FileNotFoundError if graph doesn't exist."""
        with pytest.raises(FileNotFoundError, match="Graph not found"):
            validate(tmp_path / "nonexistent.json", coverage_json)

    def test_missing_coverage_raises(self, graph_json, tmp_path):
        """Raises FileNotFoundError if coverage file doesn't exist."""
        with pytest.raises(FileNotFoundError, match="Coverage file not found"):
            validate(graph_json, tmp_path / "nonexistent.coverage")


class TestLoadCoverageJson:
    def test_loads_istanbul_format(self, coverage_json):
        result = _load_coverage_json(coverage_json)
        assert "src/app.py" in result
        assert 1 in result["src/app.py"]
        assert 5 in result["src/app.py"]

    def test_skips_zero_hit_statements(self, tmp_path):
        cov = tmp_path / "cov.json"
        cov.write_text(json.dumps({
            "src/a.py": {
                "statementMap": {
                    "0": {"start": {"line": 1}, "end": {"line": 1}},
                    "1": {"start": {"line": 5}, "end": {"line": 5}},
                },
                "s": {"0": 0, "1": 1},
            },
        }))
        result = _load_coverage_json(cov)
        assert 1 not in result["src/a.py"]
        assert 5 in result["src/a.py"]


class TestLoadCoveragePython:
    def test_loads_sqlite_format(self, coverage_sqlite):
        result = _load_coverage_python(coverage_sqlite)
        assert "/project/src/app.py" in result
        assert len(result["/project/src/app.py"]) == 8  # bits 0-7

    def test_invalid_db_raises(self, tmp_path):
        bad = tmp_path / ".coverage"
        conn = sqlite3.connect(str(bad))
        conn.execute("CREATE TABLE other (id INTEGER)")
        conn.commit()
        conn.close()
        with pytest.raises(ValueError, match="Not a valid coverage.py database"):
            _load_coverage_python(bad)


class TestNumbitsToLines:
    def test_single_byte(self):
        # 0b00000101 = lines 0 and 2
        lines = _numbits_to_lines(bytes([0x05]))
        assert lines == {0, 2}

    def test_multi_byte(self):
        # byte 0: 0xFF (lines 0-7), byte 1: 0x01 (line 8)
        lines = _numbits_to_lines(bytes([0xFF, 0x01]))
        assert 0 in lines
        assert 7 in lines
        assert 8 in lines
        assert 9 not in lines


class TestRelativizePaths:
    def test_strips_project_root(self):
        coverage = {"/home/user/project/src/app.py": {1, 2, 3}}
        result = _relativize_paths(coverage, Path("/home/user/project"))
        assert "src/app.py" in result
        assert "/home/user/project/src/app.py" not in result

    def test_skips_paths_outside_root(self):
        coverage = {"/other/path/file.py": {1}}
        result = _relativize_paths(coverage, Path("/home/user/project"))
        assert len(result) == 0

    def test_keeps_relative_paths(self):
        coverage = {"src/app.py": {1, 2}}
        result = _relativize_paths(coverage, Path("/home/user/project"))
        assert "src/app.py" in result
