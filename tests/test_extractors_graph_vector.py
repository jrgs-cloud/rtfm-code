"""Unit tests for rtfm plugin: extractors, graph ops, vector store."""

from __future__ import annotations

from pathlib import Path

import pytest

from rtfm.core.types import (
    ET_CALLS,
    ET_CONFIGURES,
    ET_CROSS_REFERENCES,
    ET_DEPENDS_ENV,
    ET_DOCUMENTS,
    ET_IMPORTS,
    ET_INHERITS,
    NT_CLASS,
    NT_CONFIG,
    NT_DOC,
    NT_FUNCTION,
    NT_MODULE,
    NT_TYPE,
    NT_VARIABLE,
    ExtractionResult,
    make_edge,
    make_node,
)

FIXTURES = Path(__file__).parent / "fixtures"


# ===========================================================================
# Extractor tests — Python code extractor
# ===========================================================================


class TestCodeExtractor:
    def test_extract_returns_extraction_result(self):
        from rtfm.extractors.code_extractor import extract

        result = extract(FIXTURES / "sample.py", FIXTURES, {})
        assert isinstance(result, ExtractionResult)
        assert len(result.nodes) > 0
        assert len(result.edges) > 0

    def test_module_node_created(self):
        from rtfm.extractors.code_extractor import extract

        result = extract(FIXTURES / "sample.py", FIXTURES, {})
        module_nodes = [n for n in result.nodes if n["node_type"] == NT_MODULE]
        assert len(module_nodes) == 1
        assert module_nodes[0]["id"] == "sample.py"

    def test_class_nodes_extracted(self):
        from rtfm.extractors.code_extractor import extract

        result = extract(FIXTURES / "sample.py", FIXTURES, {})
        class_nodes = [n for n in result.nodes if n["node_type"] == NT_CLASS]
        class_names = [n["attrs"]["name"] for n in class_nodes]
        assert "Animal" in class_names
        assert "Dog" in class_names

    def test_function_nodes_extracted(self):
        from rtfm.extractors.code_extractor import extract

        result = extract(FIXTURES / "sample.py", FIXTURES, {})
        func_nodes = [n for n in result.nodes if n["node_type"] == NT_FUNCTION]
        func_names = [n["attrs"]["name"] for n in func_nodes]
        assert "greet" in func_names
        assert "fetch_data" in func_names
        assert "speak" in func_names
        assert "bark" in func_names

    def test_inheritance_edge(self):
        from rtfm.extractors.code_extractor import extract

        result = extract(FIXTURES / "sample.py", FIXTURES, {})
        inherit_edges = [e for e in result.edges if e["edge_type"] == ET_INHERITS]
        assert any(
            e["source"] == "sample.py::Dog" and "Animal" in e["target"]
            for e in inherit_edges
        )

    def test_import_edges(self):
        from rtfm.extractors.code_extractor import extract

        result = extract(FIXTURES / "sample.py", FIXTURES, {})
        import_edges = [e for e in result.edges if e["edge_type"] == ET_IMPORTS]
        targets = [e["target"] for e in import_edges]
        assert "os" in targets
        assert "pathlib" in targets

    def test_call_edges(self):
        from rtfm.extractors.code_extractor import extract

        result = extract(FIXTURES / "sample.py", FIXTURES, {})
        call_edges = [e for e in result.edges if e["edge_type"] == ET_CALLS]
        assert any(
            e["source"] == "sample.py::fetch_data" and "greet" in e["target"]
            for e in call_edges
        )

    def test_env_var_dependency(self):
        from rtfm.extractors.code_extractor import extract

        result = extract(FIXTURES / "sample.py", FIXTURES, {})
        env_edges = [e for e in result.edges if e["edge_type"] == ET_DEPENDS_ENV]
        assert any("API_KEY" in e["target"] for e in env_edges)

    def test_nonexistent_file_returns_empty(self):
        from rtfm.extractors.code_extractor import extract

        result = extract(FIXTURES / "nonexistent.py", FIXTURES, {})
        assert result.nodes == []
        assert result.edges == []

    def test_empty_file(self, tmp_path):
        from rtfm.extractors.code_extractor import extract

        empty = tmp_path / "empty.py"
        empty.write_text("")
        result = extract(empty, tmp_path, {})
        module_nodes = [n for n in result.nodes if n["node_type"] == NT_MODULE]
        assert len(module_nodes) == 1

    def test_parse_error_returns_empty(self, tmp_path):
        from rtfm.extractors.code_extractor import extract

        bad = tmp_path / "bad.py"
        bad.write_text("def (broken syntax @@@@")
        result = extract(bad, tmp_path, {})
        assert result.nodes == []

    def test_variable_nodes_extracted(self):
        from rtfm.extractors.code_extractor import extract

        result = extract(FIXTURES / "sample.py", FIXTURES, {})
        var_nodes = [n for n in result.nodes if n["node_type"] == NT_VARIABLE]
        var_names = [n["attrs"]["name"] for n in var_nodes]
        assert "MY_VAR" in var_names


