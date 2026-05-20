"""Tests for dark-spots suggestions."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from rtfm.core.suggestions import (
    generate_suggestions,
    _suggest_test,
    _suggest_docstrings,
    _suggest_orphan,
    _suggest_fan_out,
    _suggest_coupling,
    _to_class_name,
)


def _make_mock_graph():
    """Create a mock graph with modules, functions, and edges."""
    vertices = []
    edges = []

    # Module: src/utils.py (index 0)
    v0 = MagicMock()
    v0.__getitem__ = lambda self, k: {
        "node_type": "ModuleNode", "source_file": "src/utils.py",
        "node_id": "src/utils.py", "cluster_id": 0, "attrs": {},
    }[k]
    v0.index = 0
    vertices.append(v0)

    # Function: src/utils.py::parse_config (index 1)
    v1 = MagicMock()
    v1.__getitem__ = lambda self, k: {
        "node_type": "FunctionNode", "source_file": "src/utils.py",
        "node_id": "src/utils.py::parse_config", "cluster_id": 0,
        "attrs": {"params": ["path", "strict"]},
    }[k]
    v1.index = 1
    vertices.append(v1)

    # Function: src/utils.py::validate (index 2)
    v2 = MagicMock()
    v2.__getitem__ = lambda self, k: {
        "node_type": "FunctionNode", "source_file": "src/utils.py",
        "node_id": "src/utils.py::validate", "cluster_id": 0,
        "attrs": {"params": ["data"]},
    }[k]
    v2.index = 2
    vertices.append(v2)

    # Module: src/app.py (index 3) - same cluster
    v3 = MagicMock()
    v3.__getitem__ = lambda self, k: {
        "node_type": "ModuleNode", "source_file": "src/app.py",
        "node_id": "src/app.py", "cluster_id": 0, "attrs": {},
    }[k]
    v3.index = 3
    vertices.append(v3)

    # Module: src/orphan.py (index 4) - different cluster
    v4 = MagicMock()
    v4.__getitem__ = lambda self, k: {
        "node_type": "ModuleNode", "source_file": "src/orphan.py",
        "node_id": "src/orphan.py", "cluster_id": 1, "attrs": {},
    }[k]
    v4.index = 4
    vertices.append(v4)

    graph = MagicMock()
    graph.vs = vertices
    graph.es = edges
    graph.incident = MagicMock(return_value=[])

    node_index = {
        "src/utils.py": 0,
        "src/utils.py::parse_config": 1,
        "src/utils.py::validate": 2,
        "src/app.py": 3,
        "src/orphan.py": 4,
    }

    return graph, node_index


class TestGenerateSuggestions:
    def test_adds_suggestion_to_each_signal(self):
        graph, node_index = _make_mock_graph()
        spots = [{
            "file": "src/utils.py",
            "severity": 2,
            "signals": [
                {"type": "no_test_coverage", "detail": "no test file imports this module"},
                {"type": "undocumented", "detail": "2/2 public functions lack docstrings"},
            ],
        }]

        result = generate_suggestions(spots, graph, node_index)

        assert len(result) == 1
        for signal in result[0]["signals"]:
            assert "suggestion" in signal

    def test_no_test_coverage_generates_skeleton(self):
        graph, node_index = _make_mock_graph()
        spots = [{
            "file": "src/utils.py",
            "severity": 1,
            "signals": [{"type": "no_test_coverage", "detail": "no test"}],
        }]

        generate_suggestions(spots, graph, node_index)
        suggestion = spots[0]["signals"][0]["suggestion"]

        assert suggestion["action"] == "create_test"
        assert "test_" in suggestion["target_path"]
        assert "parse_config" in suggestion["skeleton"]
        assert "validate" in suggestion["skeleton"]

    def test_orphan_includes_cluster_neighbors(self):
        graph, node_index = _make_mock_graph()
        spots = [{
            "file": "src/utils.py",
            "severity": 1,
            "signals": [{"type": "orphan", "detail": "0 inbound"}],
        }]

        generate_suggestions(spots, graph, node_index)
        suggestion = spots[0]["signals"][0]["suggestion"]

        assert suggestion["action"] == "investigate"
        assert "options" in suggestion
        assert any("Delete" in opt for opt in suggestion["options"])


class TestSuggestTest:
    def test_generates_test_path(self):
        graph, node_index = _make_mock_graph()
        result = _suggest_test("src/utils.py", graph, node_index)
        assert result["action"] == "create_test"
        assert result["target_path"] == "tests/test_utils.py"

    def test_includes_function_names(self):
        graph, node_index = _make_mock_graph()
        result = _suggest_test("src/utils.py", graph, node_index)
        assert "parse_config" in result["functions"]
        assert "validate" in result["functions"]

    def test_skeleton_has_test_classes(self):
        graph, node_index = _make_mock_graph()
        result = _suggest_test("src/utils.py", graph, node_index)
        assert "TestParseConfig" in result["skeleton"]
        assert "TestValidate" in result["skeleton"]


class TestSuggestDocstrings:
    def test_generates_templates_for_undocumented(self):
        graph, node_index = _make_mock_graph()
        result = _suggest_docstrings("src/utils.py", graph, node_index)
        assert result["action"] == "add_docstrings"
        assert len(result["functions"]) == 2
        names = [f["name"] for f in result["functions"]]
        assert "parse_config" in names
        assert "validate" in names

    def test_template_includes_params(self):
        graph, node_index = _make_mock_graph()
        result = _suggest_docstrings("src/utils.py", graph, node_index)
        parse_template = next(f for f in result["functions"] if f["name"] == "parse_config")
        assert "path" in parse_template["template"]
        assert "strict" in parse_template["template"]


class TestSuggestOrphan:
    def test_includes_options(self):
        graph, node_index = _make_mock_graph()
        result = _suggest_orphan("src/orphan.py", graph, node_index)
        assert result["action"] == "investigate"
        assert len(result["options"]) >= 2

    def test_finds_cluster_neighbors(self):
        graph, node_index = _make_mock_graph()
        # src/utils.py is in cluster 0, so for cluster 0 module it should find neighbors
        result = _suggest_orphan("src/utils.py", graph, node_index)
        assert "nearest_cluster_members" in result


class TestHelpers:
    def test_to_class_name(self):
        assert _to_class_name("parse_config") == "ParseConfig"
        assert _to_class_name("validate") == "Validate"
        assert _to_class_name("get_user_by_id") == "GetUserById"
