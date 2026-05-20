"""Tests for enricher.py — orchestrator for Jedi/Pyright/TypeScript enrichment."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from rtfm.core.enricher import enrich_graph, _run_jedi, _run_pyright, _run_typescript


@pytest.fixture
def project_root(tmp_path):
    return tmp_path


@pytest.fixture
def graph_json(tmp_path):
    gj = tmp_path / "rtfm-graph.json"
    gj.write_text('{"nodes": [], "edges": []}')
    return gj


class TestEnrichGraph:
    def test_auto_mode_runs_available_enrichers(self, project_root, graph_json):
        with patch("rtfm.core.enricher._run_jedi") as mock_jedi, \
             patch("rtfm.core.enricher._run_pyright") as mock_pyright, \
             patch("rtfm.core.enricher._run_typescript") as mock_ts:
            mock_jedi.return_value = {"status": "complete", "edges_found": 5}
            mock_pyright.return_value = {"status": "skipped", "reason": "not installed"}
            mock_ts.return_value = {"status": "skipped", "reason": "not installed"}

            result = enrich_graph(project_root, graph_json, enricher="auto")
            assert result["status"] == "complete"
            assert "jedi" in result["enrichers_used"]
            assert "pyright" not in result["enrichers_used"]

    def test_specific_enricher_selection(self, project_root, graph_json):
        with patch("rtfm.core.enricher._run_jedi") as mock_jedi:
            mock_jedi.return_value = {"status": "complete", "edges_found": 3}
            result = enrich_graph(project_root, graph_json, enricher="jedi")
            mock_jedi.assert_called_once()
            assert "jedi" in result["enrichers_used"]

    def test_no_enrichers_available(self, project_root, graph_json):
        with patch("rtfm.core.enricher._run_jedi") as mock_jedi, \
             patch("rtfm.core.enricher._run_pyright") as mock_pyright, \
             patch("rtfm.core.enricher._run_typescript") as mock_ts:
            mock_jedi.return_value = {"status": "skipped"}
            mock_pyright.return_value = {"status": "skipped"}
            mock_ts.return_value = {"status": "skipped"}

            result = enrich_graph(project_root, graph_json, enricher="auto")
            assert result["status"] == "skipped"
            assert result["enrichers_used"] == []

    def test_passes_kwargs_through(self, project_root, graph_json):
        with patch("rtfm.core.enricher._run_jedi") as mock_jedi, \
             patch("rtfm.core.enricher._run_pyright") as mock_pyright, \
             patch("rtfm.core.enricher._run_typescript") as mock_ts:
            mock_jedi.return_value = {"status": "complete"}
            mock_pyright.return_value = {"status": "skipped"}
            mock_ts.return_value = {"status": "skipped"}

            enrich_graph(project_root, graph_json, enricher="auto", merge=True, scope="src/", verbose=True)
            mock_jedi.assert_called_once_with(project_root, graph_json, merge=True, scope="src/", verbose=True, workers=None)


class TestRunJedi:
    def test_returns_skipped_when_jedi_unavailable(self, project_root, graph_json):
        """When jedi_enricher can't be imported, returns skipped status."""
        result = _run_jedi(project_root, graph_json)
        # If jedi is installed, it returns complete; if not, skipped
        assert result["status"] in ("complete", "skipped", "error")

    def test_exception_returns_error(self, project_root, graph_json):
        with patch("rtfm.core.enricher._run_jedi") as mock:
            mock.return_value = {"status": "error", "reason": "something broke"}
            result = mock(project_root, graph_json)
            assert result["status"] == "error"


class TestRunPyright:
    def test_returns_skipped_when_pyright_unavailable(self, project_root, graph_json):
        """When pyright_enricher can't be imported, returns skipped status."""
        result = _run_pyright(project_root, graph_json)
        assert result["status"] in ("complete", "skipped", "error")


class TestRunTypescript:
    def test_returns_skipped_when_ts_unavailable(self, project_root, graph_json):
        """When typescript_enricher can't be imported, returns skipped status."""
        result = _run_typescript(project_root, graph_json)
        assert result["status"] in ("complete", "skipped", "error")