# ===========================================================================
# Extractor tests — TypeScript extractor
# ===========================================================================


class TestTypescriptExtractor:
    @pytest.fixture(autouse=True)
    def _skip_without_tree_sitter(self):
        pytest.importorskip("tree_sitter")

    def test_extract_returns_extraction_result(self):
        from rtfm.extractors.typescript_extractor import extract

        result = extract(FIXTURES / "sample.ts", FIXTURES, {})
        assert isinstance(result, ExtractionResult)
        assert len(result.nodes) > 0

    def test_module_node_created(self):
        from rtfm.extractors.typescript_extractor import extract

        result = extract(FIXTURES / "sample.ts", FIXTURES, {})
        module_nodes = [n for n in result.nodes if n["node_type"] == NT_MODULE]
        assert any(n["id"] == "sample.ts" for n in module_nodes)

    def test_class_extracted(self):
        from rtfm.extractors.typescript_extractor import extract

        result = extract(FIXTURES / "sample.ts", FIXTURES, {})
        class_nodes = [n for n in result.nodes if n["node_type"] == NT_CLASS]
        class_names = [n["attrs"]["name"] for n in class_nodes]
        assert "Server" in class_names

    def test_interface_extracted_as_type(self):
        from rtfm.extractors.typescript_extractor import extract

        result = extract(FIXTURES / "sample.ts", FIXTURES, {})
        type_nodes = [n for n in result.nodes if n["node_type"] == NT_TYPE]
        type_names = [n["attrs"]["name"] for n in type_nodes]
        assert "Config" in type_names

    def test_function_extracted(self):
        from rtfm.extractors.typescript_extractor import extract

        result = extract(FIXTURES / "sample.ts", FIXTURES, {})
        func_nodes = [n for n in result.nodes if n["node_type"] == NT_FUNCTION]
        func_names = [n["attrs"]["name"] for n in func_nodes]
        assert "createServer" in func_names

    def test_arrow_function_extracted(self):
        from rtfm.extractors.typescript_extractor import extract

        result = extract(FIXTURES / "sample.ts", FIXTURES, {})
        func_nodes = [n for n in result.nodes if n["node_type"] == NT_FUNCTION]
        func_names = [n["attrs"]["name"] for n in func_nodes]
        assert "helper" in func_names

    def test_class_has_methods(self):
        from rtfm.extractors.typescript_extractor import extract

        result = extract(FIXTURES / "sample.ts", FIXTURES, {})
        class_nodes = [n for n in result.nodes if n["node_type"] == NT_CLASS]
        server = next(n for n in class_nodes if n["attrs"]["name"] == "Server")
        assert "start" in server["attrs"]["methods"]
        assert "constructor" in server["attrs"]["methods"]

    def test_import_edges(self):
        from rtfm.extractors.typescript_extractor import extract

        result = extract(FIXTURES / "sample.ts", FIXTURES, {})
        import_edges = [e for e in result.edges if e["edge_type"] == ET_IMPORTS]
        assert any("express" in e["target"] for e in import_edges)

    def test_nonexistent_file_returns_empty(self):
        from rtfm.extractors.typescript_extractor import extract

        result = extract(FIXTURES / "nonexistent.ts", FIXTURES, {})
        assert result.nodes == []
        assert result.edges == []


# ===========================================================================
# Extractor tests — Config extractor
# ===========================================================================


