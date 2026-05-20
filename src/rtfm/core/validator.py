"""Runtime validator — compare graph edges against test coverage data."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path
from typing import Any


def _load_coverage_python(coverage_path: Path) -> dict[str, set[int]]:
    """Load Python coverage data from .coverage SQLite database.

    Returns: {filename: set of executed line numbers}
    """
    if not coverage_path.exists():
        raise FileNotFoundError(f"Coverage file not found: {coverage_path}")

    conn = sqlite3.connect(str(coverage_path))
    try:
        cursor = conn.cursor()
        # coverage.py 5+ uses SQLite format
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in cursor.fetchall()}

        if "line_bits" not in tables:
            raise ValueError(f"Not a valid coverage.py database: {coverage_path}")

        # Get file mapping
        cursor.execute("SELECT id, path FROM file")
        file_map = {row[0]: row[1] for row in cursor.fetchall()}

        # Get line data
        result: dict[str, set[int]] = {}
        cursor.execute("SELECT file_id, numbits FROM line_bits")
        for file_id, numbits in cursor.fetchall():
            if file_id not in file_map:
                continue
            filepath = file_map[file_id]
            lines = _numbits_to_lines(numbits)
            result[filepath] = lines

        return result
    finally:
        conn.close()


def _numbits_to_lines(numbits: bytes) -> set[int]:
    """Convert coverage.py numbits format to set of line numbers."""
    lines: set[int] = set()
    for byte_idx, byte_val in enumerate(numbits):
        for bit_idx in range(8):
            if byte_val & (1 << bit_idx):
                lines.add(byte_idx * 8 + bit_idx)
    return lines


def _load_coverage_json(coverage_path: Path) -> dict[str, set[int]]:
    """Load coverage data from coverage-final.json (Istanbul/c8 format).

    Returns: {filename: set of executed line numbers}
    """
    if not coverage_path.exists():
        raise FileNotFoundError(f"Coverage file not found: {coverage_path}")

    with open(coverage_path) as f:
        data = json.load(f)

    result: dict[str, set[int]] = {}
    for filepath, file_cov in data.items():
        lines: set[int] = set()
        # Istanbul format: statementMap + s (statement hits)
        statement_map = file_cov.get("statementMap", {})
        hits = file_cov.get("s", {})
        for stmt_id, count in hits.items():
            if int(count) > 0 and stmt_id in statement_map:
                stmt = statement_map[stmt_id]
                start_line = stmt.get("start", {}).get("line", 0)
                end_line = stmt.get("end", {}).get("line", start_line)
                for line in range(start_line, end_line + 1):
                    lines.add(line)
        if lines:
            result[filepath] = lines

    return result


def _load_coverage(coverage_path: Path) -> dict[str, set[int]]:
    """Auto-detect coverage format and load."""
    suffix = coverage_path.suffix.lower()
    name = coverage_path.name.lower()

    if suffix == ".json" or name.endswith(".json"):
        return _load_coverage_json(coverage_path)
    else:
        # Try SQLite (coverage.py format)
        return _load_coverage_python(coverage_path)


def _relativize_paths(
    coverage: dict[str, set[int]], project_root: Path
) -> dict[str, set[int]]:
    """Convert absolute paths in coverage data to relative paths."""
    result: dict[str, set[int]] = {}
    root_str = str(project_root.resolve())
    for filepath, lines in coverage.items():
        if filepath.startswith(root_str):
            rel = filepath[len(root_str):].lstrip("/")
        elif filepath.startswith("/"):
            # Absolute but not under project root — skip
            continue
        else:
            rel = filepath
        result[rel] = lines
    return result


def validate(
    graph_path: Path,
    coverage_path: Path,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Compare graph edges against coverage data.

    Returns a validation report with:
    - validated_edges: edges where both source and target files have coverage
    - unvalidated_edges: edges where source or target lacks coverage
    - phantom_edges: edges in graph but source/target lines never executed
    - coverage_ratio: validated / (validated + unvalidated)
    - phantoms: list of phantom edge details
    - blind_spots: files with coverage but no graph edges
    """
    if not graph_path.exists():
        raise FileNotFoundError(f"Graph not found: {graph_path}. Run 'rtfm build-all' first.")

    with open(graph_path) as f:
        graph_data = json.load(f)

    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])

    # Load and relativize coverage
    coverage = _load_coverage(coverage_path)
    if project_root:
        coverage = _relativize_paths(coverage, project_root)

    # Build node → source_file mapping
    node_to_file: dict[str, str] = {}
    for node in nodes:
        node_to_file[node["id"]] = node.get("source_file", "")

    # Get set of files with coverage
    covered_files = set(coverage.keys())

    # Classify edges
    validated_edges: list[dict] = []
    unvalidated_edges: list[dict] = []
    phantom_edges: list[dict] = []

    for edge in edges:
        source_id = edge["source"]
        target_id = edge["target"]
        edge_type = edge["edge_type"]

        # Skip structural edges (contains, documents) — they're not runtime
        if edge_type in ("contains", "documents", "configures"):
            continue

        source_file = node_to_file.get(source_id, source_id.split("::")[0])
        target_file = node_to_file.get(target_id, target_id.split("::")[0])

        source_covered = source_file in covered_files
        target_covered = target_file in covered_files

        if source_covered and target_covered:
            # Both files have coverage — check if the specific lines were hit
            # For now, file-level validation (line-level requires AST line mapping)
            validated_edges.append(edge)
        elif not source_covered and not target_covered:
            # Neither file has coverage — phantom candidate
            phantom_edges.append({
                "source": source_id,
                "target": target_id,
                "edge_type": edge_type,
                "reason": "edge in graph but neither source nor target file has test coverage",
            })
        else:
            unvalidated_edges.append(edge)

    # Find blind spots: files with coverage but no graph nodes
    graph_files = {n.get("source_file", "") for n in nodes}
    blind_spot_files = covered_files - graph_files - {""}

    total_classifiable = len(validated_edges) + len(unvalidated_edges) + len(phantom_edges)
    coverage_ratio = (
        len(validated_edges) / total_classifiable if total_classifiable > 0 else 0.0
    )

    return {
        "validated_edges": len(validated_edges),
        "unvalidated_edges": len(unvalidated_edges),
        "phantom_edges": len(phantom_edges),
        "coverage_ratio": round(coverage_ratio, 3),
        "phantoms": phantom_edges[:50],  # Cap output size
        "blind_spots": [
            {"file": f, "reason": "file has test coverage but no graph representation"}
            for f in sorted(blind_spot_files)[:20]
        ],
        "total_edges_analyzed": total_classifiable,
    }
