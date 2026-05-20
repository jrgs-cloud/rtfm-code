"""TypeScript enricher — resolves attribute call chains via TS Compiler API subprocess."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


RESOLVER_SCRIPT = Path(__file__).parent / "ts_resolver.mjs"
DEFAULT_TIMEOUT = 30


def _find_node() -> str | None:
    """Find node binary."""
    return shutil.which("node")


def _collect_unresolved_calls(
    nodes: list[dict],
    edges: list[dict],
    project_root: Path,
    scope: str | None = None,
) -> list[dict]:
    """Collect call sites from TS files that have unresolved targets.

    An unresolved call is one where the edge target contains '::' but the
    target node doesn't exist in the graph (local scope assumption).
    """
    node_index = {n["id"] for n in nodes}
    ts_files: set[str] = set()

    for n in nodes:
        sf = n.get("source_file", "")
        if sf.endswith((".ts", ".tsx")) and sf not in ts_files:
            if scope and not sf.startswith(scope):
                continue
            ts_files.add(sf)

    # Find edges from TS files where target is unresolved
    call_sites: list[dict] = []
    for edge in edges:
        if edge["edge_type"] != "calls":
            continue
        source = edge["source"]
        target = edge["target"]
        # Source must be in a TS file
        source_file = source.split("::")[0] if "::" in source else source
        if source_file not in ts_files:
            continue
        # Target must be unresolved (not in node index)
        if target in node_index:
            continue

        # Extract line/col from metadata if available
        meta = edge.get("metadata", {})
        line = meta.get("line", 0)
        col = meta.get("col", 0)
        call_text = target.split("::")[-1] if "::" in target else target

        if line > 0:
            call_sites.append({
                "file": source_file,
                "line": line,
                "col": col,
                "callText": call_text,
            })

    return call_sites


def _invoke_resolver(
    call_sites: list[dict],
    project_root: Path,
    timeout: int = DEFAULT_TIMEOUT,
) -> list[dict]:
    """Invoke ts_resolver.mjs subprocess with call sites."""
    node_bin = _find_node()
    if not node_bin:
        return []

    input_data = json.dumps({
        "projectRoot": str(project_root),
        "callSites": call_sites,
    })

    try:
        result = subprocess.run(
            [node_bin, str(RESOLVER_SCRIPT)],
            input=input_data,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(project_root),
        )
    except subprocess.TimeoutExpired:
        print("[ts-enricher] Resolver timed out", file=sys.stderr)
        return []
    except FileNotFoundError:
        print("[ts-enricher] Node.js not found", file=sys.stderr)
        return []

    if result.returncode != 0:
        if result.stderr:
            print(f"[ts-enricher] {result.stderr.strip()}", file=sys.stderr)
        return []

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        print("[ts-enricher] Invalid JSON output from resolver", file=sys.stderr)
        return []


def enrich_graph(
    project_root: Path,
    graph_path: Path,
    output_path: Path | None = None,
    merge: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    scope: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Run TypeScript type resolution enrichment on the graph.

    Returns stats dict with edge counts.
    """
    node_bin = _find_node()
    if not node_bin:
        return {"status": "skipped", "reason": "Node.js not installed"}

    if not RESOLVER_SCRIPT.exists():
        return {"status": "skipped", "reason": "ts_resolver.mjs not found"}

    # Check for tsconfig.json
    tsconfig = project_root / "tsconfig.json"
    if not tsconfig.exists():
        # Search parent dirs
        found = False
        for parent in project_root.parents:
            if (parent / "tsconfig.json").exists():
                found = True
                break
        if not found:
            return {"status": "skipped", "reason": "No tsconfig.json found"}

    with open(graph_path) as f:
        graph_data = json.load(f)

    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])

    # Collect unresolved call sites
    call_sites = _collect_unresolved_calls(nodes, edges, project_root, scope)

    if not call_sites:
        return {
            "status": "complete",
            "edges_found": 0,
            "call_sites_collected": 0,
            "reason": "No unresolved TS call sites found",
        }

    if verbose:
        print(f"[ts-enricher] Collected {len(call_sites)} unresolved call sites", file=sys.stderr)

    # Invoke resolver
    resolved = _invoke_resolver(call_sites, project_root, timeout=timeout)

    # Deduplicate and validate resolved edges
    node_index = {n["id"] for n in nodes}
    edges_seen: set[tuple[str, str, str]] = {
        (e["source"], e["target"], e["edge_type"]) for e in edges
    }
    supplemental: list[dict] = []

    for r in resolved:
        source = r.get("source", "")
        target = r.get("target", "")
        edge_type = r.get("edge_type", "type_resolved_call")

        # Validate target exists in graph (or at least the file does)
        target_file = r.get("targetFile", "")
        if target not in node_index and target_file not in node_index:
            # Try just the file as target
            if target_file in node_index:
                target = target_file
            else:
                continue

        key = (source, target, edge_type)
        if key not in edges_seen:
            edges_seen.add(key)
            supplemental.append({
                "source": source,
                "target": target,
                "edge_type": edge_type,
                "metadata": {"confidence": "high", "resolver": "typescript"},
            })

    stats = {
        "status": "complete",
        "call_sites_collected": len(call_sites),
        "edges_found": len(supplemental),
        "type_resolved_call": len(supplemental),
    }

    if dry_run:
        stats["merged"] = False
        return stats

    if merge and supplemental:
        graph_data["edges"].extend(supplemental)
        graph_data.setdefault("metadata", {})["edge_count"] = len(graph_data["edges"])
        with open(graph_path, "w") as f:
            json.dump(graph_data, f, indent=2)
        stats["merged"] = True
    elif not merge and supplemental:
        out = output_path or graph_path.parent / "ts-enrichment.json"
        with open(out, "w") as f:
            json.dump(supplemental, f, indent=2)
        stats["output_path"] = str(out)

    return stats