class TestConfigExtractor:
    def test_extract_json_config(self):
        from rtfm.extractors.config_extractor import extract

        # Rename fixture to match expected pattern
        result = extract(
            FIXTURES / "sample_config.json",
            FIXTURES,
            {"config_patterns": {"sample_config.json": "json"}},
        )
        assert isinstance(result, ExtractionResult)
        assert len(result.nodes) > 0

    def test_config_nodes_have_correct_type(self):
        from rtfm.extractors.config_extractor import extract

        result = extract(
            FIXTURES / "sample_config.json",
            FIXTURES,
            {"config_patterns": {"sample_config.json": "json"}},
        )
        for node in result.nodes:
            assert node["node_type"] == NT_CONFIG

    def test_configures_edges_created(self):
        from rtfm.extractors.config_extractor import extract

        result = extract(
            FIXTURES / "sample_config.json",
            FIXTURES,
            {"config_patterns": {"sample_config.json": "json"}},
        )
        config_edges = [e for e in result.edges if e["edge_type"] == ET_CONFIGURES]
        assert len(config_edges) > 0

    def test_unrecognized_file_returns_empty(self):
        from rtfm.extractors.config_extractor import extract

        result = extract(FIXTURES / "sample.py", FIXTURES, {})
        assert result.nodes == []
        assert result.edges == []

    def test_nonexistent_file_returns_empty(self):
        from rtfm.extractors.config_extractor import extract

        result = extract(
            FIXTURES / "missing.json",
            FIXTURES,
            {"config_patterns": {"missing.json": "json"}},
        )
        assert result.nodes == []
        assert result.edges == []


# ===========================================================================
# Extractor tests — Doc extractor
# ===========================================================================


class TestDocExtractor:
    def test_extract_markdown(self):
        from rtfm.extractors.doc_extractor import extract

        result = extract(FIXTURES / "README.md", FIXTURES, {})
        assert isinstance(result, ExtractionResult)
        assert len(result.nodes) == 1

    def test_doc_node_type(self):
        from rtfm.extractors.doc_extractor import extract

        result = extract(FIXTURES / "README.md", FIXTURES, {})
        assert result.nodes[0]["node_type"] == NT_DOC

    def test_title_extracted(self):
        from rtfm.extractors.doc_extractor import extract

        result = extract(FIXTURES / "README.md", FIXTURES, {})
        assert result.nodes[0]["attrs"]["title"] == "Sample Documentation"

    def test_sections_extracted(self):
        from rtfm.extractors.doc_extractor import extract

        result = extract(FIXTURES / "README.md", FIXTURES, {})
        sections = result.nodes[0]["attrs"]["sections"]
        assert "Overview" in sections
        assert "Installation" in sections
        assert "API Reference" in sections

    def test_content_type_readme(self):
        from rtfm.extractors.doc_extractor import extract

        result = extract(FIXTURES / "README.md", FIXTURES, {})
        assert result.nodes[0]["attrs"]["content_type"] == "readme"

    def test_nonexistent_file_returns_empty(self):
        from rtfm.extractors.doc_extractor import extract

        result = extract(FIXTURES / "missing.md", FIXTURES, {})
        assert result.nodes == []

    def test_excluded_file_returns_empty(self):
        from rtfm.extractors.doc_extractor import extract

        result = extract(
            FIXTURES / "README.md", FIXTURES, {"excluded_filenames": ["README.md"]}
        )
        assert result.nodes == []


# ===========================================================================
# Extractor tests — Crossref extractor
# ===========================================================================


class TestCrossrefExtractor:
    def test_extract_cross_references(self):
        from rtfm.extractors.crossref_extractor import extract

        result = extract(FIXTURES / "README.md", FIXTURES, {})
        assert isinstance(result, ExtractionResult)
        assert len(result.edges) > 0

    def test_edge_type_is_cross_references(self):
        from rtfm.extractors.crossref_extractor import extract

        result = extract(FIXTURES / "README.md", FIXTURES, {})
        for edge in result.edges:
            assert edge["edge_type"] == ET_CROSS_REFERENCES

    def test_resolves_relative_links(self):
        from rtfm.extractors.crossref_extractor import extract

        result = extract(FIXTURES / "README.md", FIXTURES, {})
        targets = [e["target"] for e in result.edges]
        assert any("sample_config.json" in t for t in targets)
        assert any("sample.py" in t for t in targets)

    def test_ignores_external_urls(self):
        from rtfm.extractors.crossref_extractor import extract

        result = extract(FIXTURES / "README.md", FIXTURES, {})
        for edge in result.edges:
            assert "http://" not in edge["target"]
            assert "https://" not in edge["target"]

    def test_nonexistent_file_returns_empty(self):
        from rtfm.extractors.crossref_extractor import extract

        result = extract(FIXTURES / "missing.md", FIXTURES, {})
        assert result.edges == []


