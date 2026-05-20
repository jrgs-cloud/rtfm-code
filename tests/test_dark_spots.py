"""Tests for detect_dark_spots() and _is_test_file()."""
import pytest

from rtfm.core.graph_analysis import detect_dark_spots, _is_test_file


class TestIsTestFile:
    def test_test_prefix(self):
        assert _is_test_file("test_foo.py") is True

    def test_test_suffix(self):
        assert _is_test_file("foo_test.py") is True

    def test_tests_directory(self):
        assert _is_test_file("src/tests/conftest.py") is True

    def test_regular_file(self):
        assert _is_test_file("src/utils.py") is False

    def test_empty_string(self):
        assert _is_test_file("") is False

    def test_test_in_path_but_not_dir(self):
        assert _is_test_file("src/testing_utils.py") is False

    def test_nested_test_file(self):
        assert _is_test_file("plugins/rtfm/tests/test_graph.py") is True


@pytest.fixture
def mock_graph():
    """Create a minimal igraph graph for dark spots testing."""
    try:
        import igraph
    except ImportError:
        pytest.skip("igraph not installed")

    g = igraph.Graph(directed=True)

    # Add vertices: 3 modules, 1 test, 4 functions, 1 doc, 1 __init__
    g.add_vertices(10)
    g.vs["node_id"] = [
        "src/orphan.py",           # 0 - orphan module
        "src/popular.py",          # 1 - high coupling module
        "src/normal.py",           # 2 - normal module
        "tests/test_normal.py",    # 3 - test file
        "src/orphan.py::foo",      # 4 - public function (undocumented)
        "src/orphan.py::bar",      # 5 - public function (undocumented)
        "src/orphan.py::baz",      # 6 - public function (documented)
        "doc::src/orphan.py::baz", # 7 - docstring node
        "src/popular.py::fanout",  # 8 - high fan-out function
        "src/__init__.py",         # 9 - package marker (enables orphan detection)
    ]
    g.vs["node_type"] = [
        "ModuleNode", "ModuleNode", "ModuleNode", "ModuleNode",
        "FunctionNode", "FunctionNode", "FunctionNode", "DocNode",
        "FunctionNode", "ModuleNode",
    ]
    g.vs["source_file"] = [
        "src/orphan.py", "src/popular.py", "src/normal.py",
        "tests/test_normal.py",
        "src/orphan.py", "src/orphan.py", "src/orphan.py",
        "src/orphan.py",
        "src/popular.py",
        "src/__init__.py",
    ]
    g.vs["cluster_id"] = [0] * 10
    g.vs["last_updated"] = [""] * 10
    g.vs["checksum"] = [""] * 10
    g.vs["attrs"] = [{}] * 10

    # Edges
    edges = []
    edge_types = []
    edge_metadata = []

    # test imports normal (so normal has test coverage)
    edges.append((3, 2))
    edge_types.append("imports")
    edge_metadata.append({})

    # docstring documents baz
    edges.append((7, 6))
    edge_types.append("documents")
    edge_metadata.append({})

    # 16 modules import popular (high coupling)
    for i in range(16):
        g.add_vertices(1)
        g.vs[10 + i]["node_id"] = f"src/dep_{i}.py"
        g.vs[10 + i]["node_type"] = "ModuleNode"
        g.vs[10 + i]["source_file"] = f"src/dep_{i}.py"
        g.vs[10 + i]["cluster_id"] = 0
        g.vs[10 + i]["last_updated"] = ""
        g.vs[10 + i]["checksum"] = ""
        g.vs[10 + i]["attrs"] = {}
        edges.append((10 + i, 1))
        edge_types.append("imports")
        edge_metadata.append({})

    # high fan-out: fanout function calls 12 targets
    for i in range(12):
        g.add_vertices(1)
        idx = 10 + 16 + i
        g.vs[idx]["node_id"] = f"src/target_{i}.py::func"
        g.vs[idx]["node_type"] = "FunctionNode"
        g.vs[idx]["source_file"] = f"src/target_{i}.py"
        g.vs[idx]["cluster_id"] = 0
        g.vs[idx]["last_updated"] = ""
        g.vs[idx]["checksum"] = ""
        g.vs[idx]["attrs"] = {}
        edges.append((8, idx))
        edge_types.append("calls")
        edge_metadata.append({})

    g.add_edges(edges)
    g.es["edge_type"] = edge_types
    g.es["metadata"] = edge_metadata

    node_index = {v["node_id"]: v.index for v in g.vs}
    return g, node_index


class TestDetectDarkSpots:
    def test_orphan_detected(self, mock_graph):
        g, ni = mock_graph
        spots = detect_dark_spots(g, ni)
        orphan_spot = next((s for s in spots if s["file"] == "src/orphan.py"), None)
        assert orphan_spot is not None
        signal_types = [sig["type"] for sig in orphan_spot["signals"]]
        assert "orphan" in signal_types

    def test_no_test_coverage_detected(self, mock_graph):
        g, ni = mock_graph
        spots = detect_dark_spots(g, ni)
        orphan_spot = next((s for s in spots if s["file"] == "src/orphan.py"), None)
        assert orphan_spot is not None
        signal_types = [sig["type"] for sig in orphan_spot["signals"]]
        assert "no_test_coverage" in signal_types

    def test_undocumented_detected(self, mock_graph):
        g, ni = mock_graph
        spots = detect_dark_spots(g, ni)
        orphan_spot = next((s for s in spots if s["file"] == "src/orphan.py"), None)
        assert orphan_spot is not None
        signal_types = [sig["type"] for sig in orphan_spot["signals"]]
        assert "undocumented" in signal_types

    def test_high_coupling_detected(self, mock_graph):
        g, ni = mock_graph
        spots = detect_dark_spots(g, ni)
        popular_spot = next((s for s in spots if s["file"] == "src/popular.py"), None)
        assert popular_spot is not None
        signal_types = [sig["type"] for sig in popular_spot["signals"]]
        assert "high_coupling" in signal_types

    def test_high_fan_out_detected(self, mock_graph):
        g, ni = mock_graph
        spots = detect_dark_spots(g, ni)
        popular_spot = next((s for s in spots if s["file"] == "src/popular.py"), None)
        assert popular_spot is not None
        signal_types = [sig["type"] for sig in popular_spot["signals"]]
        assert "high_fan_out" in signal_types

    def test_test_files_excluded(self, mock_graph):
        g, ni = mock_graph
        spots = detect_dark_spots(g, ni)
        test_spot = next((s for s in spots if "test" in s["file"]), None)
        assert test_spot is None

    def test_scope_filter(self, mock_graph):
        g, ni = mock_graph
        spots = detect_dark_spots(g, ni, scope="src/orphan")
        assert all(s["file"].startswith("src/orphan") for s in spots)

    def test_min_severity_filter(self, mock_graph):
        g, ni = mock_graph
        spots = detect_dark_spots(g, ni, min_severity=3)
        assert all(s["severity"] >= 3 for s in spots)

    def test_sorted_by_severity(self, mock_graph):
        g, ni = mock_graph
        spots = detect_dark_spots(g, ni)
        severities = [s["severity"] for s in spots]
        assert severities == sorted(severities, reverse=True)
