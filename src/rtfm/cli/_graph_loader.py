"""Graph loading helpers for CLI commands."""

from __future__ import annotations

import pickle
import sys
from pathlib import Path

from rtfm.core.graph_store import load_or_rebuild


def load_graph(state_dir: str = "status/"):
    """Load graph from pickle/JSON in state_dir. Returns (graph, node_index)."""
    state = Path(state_dir)
    json_path = state / "rtfm-graph.json"
    pickle_path = state / "graph.pkl"

    if not json_path.is_file() and not pickle_path.is_file():
        raise FileNotFoundError(
            f"No graph found in {state_dir}. Run 'build-all' first."
        )

    return load_or_rebuild(json_path, pickle_path)
