"""Pyright-based enricher — adds diagnostic metadata and validates type safety.

Pyright provides:
- Per-file diagnostic counts (errors, warnings, info)
- Import resolution validation (catches broken imports Jedi misses)
- Type error detection (gate can use this as a signal)

For actual call resolution (self.x.method() → target), Jedi remains primary.
Pyright validates the graph; Jedi builds it.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


def is_pyright_available() -> bool:
    """Check if pyright is installed and runnable."""
    return shutil.which("pyright") is not None


def run_pyright(project_root: Path, *, timeout: int = 60) -> dict | None:
    """Run pyright --outputjson and return parsed output."""
    try:
        result = subprocess.run(
            ["pyright", "--outputjson", str(project_root)],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(project_root),
        )
        # Pyright exits non-zero if there are errors, but still outputs JSON
        if result.stdout.strip():
            return json.loads(result.stdout)
        return None
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[pyright_enricher] failed: {e}", file=sys.stderr)
        return None


def _parse_diagnostics(raw: dict, scope: str = "") -> dict[str, list[dict]]:
    """Group diagnostics by file path."""
    by_file: dict[str, list[dict]] = {}
    for diag in raw.get("generalDiagnostics", []):
        file_path = diag.get("file", "")
        if scope and not file_path.startswith(scope):
            continue
        if file_path not in by_file:
            by_file[file_path] = []
        by_file[file_path].append({
            "severity": diag.get("severity", "information"),
            "rule": diag.get("rule", ""),
            "message": diag.get("message", ""),
            "range": diag.get("range", {}),
        })
    return by_file


def _compute_file_health(diagnostics: list[dict]) -> dict:
    """Compute health metrics for a single file's diagnostics."""
    errors = sum(1 for d in diagnostics if d["severity"] == "error")
    warnings = sum(1 for d in diagnostics if d["severity"] == "warning")
    info = sum(1 for d in diagnostics if d["severity"] == "information")
    return {"errors": errors, "warnings": warnings, "info": info}


def enrich_with_pyright(
    project_root: Path,
    graph_json: Path,
    *,
    merge: bool = False,
    scope: str = "",
    verbose: bool = False,
) -> dict:
    """Run Pyright enrichment. Adds diagnostic metadata to graph nodes.

    Returns stats dict with diagnostic summary.
    """
    if not is_pyright_available():
        return {"status": "skipped", "enricher": "pyright", "reason": "pyright not installed"}

    raw = run_pyright(project_root)
    if raw is None:
        return {"status": "error", "enricher": "pyright", "reason": "pyright execution failed"}

    diagnostics_by_file = _parse_diagnostics(raw, scope)
    summary = raw.get("summary", {})

    total_errors = summary.get("errorCount", 0)
    total_warnings = summary.get("warningCount", 0)
    total_info = summary.get("informationCount", 0)
    files_analyzed = summary.get("filesAnalyzed", 0)

    if verbose:
        print(f"[pyright] analyzed {files_analyzed} files: "
              f"{total_errors} errors, {total_warnings} warnings", file=sys.stderr)

    # Merge diagnostic metadata into graph nodes
    if merge and graph_json.exists():
        try:
            data = json.loads(graph_json.read_text())
            nodes = data.get("nodes", [])
            updated = 0

            for node in nodes:
                source_file = node.get("source_file", "")
                if source_file in diagnostics_by_file:
                    health = _compute_file_health(diagnostics_by_file[source_file])
                    attrs = node.get("attrs", node)
                    attrs["pyright_diagnostics"] = health
                    updated += 1

            data["nodes"] = nodes
            graph_json.write_text(json.dumps(data, indent=2))

            if verbose:
                print(f"[pyright] updated {updated} nodes with diagnostic metadata", file=sys.stderr)
        except (json.JSONDecodeError, OSError) as e:
            print(f"[pyright] merge failed: {e}", file=sys.stderr)

    return {
        "status": "complete",
        "enricher": "pyright",
        "files_analyzed": files_analyzed,
        "diagnostics": {
            "errors": total_errors,
            "warnings": total_warnings,
            "info": total_info,
        },
        "files_with_issues": len(diagnostics_by_file),
    }