# ===========================================================================
# Graph ops tests — graph_builder
# ===========================================================================


class TestGraphBuilder:
    def _make_results(self) -> list[ExtractionResult]:
        r1 = ExtractionResult()
        r1.nodes.append(make_node(
            id="mod_a.py", node_type=NT_MODULE, source_file="mod_a.py",
            checksum="aaa",
        ))
        r1.nodes.append(make_node(
            id="mod_a.py::foo", node_type=NT_FUNCTION, source_file="mod_a.py",
            checksum="aaa", name="foo",
        ))
        r1.nodes.append(make_node(
            id="mod_a.py::Bar", node_type=NT_CLASS, source_file="mod_a.py",
            checksum="aaa", name="Bar",
        ))
        r1.edges.append(make_edge(
            source="mod_a.py::foo", target="mod_a.py::Bar",
            edge_type=ET_CALLS,
        ))
        r1.edges.append(make_edge(
            source="mod_a.py", target="os",
            edge_type=ET_IMPORTS,
        ))

        r2 = ExtractionResult()
        r2.nodes.append(make_node(
            id="mod_b.py", node_type=NT_MODULE, source_file="mod_b.py",
            checksum="bbb",
        ))
        r2.nodes.append(make_node(
            id="mod_b.py::baz", node_type=NT_FUNCTION, source_file="mod_b.py",
            checksum="bbb", name="baz",
        ))
        r2.edges.append(make_edge(
            source="mod_b.py::baz", target="mod_a.py::foo",
            edge_type=ET_CALLS,
        ))
        r2.edges.append(make_edge(
            source="mod_b.py", target="mod_a.py",
            edge_type=ET_IMPORTS,
        ))

        return [r1, r2]

    def test_merge_results_deduplicates_nodes(self):
        from rtfm.core.graph_builder import merge_results

        results = self._make_results()
        nodes, edges = merge_results(results)
        node_ids = [n["id"] for n in nodes]
        assert len(node_ids) == len(set(node_ids))

    def test_build_graph_node_count(self):
        from rtfm.core.graph_builder import build_graph, merge_results

        results = self._make_results()
        nodes, edges = merge_results(results)
        g = build_graph(nodes, edges)
        assert g.vcount() == 5

    def test_build_graph_edge_count(self):
        from rtfm.core.graph_builder import build_graph, merge_results

        results = self._make_results()
        nodes, edges = merge_results(results)
        g = build_graph(nodes, edges)
        # Only edges where both source and target exist in nodes
        # "os" is a target but not in nodes, so that edge is dropped
        assert g.ecount() == 3

    def test_build_graph_empty(self):
        from rtfm.core.graph_builder import build_graph

        g = build_graph([], [])
        assert g.vcount() == 0
        assert g.ecount() == 0

    def test_run_leiden_assigns_clusters(self):
        from rtfm.core.graph_builder import build_graph, merge_results, run_leiden

        results = self._make_results()
        nodes, edges = merge_results(results)
        g = build_graph(nodes, edges)
        g = run_leiden(g)
        cluster_ids = [g.vs[i]["cluster_id"] for i in range(g.vcount())]
        assert all(isinstance(c, int) for c in cluster_ids)


# ===========================================================================
# Graph ops tests — graph_analysis
# ===========================================================================


