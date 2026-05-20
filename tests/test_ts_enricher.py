"""Tests for TypeScript enricher — type resolution via subprocess."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from rtfm.core.typescript_enricher import (
    _collect_unresolved_calls,
    _find_node,
    _invoke_resolver,
    enrich_graph,
)


@pytest.fixture
def graph_with_ts(tmp_path):
    """Create a graph JSON with TS nodes and unresolved call edges."""
    nodes = [
        {"id": "src/api.ts", "node_type": "ModuleNode", "source_file": "src/api.ts",
         "cluster_id": 0, "last_updated": "", "checksum": ""},
        {"id": "src/api.ts::ApiHandler", "node_type": "ClassNode", "source_file": "src/api.ts",
         "cluster_id": 0, "last_updated": "", "checksum": ""},
        {"id": "src/api.ts::handle", "node_type": "FunctionNode", "source_file": "src/api.ts",
         "cluster_id": 0, "last_updated": "", "checksum": ""},
        {"id": "src/services/user.ts", "node_type": "ModuleNode", "source_file": "src/services/user.ts",
         "cluster_id": 1, "last_updated": "", "checksum": ""},
        {"id": "src/services/user.ts::UserService", "node_type": "ClassNode", "source_file": "src/services/user.ts",
         "cluster_id": 1, "last_updated": "", "checksum": ""},
        {"id": "src/services/user.ts::UserService::findByEmail", "node_type": "FunctionNode",
         "source_file": "src/services/user.ts", "cluster_id": 1, "last_updated": "", "checksum": ""},
    ]
    edges = [
        {"source": "src/api.ts", "target": "src/services/user.ts", "edge_type": "imports", "metadata": {}},
        {"source": "src/api.ts::handle", "target": "src/api.ts::findByEmail",
         "edge_type": "calls", "metadata": {"line": 7, "col": 24}},
    ]
    data = {"nodes": nodes, "edges": edges, "metadata": {"node_count": 6, "edge_count": 2}}
    graph_path = tmp_path / "rtfm-graph.json"
    graph_path.write_text(json.dumps(data))
    return graph_path


@pytest.fixture
def project_with_tsconfig(tmp_path):
    """Create a project root with tsconfig.json."""
    tsconfig = tmp_path / "tsconfig.json"
    tsconfig.write_text(json.dumps({"compilerOptions": {"target": "ES2020"}}))
    return tmp_path


class TestCollectUnresolvedCalls:
    def test_collects_calls_from_ts_files(self, graph_with_ts, tmp_path):
        data = json.loads(graph_with_ts.read_text())
        sites = _collect_unresolved_calls(data["nodes"], data["edges"], tmp_path)
        # "src/api.ts::findByEmail" is not in node_index, so it's unresolved
        assert len(sites) == 1
        assert sites[0]["file"] == "src/api.ts"
        assert sites[0]["line"] == 7
        assert sites[0]["callText"] == "findByEmail"

    def test_skips_resolved_targets(self, tmp_path):
        nodes = [
            {"id": "src/a.ts", "node_type": "ModuleNode", "source_file": "src/a.ts",
             "cluster_id": 0, "last_updated": "", "checksum": ""},
            {"id": "src/b.ts::helper", "node_type": "FunctionNode", "source_file": "src/b.ts",
             "cluster_id": 0, "last_updated": "", "checksum": ""},
        ]
        edges = [
            {"source": "src/a.ts::main", "target": "src/b.ts::helper",
             "edge_type": "calls", "metadata": {"line": 5, "col": 10}},
        ]
        sites = _collect_unresolved_calls(nodes, edges, tmp_path)
        # Target exists in node_index, so no unresolved calls
        assert len(sites) == 0

    def test_respects_scope_filter(self, graph_with_ts, tmp_path):
        data = json.loads(graph_with_ts.read_text())
        sites = _collect_unresolved_calls(data["nodes"], data["edges"], tmp_path, scope="lib/")
        assert len(sites) == 0

    def test_ignores_non_ts_files(self, tmp_path):
        nodes = [
            {"id": "src/app.py", "node_type": "ModuleNode", "source_file": "src/app.py",
             "cluster_id": 0, "last_updated": "", "checksum": ""},
        ]
        edges = [
            {"source": "src/app.py::main", "target": "src/app.py::unknown",
             "edge_type": "calls", "metadata": {"line": 3, "col": 0}},
        ]
        sites = _collect_unresolved_calls(nodes, edges, tmp_path)
        assert len(sites) == 0


class TestInvokeResolver:
    def test_returns_empty_when_node_not_found(self):
        with patch("rtfm.core.typescript_enricher._find_node", return_value=None):
            result = _invoke_resolver([{"file": "a.ts", "line": 1, "col": 0}], Path("/tmp"))
        assert result == []

    def test_returns_empty_on_timeout(self, tmp_path):
        with patch("subprocess.run") as mock_run:
            import subprocess
            mock_run.side_effect = subprocess.TimeoutExpired("node", 30)
            result = _invoke_resolver(
                [{"file": "a.ts", "line": 1, "col": 0}],
                tmp_path,
            )
        assert result == []

    def test_parses_valid_output(self, tmp_path):
        resolved = [{"source": "a.ts::foo", "target": "b.ts::bar", "edge_type": "type_resolved_call"}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout=json.dumps(resolved), stderr=""
            )
            with patch("rtfm.core.typescript_enricher._find_node", return_value="/usr/bin/node"):
                result = _invoke_resolver(
                    [{"file": "a.ts", "line": 1, "col": 0, "callText": "foo"}],
                    tmp_path,
                )
        assert len(result) == 1
        assert result[0]["target"] == "b.ts::bar"


class TestEnrichGraph:
    def test_skips_when_no_node(self, graph_with_ts, project_with_tsconfig):
        with patch("rtfm.core.typescript_enricher._find_node", return_value=None):
            stats = enrich_graph(project_with_tsconfig, graph_with_ts)
        assert stats["status"] == "skipped"
        assert "Node.js" in stats["reason"]

    def test_skips_when_no_tsconfig(self, graph_with_ts, tmp_path):
        # tmp_path has no tsconfig.json
        with patch("rtfm.core.typescript_enricher._find_node", return_value="/usr/bin/node"):
            stats = enrich_graph(tmp_path, graph_with_ts)
        assert stats["status"] == "skipped"
        assert "tsconfig" in stats["reason"]

    def test_complete_with_no_unresolved(self, project_with_tsconfig, tmp_path):
        # Graph with no unresolved calls
        nodes = [{"id": "src/a.ts", "node_type": "ModuleNode", "source_file": "src/a.ts",
                  "cluster_id": 0, "last_updated": "", "checksum": ""}]
        edges = []
        data = {"nodes": nodes, "edges": edges, "metadata": {}}
        graph_path = tmp_path / "g.json"
        graph_path.write_text(json.dumps(data))
        (tmp_path / "tsconfig.json").write_text("{}")

        with patch("rtfm.core.typescript_enricher._find_node", return_value="/usr/bin/node"):
            stats = enrich_graph(tmp_path, graph_path)
        assert stats["status"] == "complete"
        assert stats["edges_found"] == 0

    def test_merges_resolved_edges(self, graph_with_ts, project_with_tsconfig):
        resolved = [{
            "source": "src/api.ts::handle",
            "target": "src/services/user.ts::UserService::findByEmail",
            "edge_type": "type_resolved_call",
            "sourceFile": "src/api.ts",
            "targetFile": "src/services/user.ts",
        }]
        with patch("rtfm.core.typescript_enricher._find_node", return_value="/usr/bin/node"), \
             patch("rtfm.core.typescript_enricher._invoke_resolver", return_value=resolved):
            stats = enrich_graph(project_with_tsconfig, graph_with_ts, merge=True)

        assert stats["status"] == "complete"
        assert stats["edges_found"] == 1

        # Verify edge was written to graph
        data = json.loads(graph_with_ts.read_text())
        type_resolved = [e for e in data["edges"] if e["edge_type"] == "type_resolved_call"]
        assert len(type_resolved) == 1
        assert type_resolved[0]["target"] == "src/services/user.ts::UserService::findByEmail"

    def test_dry_run_does_not_write(self, graph_with_ts, project_with_tsconfig):
        resolved = [{
            "source": "src/api.ts::handle",
            "target": "src/services/user.ts::UserService::findByEmail",
            "edge_type": "type_resolved_call",
            "sourceFile": "src/api.ts",
            "targetFile": "src/services/user.ts",
        }]
        original = graph_with_ts.read_text()
        with patch("rtfm.core.typescript_enricher._find_node", return_value="/usr/bin/node"), \
             patch("rtfm.core.typescript_enricher._invoke_resolver", return_value=resolved):
            stats = enrich_graph(project_with_tsconfig, graph_with_ts, dry_run=True)

        assert stats["edges_found"] == 1
        assert stats["merged"] is False
        assert graph_with_ts.read_text() == original
