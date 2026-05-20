"""Tests for incremental graph update and watcher."""

from __future__ import annotations

import json
import os
import pickle
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from rtfm.core.types import ExtractionResult, make_node, make_edge, NT_MODULE, ET_IMPORTS
from rtfm.core.incremental import (
    update_graph,
    _acquire_lock,
    _release_lock,
    _extract_file,
    SOURCE_EXTENSIONS,
)


@pytest.fixture
def state_dir(tmp_path):
    """Create a state dir with a minimal graph JSON."""
    nodes = [
        {
            "id": "src/app.py",
            "node_type": "ModuleNode",
            "cluster_id": 0,
            "source_file": "src/app.py",
            "last_updated": "2024-01-01T00:00:00",
            "checksum": "abc123",
            "name": "app",
        },
        {
            "id": "src/utils.py",
            "node_type": "ModuleNode",
            "cluster_id": 0,
            "source_file": "src/utils.py",
            "last_updated": "2024-01-01T00:00:00",
            "checksum": "def456",
            "name": "utils",
        },
        {
            "id": "src/config.py",
            "node_type": "ModuleNode",
            "cluster_id": 1,
            "source_file": "src/config.py",
            "last_updated": "2024-01-01T00:00:00",
            "checksum": "ghi789",
            "name": "config",
        },
    ]
    edges = [
        {"source": "src/app.py", "target": "src/utils.py", "edge_type": "imports", "metadata": {}},
        {"source": "src/app.py", "target": "src/config.py", "edge_type": "imports", "metadata": {}},
        {"source": "src/utils.py", "target": "src/config.py", "edge_type": "calls", "metadata": {}},
    ]
    data = {
        "nodes": nodes,
        "edges": edges,
        "metadata": {"node_count": 3, "edge_count": 3, "cluster_count": 2},
    }
    graph_path = tmp_path / "rtfm-graph.json"
    graph_path.write_text(json.dumps(data))
    return tmp_path


@pytest.fixture
def project_root(tmp_path):
    """Create a project root with source files."""
    root = tmp_path / "project"
    root.mkdir()
    src = root / "src"
    src.mkdir()
    (src / "app.py").write_text("import utils\n")
    (src / "utils.py").write_text("def helper(): pass\n")
    (src / "config.py").write_text("DB_URL = 'localhost'\n")
    return root


class TestIncrementalUpdate:
    def test_removes_old_nodes_for_changed_file(self, state_dir, project_root):
        """Changing a file removes its old nodes from the graph."""
        changed = [project_root / "src" / "utils.py"]

        with patch("rtfm.core.incremental._extract_file") as mock_extract, \
             patch("rtfm.core.graph_builder.build_graph") as mock_build, \
             patch("rtfm.core.graph_builder.run_leiden") as mock_leiden, \
             patch("rtfm.core.graph_builder.serialize"), \
             patch("rtfm.core.incremental._save_pickle_atomic"), \
             patch("rtfm.core.incremental._mark_semantic_dirty"):
            mock_extract.return_value = ExtractionResult(
                nodes=[make_node("src/utils.py", NT_MODULE, "src/utils.py", name="utils")],
                edges=[],
            )
            mock_graph = MagicMock()
            mock_build.return_value = mock_graph
            mock_leiden.return_value = mock_graph

            stats = update_graph(changed, project_root, state_dir)

        assert stats["nodes_removed"] == 1
        assert stats["nodes_added"] == 1

    def test_removes_edges_referencing_removed_nodes(self, state_dir, project_root):
        """Edges pointing to/from removed nodes are also removed."""
        changed = [project_root / "src" / "utils.py"]

        with patch("rtfm.core.incremental._extract_file") as mock_extract, \
             patch("rtfm.core.graph_builder.build_graph") as mock_build, \
             patch("rtfm.core.graph_builder.run_leiden") as mock_leiden, \
             patch("rtfm.core.graph_builder.serialize"), \
             patch("rtfm.core.incremental._save_pickle_atomic"), \
             patch("rtfm.core.incremental._mark_semantic_dirty"):
            mock_extract.return_value = ExtractionResult()
            mock_graph = MagicMock()
            mock_build.return_value = mock_graph
            mock_leiden.return_value = mock_graph

            stats = update_graph(changed, project_root, state_dir)

        # utils.py is source of 1 edge and target of 1 edge = 2 edges removed
        assert stats["edges_delta"] < 0

    def test_preserves_unrelated_nodes(self, state_dir, project_root):
        """Nodes for unchanged files remain in the graph."""
        changed = [project_root / "src" / "utils.py"]

        with patch("rtfm.core.incremental._extract_file") as mock_extract, \
             patch("rtfm.core.graph_builder.build_graph") as mock_build, \
             patch("rtfm.core.graph_builder.run_leiden") as mock_leiden, \
             patch("rtfm.core.graph_builder.serialize"), \
             patch("rtfm.core.incremental._save_pickle_atomic"), \
             patch("rtfm.core.incremental._mark_semantic_dirty"):
            mock_extract.return_value = ExtractionResult()
            mock_graph = MagicMock()
            mock_build.return_value = mock_graph
            mock_leiden.return_value = mock_graph

            stats = update_graph(changed, project_root, state_dir)

        # Only 1 node removed (utils.py), app.py and config.py preserved
        assert stats["nodes_removed"] == 1

    def test_deleted_file_removes_without_reextract(self, state_dir, project_root):
        """A deleted file has its nodes removed but no re-extraction."""
        deleted = project_root / "src" / "utils.py"
        deleted.unlink()

        with patch("rtfm.core.graph_builder.build_graph") as mock_build, \
             patch("rtfm.core.graph_builder.run_leiden") as mock_leiden, \
             patch("rtfm.core.graph_builder.serialize"), \
             patch("rtfm.core.incremental._save_pickle_atomic"), \
             patch("rtfm.core.incremental._mark_semantic_dirty"):
            mock_graph = MagicMock()
            mock_build.return_value = mock_graph
            mock_leiden.return_value = mock_graph

            stats = update_graph([deleted], project_root, state_dir)

        assert stats["nodes_removed"] == 1
        assert stats["nodes_added"] == 0

    def test_missing_graph_raises(self, tmp_path, project_root):
        """Raises FileNotFoundError if graph JSON doesn't exist."""
        with pytest.raises(FileNotFoundError, match="Run 'rtfm build-all' first"):
            update_graph(
                [project_root / "src" / "app.py"],
                project_root,
                tmp_path,
            )


