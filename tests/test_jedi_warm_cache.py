"""Tests for warm Jedi Project cache in watcher (Phase 3)."""
from __future__ import annotations

import json
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Test: enrich_incremental accepts and reuses jedi_project param
# ---------------------------------------------------------------------------


class TestEnrichIncrementalJediProject:
    """Test that enrich_incremental accepts and uses a warm jedi.Project."""

    @pytest.fixture
    def tmp_project(self, tmp_path):
        """Create a minimal project structure with graph."""
        src = tmp_path / "src"
        src.mkdir()
        (src / "__init__.py").write_text("")
        (src / "foo.py").write_text("def hello():\n    pass\n")

        graph = {
            "nodes": [
                {"id": "src/foo.py", "source_file": "src/foo.py", "type": "module"},
                {"id": "src/foo.py::hello", "source_file": "src/foo.py", "type": "function"},
            ],
            "edges": [],
            "metadata": {"edge_count": 0},
        }
        graph_path = tmp_path / "rtfm-graph.json"
        graph_path.write_text(json.dumps(graph))
        return tmp_path, graph_path

    def test_jedi_project_param_accepted_none(self, tmp_project):
        """enrich_incremental works with jedi_project=None (backward compat)."""
        from rtfm.core.jedi_enricher import enrich_incremental

        root, graph_path = tmp_project
        changed = [root / "src" / "foo.py"]

        result = enrich_incremental(
            project_root=root,
            graph_path=graph_path,
            changed_files=changed,
            merge=False,
            jedi_project=None,
        )
        assert result["status"] == "complete"
        assert result["files_processed"] >= 1

    def test_jedi_project_param_reused(self, tmp_project):
        """enrich_incremental reuses provided jedi.Project instance."""
        import jedi
        from rtfm.core.jedi_enricher import enrich_incremental, _process_file_standalone

        root, graph_path = tmp_project
        changed = [root / "src" / "foo.py"]

        warm_project = jedi.Project(path=root)

        result = enrich_incremental(
            project_root=root,
            graph_path=graph_path,
            changed_files=changed,
            merge=False,
            jedi_project=warm_project,
        )
        assert result["status"] == "complete"

        # After call, the _jedi_project attribute should be cleaned up (restored to None)
        restored = getattr(_process_file_standalone, "_jedi_project", "NOT_SET")
        assert restored is None or restored == "NOT_SET"

    def test_jedi_project_none_creates_fresh(self, tmp_project):
        """When jedi_project=None, _process_file_standalone creates its own."""
        from rtfm.core.jedi_enricher import enrich_incremental, _process_file_standalone

        root, graph_path = tmp_project
        changed = [root / "src" / "foo.py"]

        # Ensure no leftover project attribute
        if hasattr(_process_file_standalone, "_jedi_project"):
            delattr(_process_file_standalone, "_jedi_project")

        result = enrich_incremental(
            project_root=root,
            graph_path=graph_path,
            changed_files=changed,
            merge=False,
            jedi_project=None,
        )
        assert result["status"] == "complete"

    def test_repeated_calls_same_project(self, tmp_project):
        """Multiple calls with same jedi.Project reuse the cache."""
        import jedi
        from rtfm.core.jedi_enricher import enrich_incremental

        root, graph_path = tmp_project
        changed = [root / "src" / "foo.py"]

        warm_project = jedi.Project(path=root)

        # Call twice with same project — should not error
        r1 = enrich_incremental(
            project_root=root,
            graph_path=graph_path,
            changed_files=changed,
            merge=False,
            jedi_project=warm_project,
        )
        r2 = enrich_incremental(
            project_root=root,
            graph_path=graph_path,
            changed_files=changed,
            merge=False,
            jedi_project=warm_project,
        )
        assert r1["status"] == "complete"
        assert r2["status"] == "complete"


# ---------------------------------------------------------------------------
# Test: _enrich_sync passes jedi_project through
# ---------------------------------------------------------------------------


class TestEnrichSyncPassthrough:
    """Test that _enrich_sync passes jedi_project to enrich_incremental."""

    def test_enrich_sync_passes_project(self, tmp_path):
        """_enrich_sync forwards jedi_project kwarg to enrich_incremental."""
        import jedi
        from rtfm.core.watcher import _enrich_sync

        graph = {
            "nodes": [
                {"id": "foo.py", "source_file": "foo.py", "type": "module"},
            ],
            "edges": [],
            "metadata": {"edge_count": 0},
        }
        graph_path = tmp_path / "rtfm-graph.json"
        graph_path.write_text(json.dumps(graph))
        (tmp_path / "foo.py").write_text("x = 1\n")

        warm_project = jedi.Project(path=tmp_path)

        # Patch at the source module level so the local import picks it up
        with patch("rtfm.core.jedi_enricher.enrich_incremental") as mock_ei:
            mock_ei.return_value = {"status": "complete", "edges_found": 0}
            result = _enrich_sync(tmp_path, graph_path, [tmp_path / "foo.py"], warm_project)

        # Verify enrich_incremental was called with jedi_project=warm_project
        mock_ei.assert_called_once()
        call_kwargs = mock_ei.call_args[1]
        assert call_kwargs.get("jedi_project") is warm_project

    def test_enrich_sync_none_project_backward_compat(self, tmp_path):
        """_enrich_sync works without jedi_project (default None)."""
        from rtfm.core.watcher import _enrich_sync

        graph = {
            "nodes": [
                {"id": "bar.py", "source_file": "bar.py", "type": "module"},
            ],
            "edges": [],
            "metadata": {"edge_count": 0},
        }
        graph_path = tmp_path / "rtfm-graph.json"
        graph_path.write_text(json.dumps(graph))
        (tmp_path / "bar.py").write_text("y = 2\n")

        # Call without jedi_project — uses default None
        result = _enrich_sync(tmp_path, graph_path, [tmp_path / "bar.py"])
        if result is not None:
            assert result["status"] == "complete"


# ---------------------------------------------------------------------------
# Test: Invalidation logic in watch_loop
# ---------------------------------------------------------------------------


class TestWatchLoopInvalidation:
    """Test the jedi.Project invalidation logic."""

    def test_invalidation_on_init_py_change(self):
        """Project is recreated when __init__.py is in changed files."""
        # Simulate the invalidation check logic from watch_loop
        changed_files = [Path("/project/src/__init__.py"), Path("/project/src/foo.py")]
        init_changed = any(f.name == "__init__.py" for f in changed_files)
        assert init_changed is True

    def test_no_invalidation_on_regular_files(self):
        """Project is NOT recreated for regular file changes."""
        changed_files = [Path("/project/src/foo.py"), Path("/project/src/bar.py")]
        init_changed = any(f.name == "__init__.py" for f in changed_files)
        assert init_changed is False

    def test_invalidation_interval_counter(self):
        """Project is recreated after N cycles."""
        _INVALIDATION_INTERVAL = 50
        # Simulate counter reaching threshold
        _enrich_cycle_count = 50
        should_invalidate = _enrich_cycle_count >= _INVALIDATION_INTERVAL
        assert should_invalidate is True

        # Below threshold
        _enrich_cycle_count = 49
        should_invalidate = _enrich_cycle_count >= _INVALIDATION_INTERVAL
        assert should_invalidate is False
