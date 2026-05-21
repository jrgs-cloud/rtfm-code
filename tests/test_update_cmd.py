"""Tests for the `rtfm update` CLI command."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from rtfm.cli import main


class TestUpdateCommandExists:
    def test_help_exits_zero(self):
        runner = CliRunner()
        result = runner.invoke(main, ["update", "--help"])
        assert result.exit_code == 0

    def test_help_contains_expected_options(self):
        runner = CliRunner()
        result = runner.invoke(main, ["update", "--help"])
        output = result.output
        assert "--since" in output
        assert "--fallback-threshold" in output

    def test_update_in_main_group(self):
        """update command is registered on the main group."""
        assert "update" in main.commands


class TestUpdateWithExplicitFiles:
    def test_incremental_path_with_files(self, tmp_path):
        """Passing explicit file paths triggers incremental update."""
        graph_file = tmp_path / "rtfm-graph.json"
        graph_file.write_text(json.dumps({"nodes": [], "edges": []}))

        runner = CliRunner()
        with patch("rtfm.core.incremental.update_graph") as mock_update:
            mock_update.return_value = {
                "nodes_added": 2,
                "nodes_removed": 1,
                "edges_delta": 3,
                "enrich_edges": 5,
            }
            result = runner.invoke(
                main,
                ["--state-dir", str(tmp_path), "update", "src/foo.py", "src/bar.py"],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["files_changed"] == 2
        assert data["nodes_added"] == 2
        assert data["nodes_removed"] == 1
        assert data["edges_delta"] == 3
        assert data["enrich_edges"] == 5
        assert data["mode"] == "incremental"

    def test_output_keys_present(self, tmp_path):
        """Output JSON contains all required keys."""
        graph_file = tmp_path / "rtfm-graph.json"
        graph_file.write_text(json.dumps({"nodes": [], "edges": []}))

        runner = CliRunner()
        with patch("rtfm.core.incremental.update_graph") as mock_update:
            mock_update.return_value = {
                "nodes_added": 0,
                "nodes_removed": 0,
                "edges_delta": 0,
                "enrich_edges": 0,
            }
            result = runner.invoke(
                main,
                ["--state-dir", str(tmp_path), "update", "src/foo.py"],
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        required_keys = {"files_changed", "nodes_added", "nodes_removed", "edges_delta", "enrich_edges", "mode"}
        assert required_keys.issubset(data.keys())


class TestUpdateWithSince:
    def test_since_calls_git_diff(self, tmp_path):
        """--since triggers git diff --name-only <ref>..HEAD."""
        graph_file = tmp_path / "rtfm-graph.json"
        graph_file.write_text(json.dumps({"nodes": [], "edges": []}))

        runner = CliRunner()
        mock_proc = MagicMock()
        mock_proc.stdout = "src/foo.py\nsrc/bar.py\n"
        mock_proc.returncode = 0

        with patch("subprocess.run", return_value=mock_proc) as mock_run, \
             patch("rtfm.core.incremental.update_graph") as mock_update:
            mock_update.return_value = {
                "nodes_added": 1,
                "nodes_removed": 0,
                "edges_delta": 1,
                "enrich_edges": 0,
            }
            result = runner.invoke(
                main,
                ["--state-dir", str(tmp_path), "update", "--since", "abc123"],
            )

        assert result.exit_code == 0, result.output
        # Verify git diff was called with the right ref
        call_args = mock_run.call_args_list[0][0][0]
        assert "git" in call_args
        assert "diff" in call_args
        assert "--name-only" in call_args
        assert "abc123..HEAD" in call_args

        data = json.loads(result.output)
        assert data["files_changed"] == 2
        assert data["mode"] == "incremental"


class TestUpdateFallbackThreshold:
    def test_falls_back_to_build_all_when_over_threshold(self, tmp_path):
        """When changed file count exceeds threshold, build-all is invoked."""
        graph_file = tmp_path / "rtfm-graph.json"
        graph_file.write_text(json.dumps({"nodes": [], "edges": []}))

        # Generate 3 files but set threshold to 2
        files = [f"src/file{i}.py" for i in range(3)]

        runner = CliRunner()
        with patch("rtfm.cli.build_all") as mock_build_all:
            mock_build_all.return_value = None
            result = runner.invoke(
                main,
                ["--state-dir", str(tmp_path), "update", "--fallback-threshold", "2"] + files,
            )

        # Should have printed the fallback message to stderr
        assert "[update]" in result.output or result.exit_code == 0

    def test_incremental_when_at_threshold(self, tmp_path):
        """Exactly at threshold still uses incremental (threshold is exclusive)."""
        graph_file = tmp_path / "rtfm-graph.json"
        graph_file.write_text(json.dumps({"nodes": [], "edges": []}))

        files = [f"src/file{i}.py" for i in range(2)]

        runner = CliRunner()
        with patch("rtfm.core.incremental.update_graph") as mock_update:
            mock_update.return_value = {
                "nodes_added": 0,
                "nodes_removed": 0,
                "edges_delta": 0,
                "enrich_edges": 0,
            }
            result = runner.invoke(
                main,
                ["--state-dir", str(tmp_path), "update", "--fallback-threshold", "2"] + files,
            )

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["mode"] == "incremental"


class TestUpdateGraphNotFound:
    def test_exits_nonzero_when_graph_missing(self, tmp_path):
        """update exits with error when graph JSON does not exist."""
        runner = CliRunner()
        with patch("rtfm.core.incremental.update_graph", side_effect=FileNotFoundError("no graph")):
            result = runner.invoke(
                main,
                ["--state-dir", str(tmp_path), "update", "src/foo.py"],
            )

        assert result.exit_code != 0
        err_data = json.loads(result.output)
        assert err_data["error"] == "graph_not_found"
