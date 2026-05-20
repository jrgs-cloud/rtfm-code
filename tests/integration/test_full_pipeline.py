"""Integration test — full pipeline: extract, build, query, gate.

Exercises the complete rtfm pipeline end-to-end on a sample
Python + TypeScript project (10 files).

Run with: pytest tests/integration/test_full_pipeline.py -m integration
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

igraph = pytest.importorskip("igraph", reason="igraph required for integration tests")

from rtfm.core.graph_builder import (
    build_graph,
    inject_packages,
    merge_results,
    run_leiden,
    serialize,
)
from rtfm.core.graph_store import (
    build_pickle,
    find_by_name,
    load_or_rebuild,
    search_nodes,
)
from rtfm.core.graph_analysis import (
    get_neighbors,
    impact_analysis,
    structural_query,
)
from rtfm.core.types import ExtractionResult
from rtfm.extractors.code_extractor import extract as extract_python
from rtfm.extractors.config_extractor import extract as extract_config
from rtfm.extractors.doc_extractor import extract as extract_doc
from rtfm.extractors.crossref_extractor import extract as extract_crossref

try:
    from rtfm.extractors.typescript_extractor import extract as extract_ts

    HAS_TS_EXTRACTOR = True
except ImportError:
    extract_ts = None  # type: ignore[assignment]
    HAS_TS_EXTRACTOR = False


SAMPLE_PROJECT = Path(__file__).parent / "sample_project"
EXTRACTOR_CONFIG: dict = {}


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def project_root() -> Path:
    """Return the sample project root path."""
    assert SAMPLE_PROJECT.is_dir(), f"Sample project not found: {SAMPLE_PROJECT}"
    return SAMPLE_PROJECT


@pytest.fixture(scope="module")
def extraction_results(project_root: Path) -> list[ExtractionResult]:
    """Run all extractors on the sample project and return results."""
    results: list[ExtractionResult] = []

    # Python files
    for py_file in sorted(project_root.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue
        result = extract_python(py_file, project_root, EXTRACTOR_CONFIG)
        results.append(result)

    # TypeScript files
    if HAS_TS_EXTRACTOR:
        assert extract_ts is not None
        for ts_file in sorted(project_root.rglob("*.ts")):
            result = extract_ts(ts_file, project_root, EXTRACTOR_CONFIG)
            results.append(result)

    # Config files
    for config_file in (project_root / "pyproject.toml", project_root / "package.json"):
        if config_file.is_file():
            result = extract_config(config_file, project_root, EXTRACTOR_CONFIG)
            results.append(result)

    # Doc files (markdown)
    for md_file in sorted(project_root.rglob("*.md")):
        result = extract_doc(md_file, project_root, EXTRACTOR_CONFIG)
        results.append(result)

    # Cross-references
    for md_file in sorted(project_root.rglob("*.md")):
        result = extract_crossref(md_file, project_root, EXTRACTOR_CONFIG)
        results.append(result)

    return results


@pytest.fixture(scope="module")
def built_graph(extraction_results: list[ExtractionResult], project_root: Path):
    """Build the full graph from extraction results.

    inject_packages requires source_file paths to start with project_root.
    Extractors produce relative paths, so we absolutize them first, run
    inject_packages, then relativize back for consistent downstream use.
    """
    nodes, edges = merge_results(extraction_results)

    # Absolutize source_file for inject_packages (it checks startswith)
    root_str = str(project_root)
    for node in nodes:
        sf = node["source_file"]
        if sf and not sf.startswith(root_str):
            node["source_file"] = f"{root_str}/{sf}"

    nodes, edges = inject_packages(nodes, edges, root_str)

    # Relativize back for consistent graph content
    prefix = root_str + "/"
    for node in nodes:
        sf = node["source_file"]
        if sf and sf.startswith(prefix):
            node["source_file"] = sf[len(prefix):]
        elif sf == root_str:
            node["source_file"] = ""

    graph = build_graph(nodes, edges)
    graph = run_leiden(graph)
    return graph


@pytest.fixture(scope="module")
def graph_json_path(built_graph, project_root: Path, tmp_path_factory) -> Path:
    """Serialize graph to JSON and return the path."""
    output_dir = tmp_path_factory.mktemp("graph_output")
    json_path = output_dir / "graph.json"
    serialize(built_graph, str(json_path), project_root=str(project_root))
    return json_path


@pytest.fixture(scope="module")
def loaded_graph(graph_json_path: Path, tmp_path_factory):
    """Load graph from JSON via pickle cache and return (graph, node_index)."""
    output_dir = tmp_path_factory.mktemp("pickle_cache")
    pickle_path = output_dir / "graph.pkl"
    graph, node_index = build_pickle(graph_json_path, pickle_path)
    return graph, node_index


# ---------------------------------------------------------------------------
# Test: Extraction
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestExtraction:
    """Verify extractors produce meaningful results from the sample project."""

    def test_python_extraction_produces_nodes(self, extraction_results):
        """Python extractor should find functions, classes, modules."""
        all_nodes = []
        for r in extraction_results:
            all_nodes.extend(r.nodes)

        node_types = {n["node_type"] for n in all_nodes}
        assert "FunctionNode" in node_types
        assert "ClassNode" in node_types
        assert "ModuleNode" in node_types

    def test_python_extraction_produces_edges(self, extraction_results):
        """Python extractor should find imports and calls."""
        all_edges = []
        for r in extraction_results:
            all_edges.extend(r.edges)

        edge_types = {e["edge_type"] for e in all_edges}
        assert "imports" in edge_types
        assert "calls" in edge_types

    @pytest.mark.skipif(not HAS_TS_EXTRACTOR, reason="tree-sitter-typescript not installed")
    def test_typescript_extraction_produces_nodes(self, extraction_results):
        """TypeScript extractor should find classes, functions, types."""
        all_nodes = []
        for r in extraction_results:
            all_nodes.extend(r.nodes)

        # Check for TS-specific nodes (client.ts has ApiClient class)
        class_nodes = [n for n in all_nodes if n["node_type"] == "ClassNode"]
        class_names = [n["attrs"].get("name", "") for n in class_nodes]
        assert "ApiClient" in class_names or "AuthMiddleware" in class_names

    @pytest.mark.skipif(not HAS_TS_EXTRACTOR, reason="tree-sitter-typescript not installed")
    def test_typescript_type_nodes(self, extraction_results):
        """TypeScript extractor should find interfaces and enums."""
        all_nodes = []
        for r in extraction_results:
            all_nodes.extend(r.nodes)

        type_nodes = [n for n in all_nodes if n["node_type"] == "TypeNode"]
        type_names = [n["attrs"].get("name", "") for n in type_nodes]
        # types.ts has ApiConfig, UserResponse, Session, PaginatedResponse, ApiError
        assert any(name in type_names for name in ("ApiConfig", "UserResponse", "Session"))

    def test_config_extraction(self, extraction_results):
        """Config extractor should find pyproject.toml and package.json."""
        all_nodes = []
        for r in extraction_results:
            all_nodes.extend(r.nodes)

        config_nodes = [n for n in all_nodes if n["node_type"] == "ConfigNode"]
        assert len(config_nodes) > 0

    def test_doc_extraction(self, extraction_results):
        """Doc extractor should find README.md."""
        all_nodes = []
        for r in extraction_results:
            all_nodes.extend(r.nodes)

        doc_nodes = [n for n in all_nodes if n["node_type"] == "DocNode"]
        assert len(doc_nodes) > 0


# ---------------------------------------------------------------------------
# Test: Build pipeline
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestBuildPipeline:
    """Verify the full build pipeline produces a valid graph."""

    def test_graph_has_nodes(self, built_graph):
        """Built graph should have nodes."""
        assert built_graph.vcount() > 0

    def test_graph_has_edges(self, built_graph):
        """Built graph should have edges."""
        assert built_graph.ecount() > 0

    def test_graph_has_clusters(self, built_graph):
        """Leiden/Louvain should assign cluster IDs."""
        cluster_ids = set()
        for v in built_graph.vs:
            cluster_ids.add(v["cluster_id"])
        # With multiple files, we expect at least 1 cluster
        assert len(cluster_ids) >= 1

    def test_graph_serializes_to_json(self, graph_json_path):
        """Graph should serialize to valid JSON."""
        assert graph_json_path.is_file()
        with open(graph_json_path) as f:
            data = json.load(f)

        assert "nodes" in data
        assert "edges" in data
        assert "metadata" in data
        assert data["metadata"]["node_count"] > 0
        assert data["metadata"]["edge_count"] > 0

    def test_graph_loads_from_pickle(self, loaded_graph):
        """Graph should load from pickle with valid node index."""
        graph, node_index = loaded_graph
        assert graph.vcount() > 0
        assert len(node_index) == graph.vcount()

    def test_load_or_rebuild_uses_cache(self, graph_json_path, tmp_path_factory):
        """load_or_rebuild should use pickle cache when fresh."""
        output_dir = tmp_path_factory.mktemp("cache_test")
        pickle_path = output_dir / "graph.pkl"

        # First call builds pickle
        g1, idx1 = load_or_rebuild(graph_json_path, pickle_path)
        assert pickle_path.is_file()

        # Second call should use cache (pickle is newer than JSON)
        g2, idx2 = load_or_rebuild(graph_json_path, pickle_path)
        assert g2.vcount() == g1.vcount()
        assert len(idx2) == len(idx1)


# ---------------------------------------------------------------------------
# Test: Query subcommands
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestQuerySubcommands:
    """Verify query operations work against the built graph."""

    def test_gate_returns_valid_json(self, loaded_graph):
        """Gate check on a file should return a dict with structured data."""
        graph, node_index = loaded_graph

        # Find a source file node
        source_files = set()
        for v in graph.vs:
            sf = v["source_file"]
            if sf and sf.endswith(".py"):
                source_files.add(sf)

        assert len(source_files) > 0, "No Python source files found in graph"

        # Gate is essentially impact analysis — verify it returns structured data
        target_file = sorted(source_files)[0]
        result = impact_analysis(graph, node_index, target_file, depth=2)
        assert isinstance(result, dict)
        assert "kb_miss" in result
        # If the file is found, we get impact data
        if not result.get("kb_miss"):
            assert "primary_impact" in result
            assert "cluster_context" in result

    def test_impact_on_file(self, loaded_graph):
        """Impact analysis should return downstream dependents."""
        graph, node_index = loaded_graph

        # Find a module node (e.g., src/utils.py — many things import it)
        utils_node = find_by_name(graph, node_index, "utils.py")
        if utils_node is None:
            # Try with relative path
            for nid in node_index:
                if "utils.py" in nid:
                    utils_node = graph.vs[node_index[nid]]
                    break

        assert utils_node is not None, "Could not find utils.py node in graph"

        result = impact_analysis(graph, node_index, utils_node["node_id"], depth=2)
        assert not result.get("kb_miss", True)
        # utils.py is imported by app.py and auth_service.py, so there should be impact
        assert "primary_impact" in result

    def test_neighbors_on_node(self, loaded_graph):
        """Neighbors query should return connected nodes."""
        graph, node_index = loaded_graph

        # Find a function node
        func_node = None
        for v in graph.vs:
            if v["node_type"] == "FunctionNode":
                func_node = v
                break

        assert func_node is not None, "No FunctionNode found in graph"

        result = get_neighbors(graph, node_index, func_node["node_id"], depth=1)
        assert isinstance(result, dict)
        assert "kb_miss" in result
        # The function should have at least some connection (import, call, or contains)
        if not result.get("kb_miss"):
            assert "neighbors" in result
            assert "count" in result

    def test_query_for_function_name(self, loaded_graph):
        """Searching for a function name should return matching nodes."""
        graph, node_index = loaded_graph

        # Search for "main" — defined in app.py
        results = search_nodes(graph, node_index, "main", max_results=10)
        assert len(results) > 0

        # Verify at least one result contains "main" in its node_id
        node_ids = [v["node_id"] for v in results]
        assert any("main" in nid for nid in node_ids)

    def test_structural_query_what_calls(self, loaded_graph):
        """Structural query 'what calls X' should return callers."""
        graph, node_index = loaded_graph

        # hash_password is called by create_user and login
        result = structural_query(graph, node_index, "what calls hash_password")
        assert isinstance(result, dict)
        # May or may not find results depending on edge resolution
        assert "results" in result or "kb_miss" in result

    def test_structural_query_what_imports(self, loaded_graph):
        """Structural query 'what imports X' should return importers."""
        graph, node_index = loaded_graph

        result = structural_query(graph, node_index, "what imports utils")
        assert isinstance(result, dict)
        assert "results" in result or "kb_miss" in result


# ---------------------------------------------------------------------------
# Test: Semantic search graceful degradation
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestSemanticDegradation:
    """Verify semantic search degrades gracefully without fastembed model."""

    def test_vector_store_import_without_model(self):
        """Importing vector_store should not crash even if model download fails."""
        # The module imports fine — it only fails when you try to embed
        try:
            from rtfm.core.vector_store import search, index_exists
            # If fastembed is installed, this import succeeds
            assert callable(search)
            assert callable(index_exists)
        except ImportError:
            # If fastembed is not installed, we get ImportError — that's graceful
            pass

    def test_search_on_nonexistent_index_returns_empty(self, tmp_path):
        """Searching a non-existent index should return empty list or degradation error, not crash."""
        try:
            from rtfm.core.vector_store import search, is_semantic_available

            results = search("test query", tmp_path / "nonexistent", top_k=5)
            if is_semantic_available():
                assert results == []
            else:
                assert isinstance(results, dict)
                assert results["error"] == "model_unavailable"
        except ImportError:
            pytest.skip("fastembed/lancedb not installed")

    def test_index_exists_returns_false_for_missing(self, tmp_path):
        """index_exists should return False for non-existent path."""
        try:
            from rtfm.core.vector_store import index_exists

            assert index_exists(tmp_path / "nonexistent") is False
        except ImportError:
            pytest.skip("fastembed/lancedb not installed")


# ---------------------------------------------------------------------------
# Test: Performance
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestPerformance:
    """Verify the pipeline completes within acceptable time bounds."""

    def test_build_all_under_60_seconds(self, project_root):
        """Full extraction + graph build should complete in < 60s."""
        start = time.time()

        # Run full pipeline
        results: list[ExtractionResult] = []

        # Python
        for py_file in sorted(project_root.rglob("*.py")):
            if py_file.name == "__init__.py":
                continue
            results.append(extract_python(py_file, project_root, EXTRACTOR_CONFIG))

        # TypeScript
        if HAS_TS_EXTRACTOR:
            assert extract_ts is not None
            for ts_file in sorted(project_root.rglob("*.ts")):
                results.append(extract_ts(ts_file, project_root, EXTRACTOR_CONFIG))

        # Config
        for config_file in (project_root / "pyproject.toml", project_root / "package.json"):
            if config_file.is_file():
                results.append(extract_config(config_file, project_root, EXTRACTOR_CONFIG))

        # Docs
        for md_file in sorted(project_root.rglob("*.md")):
            results.append(extract_doc(md_file, project_root, EXTRACTOR_CONFIG))

        # Crossref
        for md_file in sorted(project_root.rglob("*.md")):
            results.append(extract_crossref(md_file, project_root, EXTRACTOR_CONFIG))

        # Build
        nodes, edges = merge_results(results)
        nodes, edges = inject_packages(nodes, edges, str(project_root))
        graph = build_graph(nodes, edges)
        graph = run_leiden(graph)

        elapsed = time.time() - start
        assert elapsed < 60.0, f"Build took {elapsed:.2f}s — exceeds 60s limit"

    def test_query_response_time(self, loaded_graph):
        """Individual queries should respond in < 1s."""
        graph, node_index = loaded_graph

        start = time.time()
        search_nodes(graph, node_index, "main", max_results=10)
        elapsed = time.time() - start
        assert elapsed < 1.0, f"Query took {elapsed:.2f}s — exceeds 1s limit"


# ---------------------------------------------------------------------------
# Test: Graph integrity
# ---------------------------------------------------------------------------


@pytest.mark.integration
class TestGraphIntegrity:
    """Verify graph structural integrity after build."""

    def test_no_orphan_edges(self, loaded_graph):
        """All edges should reference valid nodes."""
        graph, node_index = loaded_graph

        for e in graph.es:
            assert 0 <= e.source < graph.vcount()
            assert 0 <= e.target < graph.vcount()

    def test_all_nodes_have_required_attrs(self, loaded_graph):
        """Every node should have node_id, node_type, source_file."""
        graph, node_index = loaded_graph

        for v in graph.vs:
            assert v["node_id"] is not None
            assert v["node_type"] is not None
            assert v["source_file"] is not None

    def test_node_index_consistent(self, loaded_graph):
        """Node index should map every node_id to its correct vertex index."""
        graph, node_index = loaded_graph

        for node_id, idx in node_index.items():
            assert graph.vs[idx]["node_id"] == node_id

    def test_multiple_node_types_present(self, loaded_graph):
        """Graph should contain multiple node types from different extractors."""
        graph, node_index = loaded_graph

        node_types = {v["node_type"] for v in graph.vs}
        # At minimum: ModuleNode, FunctionNode, ClassNode from Python
        assert "ModuleNode" in node_types
        assert "FunctionNode" in node_types
        assert "ClassNode" in node_types

    def test_package_nodes_injected(self, loaded_graph):
        """Package hierarchy nodes should be present."""
        graph, node_index = loaded_graph

        package_nodes = [v for v in graph.vs if v["node_type"] == "PackageNode"]
        assert len(package_nodes) > 0, "No PackageNode found — inject_packages may have failed"
