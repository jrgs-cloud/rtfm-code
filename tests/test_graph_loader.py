"""Tests for cli/_graph_loader.py — graph loading for CLI commands."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtfm.cli._graph_loader import load_graph


@pytest.fixture
def state_dir_with_graph(tmp_path):
    """Create a state directory with a valid graph JSON."""
    state = tmp_path / "status"
    state.mkdir()
    data = {
        "nodes": [
            {
                "id": "src/main.py",
                "node_type": "ModuleNode",
                "cluster_id": 0,
                "source_file": "src/main.py",
                "last_updated": "2026-01-01T00:00:00",
                "checksum": "aaa",
            },
            {
                "id": "src/main.py::run",
                "node_type": "FunctionNode",
                "cluster_id": 0,
                "source_file": "src/main.py",
                "last_updated": "2026-01-01T00:00:00",
                "checksum": "aaa",
                "name": "run",
            },
        ],
        "edges": [
            {
                "source": "src/main.py",
                "target": "src/main.py::run",
                "edge_type": "contains",
                "metadata": {},
            },
        ],
    }
    json_path = state / "rtfm-graph.json"
    json_path.write_text(json.dumps(data))
    return str(state) + "/"


class TestLoadGraph:
    def test_loads_from_json(self, state_dir_with_graph):
        g, node_index = load_graph(state_dir=state_dir_with_graph)
        assert g.vcount() == 2
        assert g.ecount() == 1
        assert "src/main.py" in node_index

    def test_creates_pickle_cache(self, state_dir_with_graph):
        load_graph(state_dir=state_dir_with_graph)
        pickle_path = Path(state_dir_with_graph) / "graph.pkl"
        assert pickle_path.is_file()

    def test_second_load_uses_pickle_cache(self, state_dir_with_graph):
        """Second load uses pickle (faster) when JSON hasn't changed."""
        g1, idx1 = load_graph(state_dir=state_dir_with_graph)
        g2, idx2 = load_graph(state_dir=state_dir_with_graph)
        assert g2.vcount() == g1.vcount()
        assert set(idx2.keys()) == set(idx1.keys())

    def test_rebuilds_when_json_newer(self, state_dir_with_graph):
        """Rebuilds pickle when JSON is modified after pickle creation."""
        import os
        import time

        load_graph(state_dir=state_dir_with_graph)
        # Touch JSON to make it newer than pickle
        time.sleep(0.01)
        json_path = Path(state_dir_with_graph) / "rtfm-graph.json"
        os.utime(json_path, None)
        g, node_index = load_graph(state_dir=state_dir_with_graph)
        assert g.vcount() == 2

    def test_raises_when_no_files(self, tmp_path):
        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        with pytest.raises(FileNotFoundError, match="No graph found"):
            load_graph(state_dir=str(empty_dir) + "/")
