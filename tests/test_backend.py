"""Tests for _backend.py — graph protocol, backend selection, NxGraph adapter."""

from __future__ import annotations

import pytest

from rtfm.core._backend import BACKEND, create_graph, NxGraph


class TestBackendSelection:
    def test_backend_is_string(self):
        assert BACKEND in ("igraph", "networkx")

    def test_create_graph_returns_valid_graph(self):
        g = create_graph()
        assert g.vcount() == 0
        assert g.ecount() == 0


class TestNxGraphAdapter:
    """Test the networkx adapter directly regardless of which backend is active."""

    @pytest.fixture
    def graph(self):
        g = NxGraph()
        return g

    def test_empty_graph(self, graph):
        assert graph.vcount() == 0
        assert graph.ecount() == 0

    def test_add_vertices(self, graph):
        graph.add_vertices(5)
        assert graph.vcount() == 5

    def test_add_edges(self, graph):
        graph.add_vertices(3)
        graph.add_edges([(0, 1), (1, 2)])
        assert graph.ecount() == 2

    def test_vertex_attributes(self, graph):
        graph.add_vertices(2)
        graph.vs[0]["name"] = "alice"
        graph.vs[1]["name"] = "bob"
        assert graph.vs[0]["name"] == "alice"
        assert graph.vs[1]["name"] == "bob"

    def test_edge_bulk_attributes(self, graph):
        """Edge attributes are set via bulk assignment on EdgeSeq."""
        graph.add_vertices(2)
        graph.add_edges([(0, 1)])
        graph.es["weight"] = [3.14]
        assert graph.es[0]["weight"] == 3.14

    def test_vertex_index(self, graph):
        graph.add_vertices(3)
        assert graph.vs[0].index == 0
        assert graph.vs[2].index == 2

    def test_edge_source_target(self, graph):
        graph.add_vertices(3)
        graph.add_edges([(0, 2)])
        assert graph.es[0].source == 0
        assert graph.es[0].target == 2

    def test_neighbors(self, graph):
        graph.add_vertices(4)
        graph.add_edges([(0, 1), (0, 2), (3, 0)])
        out_neighbors = graph.neighbors(0, mode="out")
        assert 1 in out_neighbors
        assert 2 in out_neighbors

    def test_incident(self, graph):
        graph.add_vertices(3)
        graph.add_edges([(0, 1), (0, 2), (2, 0)])
        out_edges = graph.incident(0, mode="out")
        assert len(out_edges) == 2
        in_edges = graph.incident(0, mode="in")
        assert len(in_edges) == 1

    def test_vs_iteration(self, graph):
        graph.add_vertices(3)
        graph.vs[0]["node_id"] = "a"
        graph.vs[1]["node_id"] = "b"
        graph.vs[2]["node_id"] = "c"
        ids = [v["node_id"] for v in graph.vs]
        assert ids == ["a", "b", "c"]

    def test_vs_bulk_attribute(self, graph):
        graph.add_vertices(2)
        graph.vs[0]["node_type"] = "ModuleNode"
        graph.vs[1]["node_type"] = "FunctionNode"
        types = graph.vs["node_type"]
        assert types == ["ModuleNode", "FunctionNode"]

    def test_degree(self, graph):
        graph.add_vertices(3)
        graph.add_edges([(0, 1), (0, 2)])
        degrees = graph.degree(mode="out")
        assert degrees[0] == 2
        assert degrees[1] == 0

    def test_subgraph(self, graph):
        graph.add_vertices(4)
        graph.vs[0]["name"] = "a"
        graph.vs[1]["name"] = "b"
        graph.vs[2]["name"] = "c"
        graph.vs[3]["name"] = "d"
        graph.add_edges([(0, 1), (1, 2), (2, 3)])
        sub = graph.subgraph([0, 1, 2])
        assert sub.vcount() == 3

    def test_es_bulk_attribute_read(self, graph):
        graph.add_vertices(3)
        graph.add_edges([(0, 1), (1, 2)])
        graph.es["edge_type"] = ["imports", "calls"]
        types = graph.es["edge_type"]
        assert types == ["imports", "calls"]

    def test_es_select_between(self, graph):
        graph.add_vertices(3)
        graph.add_edges([(0, 1), (1, 2), (0, 2)])
        graph.es["edge_type"] = ["imports", "calls", "inherits"]
        selected = graph.es.select(_between=([0], [1]))
        assert len(selected) == 1
        assert selected[0]["edge_type"] == "imports"

    def test_create_graph_directed(self):
        g = NxGraph(directed=True)
        g.add_vertices(2)
        g.add_edges([(0, 1)])
        assert g.neighbors(0, mode="out") == [1]
        assert g.neighbors(1, mode="out") == []