class TestGraphAnalysis:
    def _build_test_graph(self):
        from rtfm.core.graph_builder import build_graph, merge_results, run_leiden

        r = ExtractionResult()
        r.nodes.append(make_node(
            id="src/app.py", node_type=NT_MODULE, source_file="src/app.py",
            checksum="a1",
        ))
        r.nodes.append(make_node(
            id="src/app.py::main", node_type=NT_FUNCTION, source_file="src/app.py",
            checksum="a1", name="main",
        ))
        r.nodes.append(make_node(
            id="src/handler.py", node_type=NT_MODULE, source_file="src/handler.py",
            checksum="h1",
        ))
        r.nodes.append(make_node(
            id="src/handler.py::handle", node_type=NT_FUNCTION, source_file="src/handler.py",
            checksum="h1", name="handle",
        ))
        r.nodes.append(make_node(
            id="src/utils.py", node_type=NT_MODULE, source_file="src/utils.py",
            checksum="u1",
        ))
        r.nodes.append(make_node(
            id="src/utils.py::helper", node_type=NT_FUNCTION, source_file="src/utils.py",
            checksum="u1", name="helper",
        ))

        r.edges.append(make_edge(
            source="src/app.py::main", target="src/handler.py::handle",
            edge_type=ET_CALLS,
        ))
        r.edges.append(make_edge(
            source="src/handler.py::handle", target="src/utils.py::helper",
            edge_type=ET_CALLS,
        ))
        r.edges.append(make_edge(
            source="src/app.py", target="src/handler.py",
            edge_type=ET_IMPORTS,
        ))
        r.edges.append(make_edge(
            source="src/handler.py", target="src/utils.py",
            edge_type=ET_IMPORTS,
        ))

        nodes, edges = merge_results([r])
        g = build_graph(nodes, edges)
        g = run_leiden(g)
        node_index = {g.vs[i]["node_id"]: i for i in range(g.vcount())}
        return g, node_index

    def test_impact_analysis_returns_downstream(self):
        from rtfm.core.graph_analysis import impact_analysis

        g, node_index = self._build_test_graph()
        result = impact_analysis(g, node_index, "src/app.py::main", depth=2)
        assert result["kb_miss"] is False
        impacted_ids = [n["node_id"] for n in result["primary_impact"]]
        assert "src/handler.py::handle" in impacted_ids

    def test_impact_analysis_missing_node(self):
        from rtfm.core.graph_analysis import impact_analysis

        g, node_index = self._build_test_graph()
        result = impact_analysis(g, node_index, "nonexistent::func")
        assert result["kb_miss"] is True

    def test_get_neighbors_out(self):
        from rtfm.core.graph_analysis import get_neighbors

        g, node_index = self._build_test_graph()
        result = get_neighbors(g, node_index, "src/app.py::main", direction="out")
        assert result["kb_miss"] is False
        assert result["count"] > 0
        neighbor_ids = [n["node_id"] for n in result["neighbors"]]
        assert "src/handler.py::handle" in neighbor_ids

    def test_get_neighbors_missing_node(self):
        from rtfm.core.graph_analysis import get_neighbors

        g, node_index = self._build_test_graph()
        result = get_neighbors(g, node_index, "nonexistent")
        assert result["kb_miss"] is True

    def test_get_cluster(self):
        from rtfm.core.graph_analysis import get_cluster

        g, node_index = self._build_test_graph()
        result = get_cluster(g, node_index, node_id="src/app.py")
        assert result["kb_miss"] is False
        assert result["size"] > 0
        assert isinstance(result["nodes"], list)


# ===========================================================================
# Vector store tests — graceful degradation
# ===========================================================================

_lancedb_available = False
try:
    from rtfm.core.vector_store import _LANCEDB_AVAILABLE
    _lancedb_available = _LANCEDB_AVAILABLE
except ImportError:
    pass


class TestVectorStore:
    def test_index_exists_false_for_missing_path(self, tmp_path):
        from rtfm.core.vector_store import index_exists

        assert index_exists(tmp_path / "nonexistent") is False

    @pytest.mark.skipif(not _lancedb_available, reason="lancedb not installed")
    def test_create_index_raises_on_empty_chunks(self, tmp_path):
        from rtfm.core.vector_store import create_index

        with pytest.raises(ValueError, match="No chunks provided"):
            create_index([], tmp_path / "idx")

    @pytest.mark.skipif(not _lancedb_available, reason="lancedb not installed")
    def test_search_returns_empty_for_missing_index(self, tmp_path):
        from rtfm.core.vector_store import search

        results = search("test query", tmp_path / "nonexistent")
        assert results == []

    @pytest.mark.skipif(not _lancedb_available, reason="lancedb not installed")
    def test_get_index_stats_raises_for_missing(self, tmp_path):
        from rtfm.core.vector_store import get_index_stats

        with pytest.raises(FileNotFoundError):
            get_index_stats(tmp_path / "nonexistent")

    def test_search_returns_error_when_lancedb_unavailable(self, tmp_path, monkeypatch):
        import rtfm.core.vector_store as vs
        monkeypatch.setattr(vs, "_LANCEDB_AVAILABLE", False)
        from rtfm.core.vector_store import search

        result = search("test query", tmp_path / "nonexistent")
        assert isinstance(result, dict)
        assert result["error"] == "model_unavailable"

    def test_create_index_returns_error_when_lancedb_unavailable(self, tmp_path, monkeypatch):
        import rtfm.core.vector_store as vs
        monkeypatch.setattr(vs, "_LANCEDB_AVAILABLE", False)
        from rtfm.core.vector_store import create_index

        result = create_index([{"content": "x"}], tmp_path / "idx")
        assert isinstance(result, dict)
        assert result["error"] == "model_unavailable"

    def test_get_index_stats_returns_error_when_lancedb_unavailable(self, tmp_path, monkeypatch):
        import rtfm.core.vector_store as vs
        monkeypatch.setattr(vs, "_LANCEDB_AVAILABLE", False)
        from rtfm.core.vector_store import get_index_stats

        result = get_index_stats(tmp_path / "nonexistent")
        assert isinstance(result, dict)
        assert result["error"] == "model_unavailable"


