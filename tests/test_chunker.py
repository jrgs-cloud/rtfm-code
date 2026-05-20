"""Tests for chunker.py — graph node to embeddable text chunks."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtfm.core.chunker import chunk_nodes, _format_function, _format_class, _format_module, _node_to_chunk


@pytest.fixture
def project_root(tmp_path):
    return tmp_path


class TestChunkNodes:
    def test_empty_nodes(self, project_root):
        result = chunk_nodes([], project_root)
        assert result == []

    def test_module_node_produces_chunk(self, project_root):
        nodes = [{
            "id": "src/app.py",
            "node_type": "ModuleNode",
            "source_file": "src/app.py",
            "attrs": {"functions": ["main", "setup"], "classes": []},
        }]
        result = chunk_nodes(nodes, project_root)
        assert len(result) == 1
        assert result[0]["node_id"] == "src/app.py"
        assert result[0]["node_type"] == "ModuleNode"

    def test_function_node_produces_chunk(self, project_root):
        # Create a source file for line reading
        src = project_root / "src" / "utils.py"
        src.parent.mkdir(parents=True)
        src.write_text("def helper(x, y):\n    return x + y\n")

        nodes = [{
            "id": "src/utils.py::helper",
            "node_type": "FunctionNode",
            "source_file": "src/utils.py",
            "attrs": {"params": [{"name": "x"}, {"name": "y"}], "return_type": "int", "line_range": [1, 2]},
        }]
        result = chunk_nodes(nodes, project_root)
        assert len(result) == 1
        assert "helper" in result[0]["content"]

    def test_class_node_produces_chunk(self, project_root):
        nodes = [{
            "id": "src/models.py::User",
            "node_type": "ClassNode",
            "source_file": "src/models.py",
            "attrs": {"methods": ["__init__", "save"], "bases": ["BaseModel"]},
        }]
        result = chunk_nodes(nodes, project_root)
        assert len(result) == 1
        assert "User" in result[0]["content"]

    def test_nodes_without_id_produce_empty_id_chunk(self, project_root):
        nodes = [{"node_type": "ModuleNode", "source_file": "x.py", "attrs": {}}]
        result = chunk_nodes(nodes, project_root)
        # Nodes without id still produce a chunk but with empty node_id
        assert len(result) == 1
        assert result[0]["node_id"] == ""

    def test_doc_node_produces_chunk(self, project_root):
        nodes = [{
            "id": "docs/README.md",
            "node_type": "DocNode",
            "source_file": "docs/README.md",
            "attrs": {"title": "Getting Started", "sections": ["Install", "Usage"]},
        }]
        result = chunk_nodes(nodes, project_root)
        assert len(result) == 1

    def test_config_node_produces_chunk(self, project_root):
        nodes = [{
            "id": "config::pyproject.toml",
            "node_type": "ConfigNode",
            "source_file": "pyproject.toml",
            "attrs": {"keys": ["name", "version", "dependencies"]},
        }]
        result = chunk_nodes(nodes, project_root)
        assert len(result) == 1


class TestFormatFunction:
    def test_basic_function(self):
        node = {
            "id": "src/app.py::handle",
            "node_type": "FunctionNode",
            "source_file": "src/app.py",
            "attrs": {"params": [{"name": "self"}, {"name": "request"}], "return_type": "Response"},
        }
        result = _format_function(node, None)
        assert "handle" in result
        assert "self" in result or "request" in result

    def test_function_with_source(self):
        node = {
            "id": "src/app.py::handle",
            "node_type": "FunctionNode",
            "source_file": "src/app.py",
            "attrs": {"params": [], "return_type": None},
        }
        source = "def handle():\n    pass"
        result = _format_function(node, source)
        assert "handle" in result


class TestFormatClass:
    def test_basic_class(self):
        node = {
            "id": "src/models.py::User",
            "node_type": "ClassNode",
            "source_file": "src/models.py",
            "attrs": {"methods": ["__init__", "save", "delete"], "bases": ["Model"]},
        }
        result = _format_class(node, None)
        assert "User" in result
        assert "Model" in result or "methods" in result.lower()

    def test_class_with_source(self):
        node = {
            "id": "src/models.py::User",
            "node_type": "ClassNode",
            "source_file": "src/models.py",
            "attrs": {"methods": [], "bases": []},
        }
        source = "class User:\n    pass"
        result = _format_class(node, source)
        assert "User" in result


class TestFormatModule:
    def test_module_with_functions_and_classes(self):
        node = {
            "id": "src/app.py",
            "node_type": "ModuleNode",
            "source_file": "src/app.py",
            "attrs": {"functions": ["main", "setup"], "classes": ["App"]},
        }
        result = _format_module(node)
        assert "app.py" in result or "src/app.py" in result
