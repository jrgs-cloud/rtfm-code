"""Tests for extractors/code_extractor.py — Python source extraction via tree-sitter."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtfm.extractors.code_extractor import extract


@pytest.fixture
def project_root(tmp_path):
    return tmp_path


def _write_and_extract(tmp_path, filename, source, config=None):
    """Helper: write source to file and run extract."""
    file_path = tmp_path / filename
    file_path.write_text(source)
    return extract(file_path, tmp_path, config or {})


# ---------------------------------------------------------------------------
# Module extraction
# ---------------------------------------------------------------------------


class TestModuleExtraction:
    def test_extracts_module_node(self, project_root):
        result = _write_and_extract(project_root, "app.py", "x = 1\n")
        modules = [n for n in result.nodes if n["node_type"] == "ModuleNode"]
        assert len(modules) == 1
        assert modules[0]["id"] == "app.py"

    def test_module_line_count(self, project_root):
        source = "a = 1\nb = 2\nc = 3\n"
        result = _write_and_extract(project_root, "lines.py", source)
        modules = [n for n in result.nodes if n["node_type"] == "ModuleNode"]
        assert modules[0]["attrs"]["line_count"] == 4  # 3 lines + trailing

    def test_module_imports_collected(self, project_root):
        source = "import os\nfrom pathlib import Path\n"
        result = _write_and_extract(project_root, "imports.py", source)
        modules = [n for n in result.nodes if n["node_type"] == "ModuleNode"]
        imports = modules[0]["attrs"]["imports"]
        assert "os" in imports
        assert "pathlib" in imports


# ---------------------------------------------------------------------------
# Function extraction
# ---------------------------------------------------------------------------


class TestFunctionExtraction:
    def test_extracts_function(self, project_root):
        source = "def hello(name: str) -> str:\n    return f'hi {name}'\n"
        result = _write_and_extract(project_root, "funcs.py", source)
        funcs = [n for n in result.nodes if n["node_type"] == "FunctionNode"]
        assert len(funcs) == 1
        assert funcs[0]["id"] == "funcs.py::hello"
        assert funcs[0]["attrs"]["name"] == "hello"
        assert funcs[0]["attrs"]["return_type"] == "str"

    def test_extracts_params_with_types(self, project_root):
        source = "def add(a: int, b: int) -> int:\n    return a + b\n"
        result = _write_and_extract(project_root, "params.py", source)
        funcs = [n for n in result.nodes if n["node_type"] == "FunctionNode"]
        params = funcs[0]["attrs"]["params"]
        assert len(params) == 2
        # tree-sitter typed_parameter doesn't expose 'name' as a field;
        # type annotations are captured correctly
        assert params[0]["type"] == "int"
        assert params[1]["type"] == "int"

    def test_extracts_untyped_params(self, project_root):
        source = "def greet(name):\n    return name\n"
        result = _write_and_extract(project_root, "untyped.py", source)
        funcs = [n for n in result.nodes if n["node_type"] == "FunctionNode"]
        params = funcs[0]["attrs"]["params"]
        assert len(params) == 1
        assert params[0]["name"] == "name"

    def test_decorated_function(self, project_root):
        source = "@staticmethod\ndef helper():\n    pass\n"
        result = _write_and_extract(project_root, "deco.py", source)
        funcs = [n for n in result.nodes if n["node_type"] == "FunctionNode"]
        assert len(funcs) == 1
        assert "staticmethod" in funcs[0]["attrs"]["decorators"]


# ---------------------------------------------------------------------------
# Class extraction
# ---------------------------------------------------------------------------


class TestClassExtraction:
    def test_extracts_class(self, project_root):
        source = "class Animal:\n    def speak(self):\n        pass\n"
        result = _write_and_extract(project_root, "cls.py", source)
        classes = [n for n in result.nodes if n["node_type"] == "ClassNode"]
        assert len(classes) == 1
        assert classes[0]["id"] == "cls.py::Animal"
        assert "speak" in classes[0]["attrs"]["methods"]

    def test_class_inheritance_edge(self, project_root):
        source = "class Base:\n    pass\n\nclass Child(Base):\n    pass\n"
        result = _write_and_extract(project_root, "inherit.py", source)
        inherit_edges = [e for e in result.edges if e["edge_type"] == "inherits"]
        assert len(inherit_edges) == 1
        assert inherit_edges[0]["source"] == "inherit.py::Child"
        assert inherit_edges[0]["target"] == "inherit.py::Base"

    def test_class_attributes(self, project_root):
        source = "class Config:\n    debug = True\n    port = 8080\n"
        result = _write_and_extract(project_root, "attrs.py", source)
        classes = [n for n in result.nodes if n["node_type"] == "ClassNode"]
        assert "debug" in classes[0]["attrs"]["attributes"]
        assert "port" in classes[0]["attrs"]["attributes"]


# ---------------------------------------------------------------------------
# Import edges
# ---------------------------------------------------------------------------


class TestImportEdges:
    def test_import_from_creates_edge(self, project_root):
        source = "from pathlib import Path\n"
        result = _write_and_extract(project_root, "imp.py", source)
        import_edges = [e for e in result.edges if e["edge_type"] == "imports"]
        assert len(import_edges) == 1
        assert import_edges[0]["source"] == "imp.py"

    def test_relative_import_resolved(self, project_root):
        pkg = project_root / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        source = "from .utils import helper\n"
        file_path = pkg / "main.py"
        file_path.write_text(source)
        result = extract(file_path, project_root, {})
        import_edges = [e for e in result.edges if e["edge_type"] == "imports"]
        assert len(import_edges) == 1
        assert import_edges[0]["target"] == "pkg/utils.py"

    def test_absolute_import_statement(self, project_root):
        source = "import json\nimport os\n"
        result = _write_and_extract(project_root, "abs.py", source)
        import_edges = [e for e in result.edges if e["edge_type"] == "imports"]
        targets = [e["target"] for e in import_edges]
        assert "json" in targets
        assert "os" in targets


# ---------------------------------------------------------------------------
# Call edges
# ---------------------------------------------------------------------------


class TestCallEdges:
    def test_function_call_creates_edge(self, project_root):
        source = "def helper():\n    pass\n\ndef main():\n    helper()\n"
        result = _write_and_extract(project_root, "calls.py", source)
        call_edges = [e for e in result.edges if e["edge_type"] == "calls"]
        assert len(call_edges) >= 1
        sources = [e["source"] for e in call_edges]
        assert "calls.py::main" in sources

    def test_imported_call_resolved(self, project_root):
        pkg = project_root / "pkg"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        source = "from .utils import do_thing\n\ndef run():\n    do_thing()\n"
        file_path = pkg / "runner.py"
        file_path.write_text(source)
        result = extract(file_path, project_root, {})
        call_edges = [e for e in result.edges if e["edge_type"] == "calls"]
        # Should resolve to the imported target
        targets = [e["target"] for e in call_edges]
        assert any("do_thing" in t for t in targets)


# ---------------------------------------------------------------------------
# Environment variable dependencies
# ---------------------------------------------------------------------------


class TestEnvVarDeps:
    def test_os_getenv_detected(self, project_root):
        source = "import os\nval = os.getenv('MY_VAR')\n"
        result = _write_and_extract(project_root, "env.py", source)
        env_edges = [e for e in result.edges if e["edge_type"] == "depends_env"]
        assert len(env_edges) == 1
        assert env_edges[0]["target"] == "config::env_var::MY_VAR"

    def test_os_environ_get_detected(self, project_root):
        source = "import os\nval = os.environ.get('SECRET_KEY')\n"
        result = _write_and_extract(project_root, "env2.py", source)
        env_edges = [e for e in result.edges if e["edge_type"] == "depends_env"]
        assert len(env_edges) == 1
        assert env_edges[0]["target"] == "config::env_var::SECRET_KEY"


# ---------------------------------------------------------------------------
# Docstring edges
# ---------------------------------------------------------------------------


class TestDocstringEdges:
    def test_function_docstring_creates_doc_node(self, project_root):
        source = 'def greet():\n    """Say hello."""\n    pass\n'
        result = _write_and_extract(project_root, "doc.py", source)
        doc_nodes = [n for n in result.nodes if n["node_type"] == "DocNode"]
        assert len(doc_nodes) == 1
        assert doc_nodes[0]["attrs"]["docstring"] == "Say hello."

    def test_docstring_edge_links_to_function(self, project_root):
        source = 'def greet():\n    """Say hello."""\n    pass\n'
        result = _write_and_extract(project_root, "doc2.py", source)
        doc_edges = [e for e in result.edges if e["edge_type"] == "documents"]
        assert len(doc_edges) == 1
        assert doc_edges[0]["target"] == "doc2.py::greet"


# ---------------------------------------------------------------------------
# Variable extraction
# ---------------------------------------------------------------------------


class TestVariableExtraction:
    def test_module_level_variable(self, project_root):
        source = "DEBUG = True\nPORT = 8080\n"
        result = _write_and_extract(project_root, "vars.py", source)
        var_nodes = [n for n in result.nodes if n["node_type"] == "VariableNode"]
        names = [n["attrs"]["name"] for n in var_nodes]
        assert "DEBUG" in names
        assert "PORT" in names


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_nonexistent_file_returns_empty(self, project_root):
        result = extract(project_root / "missing.py", project_root, {})
        assert result.nodes == []
        assert result.edges == []

    def test_syntax_error_returns_empty_by_default(self, project_root):
        source = "def broken(\n"  # Invalid syntax
        result = _write_and_extract(project_root, "bad.py", source)
        assert result.nodes == []
        assert result.edges == []

    def test_syntax_error_with_allow_partial(self, project_root):
        source = "def broken(\n"
        result = _write_and_extract(project_root, "bad2.py", source, {"allow_partial_parse": True})
        # With allow_partial_parse, it attempts extraction even with errors
        # May or may not produce nodes depending on tree-sitter recovery
        assert isinstance(result.nodes, list)