# ===========================================================================
# Gate threshold logic
# ===========================================================================


class TestGateThreshold:
    """Test the gate command's level classification logic."""

    def _setup_graph(self, tmp_path, num_dependents: int):
        """Create a minimal graph with a known number of dependents for target.py."""
        import json
        import pickle
        from rtfm.core.graph_builder import build_graph, merge_results
        from rtfm.core.types import ExtractionResult, make_node, make_edge

        results = []
        r = ExtractionResult()
        r.nodes.append(make_node(id="target.py", node_type="ModuleNode", source_file="target.py"))
        for i in range(num_dependents):
            dep_id = f"dep_{i}.py"
            r.nodes.append(make_node(id=dep_id, node_type="ModuleNode", source_file=dep_id))
            r.edges.append(make_edge(source="target.py", target=dep_id, edge_type="imports"))
        results.append(r)

        nodes, edges = merge_results(results)
        graph = build_graph(nodes, edges)

        # Serialize
        graph_json = {"nodes": nodes, "edges": edges}
        json_path = tmp_path / "rtfm-graph.json"
        pickle_path = tmp_path / "graph.pkl"
        json_path.write_text(json.dumps(graph_json))
        with open(pickle_path, "wb") as f:
            pickle.dump(graph, f)

        return tmp_path

    def test_info_level_below_threshold(self, tmp_path, monkeypatch):
        """Files with <=4 dependents should return level 'info'."""
        import json
        from click.testing import CliRunner
        from rtfm.cli import main

        state_dir = self._setup_graph(tmp_path, num_dependents=3)
        monkeypatch.chdir(tmp_path)

        # Write .rtfm.json with threshold
        config = {"gate": {"warning_threshold": 5, "locked_files": []}}
        (tmp_path / ".rtfm.json").write_text(json.dumps(config))

        runner = CliRunner()
        result = runner.invoke(main, ["--state-dir", str(state_dir), "gate", "target.py"])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["level"] == "info"
        assert output["dependents_count"] <= 5

    def test_warning_level_above_threshold(self, tmp_path, monkeypatch):
        """Files with >5 dependents should return level 'warning'."""
        import json
        from click.testing import CliRunner
        from rtfm.cli import main

        state_dir = self._setup_graph(tmp_path, num_dependents=7)
        monkeypatch.chdir(tmp_path)

        config = {"gate": {"warning_threshold": 5, "locked_files": []}}
        (tmp_path / ".rtfm.json").write_text(json.dumps(config))

        runner = CliRunner()
        result = runner.invoke(main, ["--state-dir", str(state_dir), "gate", "target.py"])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["level"] == "warning"
        assert output["dependents_count"] > 5

    def test_block_level_locked_file(self, tmp_path, monkeypatch):
        """Locked files should return level 'block'."""
        import json
        from click.testing import CliRunner
        from rtfm.cli import main

        state_dir = self._setup_graph(tmp_path, num_dependents=0)
        monkeypatch.chdir(tmp_path)

        config = {"gate": {"warning_threshold": 5, "locked_files": ["target.py"]}}
        (tmp_path / ".rtfm.json").write_text(json.dumps(config))

        runner = CliRunner()
        result = runner.invoke(main, ["--state-dir", str(state_dir), "gate", "target.py"])
        assert result.exit_code == 0, result.output
        output = json.loads(result.output)
        assert output["level"] == "block"
        assert output["reason"] == "locked file"
        assert "target.py" in output["locked_hits"]
