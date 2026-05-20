"""Scaffold smoke tests — verify package structure and CLI entry point."""

from click.testing import CliRunner

import rtfm
from rtfm.cli import main


def test_version() -> None:
    assert rtfm.__version__ == "0.1.0"


def test_cli_help_exits_zero() -> None:
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0, result.output


def test_cli_exposes_all_subcommands() -> None:
    expected = {
        "build-all",
        "gate",
        "query",
        "search",
        "hybrid",
        "impact",
        "node",
        "neighbors",
        "cluster",
    }
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    for cmd in expected:
        assert cmd in result.output, f"subcommand '{cmd}' missing from --help output"


def test_subcommands_exit_zero_with_help() -> None:
    """Each subcommand should exit 0 when invoked with --help."""
    runner = CliRunner()
    subcommands = [
        "build-all", "gate", "query", "search",
        "hybrid", "impact", "node", "neighbors", "cluster",
    ]
    for cmd in subcommands:
        result = runner.invoke(main, [cmd, "--help"])
        assert result.exit_code == 0, f"{cmd} --help exited {result.exit_code}: {result.output}"