class TestLockFile:
    def test_acquire_and_release(self, tmp_path):
        """Lock can be acquired and released."""
        lock_path = tmp_path / ".rtfm.lock"
        fd = _acquire_lock(lock_path)
        assert fd >= 0
        assert lock_path.exists()
        _release_lock(fd)

    def test_lock_blocks_concurrent(self, tmp_path):
        """Second lock attempt times out while first is held."""
        import rtfm.core.incremental as inc

        lock_path = tmp_path / ".rtfm.lock"
        # Reduce timeout for test speed
        original_timeout = inc.LOCK_TIMEOUT_S
        inc.LOCK_TIMEOUT_S = 0.5
        try:
            fd1 = _acquire_lock(lock_path)
            with pytest.raises(TimeoutError):
                _acquire_lock(lock_path)
            _release_lock(fd1)
        finally:
            inc.LOCK_TIMEOUT_S = original_timeout


class TestExtractFile:
    def test_routes_python_to_code_extractor(self, project_root):
        """Python files route to code_extractor."""
        py_file = project_root / "src" / "app.py"
        with patch("rtfm.extractors.code_extractor.extract") as mock:
            mock.return_value = ExtractionResult()
            _extract_file(py_file, project_root, {})
            mock.assert_called_once()

    def test_routes_markdown_to_doc_extractor(self, tmp_path):
        """Markdown files route to doc_extractor."""
        md_file = tmp_path / "README.md"
        md_file.write_text("# Hello\n")
        with patch("rtfm.extractors.doc_extractor.extract") as mock:
            mock.return_value = ExtractionResult()
            _extract_file(md_file, tmp_path, {})
            mock.assert_called_once()

    def test_unknown_extension_returns_empty(self, tmp_path):
        """Unknown extensions return empty result."""
        bin_file = tmp_path / "data.bin"
        bin_file.write_bytes(b"\x00")
        result = _extract_file(bin_file, tmp_path, {})
        assert result.nodes == []
        assert result.edges == []


class TestWatcherSkipLogic:
    def test_should_skip_pycache(self):
        from rtfm.core.watcher import _should_skip
        assert _should_skip(Path("__pycache__/module.pyc"))

    def test_should_skip_git(self):
        from rtfm.core.watcher import _should_skip
        assert _should_skip(Path(".git/objects/abc"))

    def test_should_not_skip_source(self):
        from rtfm.core.watcher import _should_skip
        assert not _should_skip(Path("src/app.py"))

    def test_should_skip_non_source_extension(self):
        from rtfm.core.watcher import _should_skip
        assert _should_skip(Path("src/image.png"))

    def test_should_skip_hidden_dir(self):
        from rtfm.core.watcher import _should_skip
        assert _should_skip(Path(".hidden/config.py"))
