"""Tests for _extract_var_reads_writes and depends_env edge creation."""
import pytest
from pathlib import Path

from rtfm.extractors.code_extractor import extract


class TestVarReadsWrites:
    def test_function_reads_module_var(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("MAX_SIZE = 100\n\ndef check(x):\n    return x < MAX_SIZE\n")
        result = extract(src, tmp_path, {"allow_partial_parse": True})
        reads = [e for e in result.edges if e["edge_type"] == "reads"]
        assert len(reads) >= 1
        assert any("MAX_SIZE" in e["target"] for e in reads)

    def test_function_writes_module_var(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("counter = 0\n\ndef increment():\n    global counter\n    counter = counter + 1\n")
        result = extract(src, tmp_path, {"allow_partial_parse": True})
        writes = [e for e in result.edges if e["edge_type"] == "writes"]
        assert len(writes) >= 1
        assert any("counter" in e["target"] for e in writes)

    def test_local_assignment_shadows_module_var(self, tmp_path):
        """Local assignment anywhere in function shadows module var (Python scoping)."""
        src = tmp_path / "mod.py"
        src.write_text("x = 10\n\ndef foo():\n    x = 20\n    return x\n")
        result = extract(src, tmp_path, {"allow_partial_parse": True})
        # x is assigned locally → shadows module x → no reads/writes edge
        reads = [e for e in result.edges if e["edge_type"] == "reads" and "::module::x" in e["target"]]
        assert len(reads) == 0

    def test_parameter_shadows_module_var(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("name = 'default'\n\ndef greet(name):\n    return f'hi {name}'\n")
        result = extract(src, tmp_path, {"allow_partial_parse": True})
        reads = [e for e in result.edges if e["edge_type"] == "reads" and "::module::name" in e["target"]]
        assert len(reads) == 0

    def test_class_method_reads_module_var(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("CONFIG = {}\n\nclass Foo:\n    def load(self):\n        return CONFIG\n")
        result = extract(src, tmp_path, {"allow_partial_parse": True})
        reads = [e for e in result.edges if e["edge_type"] == "reads" and "CONFIG" in e["target"]]
        assert len(reads) >= 1

    def test_no_module_vars_no_edges(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("def foo():\n    x = 1\n    return x\n")
        result = extract(src, tmp_path, {"allow_partial_parse": True})
        rw = [e for e in result.edges if e["edge_type"] in ("reads", "writes") and "::module::" in e["target"]]
        assert len(rw) == 0

    def test_syntax_error_no_crash(self, tmp_path):
        src = tmp_path / "bad.py"
        src.write_text("def foo(\n")  # invalid syntax
        result = extract(src, tmp_path, {"allow_partial_parse": True})
        # Should not crash — may have empty results
        assert result is not None


class TestDependsEnv:
    def test_os_getenv_literal(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("import os\nx = os.getenv('HOME')\n")
        result = extract(src, tmp_path, {"allow_partial_parse": True})
        deps = [e for e in result.edges if e["edge_type"] == "depends_env"]
        assert len(deps) == 1
        assert deps[0]["target"] == "config::env_var::HOME"

    def test_os_environ_get_literal(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("import os\nx = os.environ.get('PATH')\n")
        result = extract(src, tmp_path, {"allow_partial_parse": True})
        deps = [e for e in result.edges if e["edge_type"] == "depends_env"]
        assert len(deps) == 1
        assert deps[0]["target"] == "config::env_var::PATH"

    def test_os_environ_get_variable_no_edge(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("import os\nkey = 'HOME'\nx = os.environ.get(key)\n")
        result = extract(src, tmp_path, {"allow_partial_parse": True})
        deps = [e for e in result.edges if e["edge_type"] == "depends_env"]
        assert len(deps) == 0

    def test_depends_env_creates_config_node(self, tmp_path):
        src = tmp_path / "mod.py"
        src.write_text("import os\nx = os.getenv('AWS_REGION')\n")
        result = extract(src, tmp_path, {"allow_partial_parse": True})
        config_nodes = [n for n in result.nodes if n["id"] == "config::env_var::AWS_REGION"]
        assert len(config_nodes) == 1
        assert config_nodes[0]["node_type"] == "ConfigNode"
