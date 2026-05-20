"""High-level graph analysis — structural queries, impact, neighbors, clusters."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

from ._backend import GraphProtocol
from .graph_store import (
    find_by_name,
    find_vertex,
    kb_miss,
    matches_project,
    search_nodes,
    vertex_to_dict,
    _get_edge_type_between,
)


# ---------------------------------------------------------------------------
# Structural query (pattern-matched natural language)
# ---------------------------------------------------------------------------


def structural_query(
    graph: GraphProtocol,
    node_index: dict[str, int],
    query: str,
    max_results: int = 10,
    project: str | None = None,
) -> dict[str, Any]:
    """Answer structural questions like 'what calls X' or 'what imports Y'."""
    patterns = [
        (r"what\s+calls?\s+(\w+)", "calls", "in"),
        (r"what\s+does\s+(\w+)\s+call", "calls", "out"),
        (r"what\s+imports?\s+(\w+)", "imports", "in"),
        (r"what\s+does\s+(\w+)\s+import", "imports", "out"),
        (r"what\s+inherits?\s+(?:from\s+)?(\w+)", "inherits", "in"),
    ]

    for pattern, edge_type, direction in patterns:
        m = re.search(pattern, query, re.IGNORECASE)
        if m:
            target_name = m.group(1)
            node = find_by_name(graph, node_index, target_name)
            if node is None:
                return kb_miss(f"Node not found: {target_name}")

            idx = node_index[node["node_id"]]
            mode = "in" if direction == "in" else "out"
            neighbors = graph.neighbors(idx, mode=mode)

            results = []
            for n_idx in neighbors:
                if mode == "in":
                    edge_list = graph.es.select(_between=([n_idx], [idx]))
                else:
                    edge_list = graph.es.select(_between=([idx], [n_idx]))
                for e in edge_list:
                    if e["edge_type"] == edge_type:
                        v = graph.vs[n_idx]
                        if project and not matches_project(v, project):
                            continue
                        d = vertex_to_dict(v)
                        d["relevance"] = f"{edge_type} relationship"
                        d["edge_metadata"] = e["metadata"]
                        results.append(d)
                        break

                if len(results) >= max_results:
                    break

            if not results:
                return kb_miss(
                    f"No {edge_type} relationships found for {target_name}"
                )

            return {"results": results, "result_count": len(results), "kb_miss": False}

    matches = search_nodes(graph, node_index, query, max_results, project)
    if not matches:
        return kb_miss(f"No nodes matching: {query}")

    results = []
    for v in matches:
        d = vertex_to_dict(v)
        d["relevance"] = "name/id match"
        results.append(d)

    return {"results": results, "result_count": len(results), "kb_miss": False}


# ---------------------------------------------------------------------------
# Node detail
# ---------------------------------------------------------------------------


def get_node_detail(
    graph: GraphProtocol, node_index: dict[str, int], node_id: str
) -> dict[str, Any]:
    """Get full detail for a single node including all edges."""
    v = find_vertex(node_index, graph, node_id)
    if v is None:
        v = find_by_name(graph, node_index, node_id)
    if v is None:
        return kb_miss(f"Node not found: {node_id}")

    idx = node_index[v["node_id"]]
    edges_in = []
    edges_out = []

    for e in graph.es.select(_target=idx):
        edges_in.append({
            "type": e["edge_type"],
            "from_node": graph.vs[e.source]["node_id"],
        })

    for e in graph.es.select(_source=idx):
        edges_out.append({
            "type": e["edge_type"],
            "to_node": graph.vs[e.target]["node_id"],
        })

    node = vertex_to_dict(v)
    node["edges_in"] = edges_in
    node["edges_out"] = edges_out

    return {"node": node, "kb_miss": False}


# ---------------------------------------------------------------------------
# Neighbors (multi-hop BFS)
# ---------------------------------------------------------------------------


def get_neighbors(
    graph: GraphProtocol,
    node_index: dict[str, int],
    node_id: str,
    edge_types: list[str] | None = None,
    direction: str = "both",
    depth: int = 1,
) -> dict[str, Any]:
    """Get neighbors of a node up to N hops, optionally filtered by edge type."""
    v = find_vertex(node_index, graph, node_id)
    if v is None:
        v = find_by_name(graph, node_index, node_id)
    if v is None:
        return kb_miss(f"Node not found: {node_id}")

    depth = min(max(depth, 1), 3)
    mode = {"in": "in", "out": "out", "both": "all"}.get(direction, "all")

    visited: set[int] = set()
    current_layer: set[int] = {node_index[v["node_id"]]}
    neighbors: list[dict[str, Any]] = []

    for d in range(1, depth + 1):
        next_layer: set[int] = set()
        for idx in current_layer:
            for n_idx in graph.neighbors(idx, mode=mode):
                if n_idx in visited or n_idx in current_layer:
                    continue

                if edge_types:
                    if direction == "in":
                        edges = graph.es.select(_between=([n_idx], [idx]))
                    else:
                        edges = graph.es.select(_between=([idx], [n_idx]))
                    matching = any(e["edge_type"] in edge_types for e in edges)
                    if not matching:
                        continue

                nv = graph.vs[n_idx]
                neighbors.append({
                    "node_id": nv["node_id"],
                    "node_type": nv["node_type"],
                    "edge_type": _get_edge_type_between(graph, idx, n_idx, direction),
                    "direction": direction,
                    "depth": d,
                })
                next_layer.add(n_idx)

        visited.update(current_layer)
        current_layer = next_layer

    return {"neighbors": neighbors, "count": len(neighbors), "kb_miss": False}


# ---------------------------------------------------------------------------
# Cluster
# ---------------------------------------------------------------------------


def get_cluster(
    graph: GraphProtocol,
    node_index: dict[str, int],
    node_id: str | None = None,
    cluster_id: int | None = None,
) -> dict[str, Any]:
    """Get all nodes in a cluster, identified by node_id or cluster_id."""
    target_cluster: int | None = cluster_id

    if node_id and target_cluster is None:
        v = find_vertex(node_index, graph, node_id)
        if v is None:
            v = find_by_name(graph, node_index, node_id)
        if v is None:
            return kb_miss(f"Node not found: {node_id}")
        target_cluster = v["cluster_id"]

    if target_cluster is None:
        return kb_miss("Provide node_id or cluster_id")

    cluster_nodes = []
    for v in graph.vs:
        if v["cluster_id"] == target_cluster:
            cluster_nodes.append({
                "node_id": v["node_id"],
                "node_type": v["node_type"],
                "source_file": v["source_file"],
            })

    if not cluster_nodes:
        return kb_miss(f"No nodes in cluster {target_cluster}")

    cluster_indices = {node_index[n["node_id"]] for n in cluster_nodes}
    cross_edges = []
    for idx in cluster_indices:
        for e in graph.es.select(_source=idx):
            target_idx = e.target
            if target_idx not in cluster_indices:
                tv = graph.vs[target_idx]
                cross_edges.append({
                    "from_node": graph.vs[idx]["node_id"],
                    "to_node": tv["node_id"],
                    "edge_type": e["edge_type"],
                    "target_cluster_id": tv["cluster_id"],
                })

    return {
        "cluster_id": target_cluster,
        "nodes": cluster_nodes,
        "size": len(cluster_nodes),
        "cross_cluster_edges": cross_edges,
        "cluster_context": {
            "cluster_id": target_cluster,
            "cluster_size": len(cluster_indices),
            "members": cluster_nodes,
        },
        "kb_miss": False,
    }


# ---------------------------------------------------------------------------
# Impact analysis (blast radius)
# ---------------------------------------------------------------------------


def impact_analysis(
    graph: GraphProtocol,
    node_index: dict[str, int],
    node_id: str,
    depth: int = 2,
) -> dict[str, Any]:
    """Compute blast radius for a node — downstream dependents and cluster context."""
    v = find_vertex(node_index, graph, node_id)
    if v is None:
        v = find_by_name(graph, node_index, node_id)
    if v is None:
        return kb_miss(f"Node not found: {node_id}")

    node_id = v["node_id"]
    cluster = v["cluster_id"]
    start_idx = node_index[node_id]

    module_idx: int | None = None
    if "::" in node_id:
        module_id = node_id.split("::")[0]
        mv = find_vertex(node_index, graph, module_id)
        if mv is not None:
            module_idx = node_index[mv["node_id"]]

    cluster_members = []
    cluster_indices: set[int] = set()
    for cv in graph.vs:
        if cv["cluster_id"] == cluster and cv["node_id"] != node_id:
            cluster_members.append({
                "node_id": cv["node_id"],
                "node_type": cv["node_type"],
                "source_file": cv["source_file"],
                "relationship": "same_cluster",
            })
            cluster_indices.add(node_index[cv["node_id"]])

    depth = min(max(depth, 1), 3)
    bfs_seeds: set[int] = {start_idx}
    if module_idx is not None:
        bfs_seeds.add(module_idx)
    visited: set[int] = set(bfs_seeds)
    current: set[int] = set(bfs_seeds)
    primary: list[dict[str, Any]] = []
    secondary: list[dict[str, Any]] = []

    for _ in range(depth):
        next_layer: set[int] = set()
        for idx in current:
            # Follow outgoing edges (downstream dependents)
            for n_idx in graph.neighbors(idx, mode="out"):
                if n_idx not in visited:
                    visited.add(n_idx)
                    next_layer.add(n_idx)
                    nv = graph.vs[n_idx]
                    primary.append({
                        "node_id": nv["node_id"],
                        "node_type": nv["node_type"],
                        "source_file": nv["source_file"],
                        "relationship": "downstream",
                        "cluster_id": nv["cluster_id"],
                    })
            # Follow incoming edges (upstream dependents — things that read/use this)
            for n_idx in graph.neighbors(idx, mode="in"):
                if n_idx not in visited:
                    visited.add(n_idx)
                    next_layer.add(n_idx)
                    nv = graph.vs[n_idx]
                    primary.append({
                        "node_id": nv["node_id"],
                        "node_type": nv["node_type"],
                        "source_file": nv["source_file"],
                        "relationship": "upstream",
                        "cluster_id": nv["cluster_id"],
                    })
        current = next_layer

    result: dict[str, Any] = {
        "primary_impact": primary,
        "secondary_impact": secondary,
        "cluster_context": {
            "cluster_id": cluster,
            "cluster_size": len(cluster_indices),
            "members": cluster_members,
        },
        "kb_miss": False,
    }
    return result


# ---------------------------------------------------------------------------
# Staleness check
# ---------------------------------------------------------------------------


def check_staleness(graph_path: Path) -> str | None:
    """Check if graph is older than the latest git commit."""
    if not graph_path.is_file():
        return None

    graph_mtime = graph_path.stat().st_mtime

    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct"],
            capture_output=True,
            text=True,
            timeout=5,
            cwd=str(graph_path.parent),
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        commit_timestamp = int(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError):
        return None

    if graph_mtime < commit_timestamp:
        return (
            "Graph was built before the latest commit. "
            "Results may not reflect recent changes."
        )

    return None


# ---------------------------------------------------------------------------
# Dark spot detection
# ---------------------------------------------------------------------------


def detect_dark_spots(
    graph: GraphProtocol,
    node_index: dict[str, int],
    scope: str | None = None,
    min_severity: int = 1,
) -> list[dict[str, Any]]:
    """Detect structural quality concerns from graph topology.

    Returns a list of modules with signals indicating potential quality issues,
    sorted by severity (number of signals triggered) descending.
    """
    # Collect module vertices
    modules = [
        v for v in graph.vs
        if v["node_type"] == "ModuleNode"
        and (not scope or v["source_file"].startswith(scope))
        and not _is_test_file(v["source_file"])
    ]

    # Build test coverage map: which modules are imported by test files
    test_vertices = [v for v in graph.vs if _is_test_file(v["source_file"] or "")]
    tested_modules: set[str] = set()
    for tv in test_vertices:
        for eid in graph.incident(tv.index, mode="out"):
            edge = graph.es[eid]
            if edge["edge_type"] in ("imports", "calls", "type_resolved_call"):
                target_v = graph.vs[edge.target]
                if target_v["node_type"] == "ModuleNode":
                    tested_modules.add(target_v["node_id"])
                else:
                    # If a test calls/imports a function, the module is tested
                    target_sf = target_v["source_file"] or ""
                    if target_sf:
                        tested_modules.add(target_sf)

    # Filename-based heuristic: test_foo.py or test_foo_bar.py tests foo.py / foo_bar.py / _foo.py
    # This catches cases where import resolution fails (src-layout packages)
    _add_filename_test_coverage(test_vertices, modules, tested_modules)

    # Build reachability map: modules whose children (functions/classes) are
    # targets of edges from other modules. This catches dynamic imports where
    # the module node itself has no inbound edge but its contents are used.
    modules_with_used_children: dict[str, int] = {}  # source_file → inbound count
    for v in graph.vs:
        if v["node_type"] in ("FunctionNode", "ClassNode"):
            sf = v["source_file"] or ""
            if not sf:
                continue
            for eid in graph.incident(v.index, mode="in"):
                edge = graph.es[eid]
                src_v = graph.vs[edge.source]
                src_sf = src_v["source_file"] or ""
                if src_sf and src_sf != sf and not _is_test_file(src_sf):
                    modules_with_used_children[sf] = modules_with_used_children.get(sf, 0) + 1

    # Build function-to-module map and count fan-out
    func_fan_out: dict[str, int] = {}  # module_source_file → max fan-out
    for v in graph.vs:
        if v["node_type"] != "FunctionNode":
            continue
        sf = v["source_file"] or ""
        out_calls = sum(
            1 for eid in graph.incident(v.index, mode="out")
            if graph.es[eid]["edge_type"] in ("calls", "type_resolved_call")
        )
        if sf not in func_fan_out or out_calls > func_fan_out[sf]:
            func_fan_out[sf] = out_calls

    # Build doc coverage map: functions with DocNode edges
    documented_funcs: set[str] = set()
    for v in graph.vs:
        if v["node_type"] == "DocNode":
            for eid in graph.incident(v.index, mode="out"):
                edge = graph.es[eid]
                if edge["edge_type"] == "documents":
                    target_v = graph.vs[edge.target]
                    documented_funcs.add(target_v["node_id"])

    results: list[dict[str, Any]] = []

    for mod in modules:
        sf = mod["source_file"]
        mod_id = mod["node_id"]
        signals: list[dict[str, str]] = []

        # 1. No test coverage
        if mod_id not in tested_modules:
            signals.append({"type": "no_test_coverage", "detail": "no test file imports this module"})

        # 2. Undocumented public functions
        funcs_in_mod = [
            v for v in graph.vs
            if v["node_type"] == "FunctionNode"
            and (v["source_file"] or "") == sf
            and not v["node_id"].rsplit("::", 1)[-1].startswith("_")
        ]
        if funcs_in_mod:
            undoc = sum(1 for f in funcs_in_mod if f["node_id"] not in documented_funcs)
            if undoc > len(funcs_in_mod) * 0.5:
                signals.append({
                    "type": "undocumented",
                    "detail": f"{undoc}/{len(funcs_in_mod)} public functions lack docstrings",
                })

        # 3. High fan-out
        max_fo = func_fan_out.get(sf, 0)
        if max_fo > 10:
            signals.append({"type": "high_fan_out", "detail": f"function with {max_fo} outbound calls"})

        # 4. High coupling (inbound) — count both direct module edges and child usage
        inbound = sum(
            1 for eid in graph.incident(mod.index, mode="in")
            if graph.vs[graph.es[eid].source]["node_type"] == "ModuleNode"
            and graph.vs[graph.es[eid].source]["source_file"] != sf
        )
        child_inbound = modules_with_used_children.get(sf, 0)
        total_inbound = inbound + child_inbound

        if total_inbound > 15:
            signals.append({"type": "high_coupling", "detail": f"{total_inbound} inbound edges from other modules"})

        # 5. Orphan (no inbound from other modules)
        # Only flag if the file is in a proper Python package (has __init__.py)
        # Standalone scripts aren't meant to be imported
        # Check both direct module edges AND edges to children (functions/classes)
        # Exclude __init__.py, __main__.py, and entry points — they're never imported
        if total_inbound == 0 and _is_in_package(sf, node_index) and not _is_entry_point(sf):
            signals.append({"type": "orphan", "detail": "0 inbound edges from other modules"})

        if len(signals) >= min_severity:
            results.append({
                "file": sf,
                "severity": len(signals),
                "signals": signals,
            })

    results.sort(key=lambda x: x["severity"], reverse=True)
    return results


def _is_test_file(path: str) -> bool:
    """Check if a file path looks like a test file."""
    if not path:
        return False
    name = path.rsplit("/", 1)[-1] if "/" in path else path
    return name.startswith("test_") or name.endswith("_test.py") or "/tests/" in path


def _is_in_package(path: str, node_index: dict[str, int]) -> bool:
    """Check if a file is inside a Python package (directory has __init__.py)."""
    if "/" not in path:
        return False
    directory = path.rsplit("/", 1)[0]
    return f"{directory}/__init__.py" in node_index


def _is_entry_point(path: str) -> bool:
    """Check if a file is a package marker or entry point (never an orphan).

    These files are invoked directly or define package structure — they're
    not meant to be imported by other modules.
    """
    if not path:
        return False
    name = path.rsplit("/", 1)[-1] if "/" in path else path
    return name in ("__init__.py", "__main__.py", "main.py", "app.py", "cli.py", "conftest.py")


def _add_filename_test_coverage(test_vertices: list, modules: list, tested_modules: set[str]) -> None:
    """Filename-based heuristic: test_foo.py tests foo.py / _foo.py.

    Handles cases where import resolution fails (src-layout packages,
    dynamic imports, PYTHONPATH-based imports).
    """
    # Build a map: base_name → list of module source_files
    # e.g. "backend" → ["plugins/rtfm/src/rtfm/core/_backend.py"]
    module_by_basename: dict[str, list[str]] = {}
    for mod in modules:
        sf = mod["source_file"]
        if not sf:
            continue
        filename = sf.rsplit("/", 1)[-1] if "/" in sf else sf
        # Strip extension and leading underscore
        base = filename.removesuffix(".py").removesuffix(".ts").removesuffix(".tsx")
        base_clean = base.lstrip("_")
        if base_clean:
            module_by_basename.setdefault(base_clean, []).append(sf)

    # For each test file, extract what it's testing from the filename
    for tv in test_vertices:
        test_sf = tv["source_file"] or ""
        if not test_sf:
            continue
        test_filename = test_sf.rsplit("/", 1)[-1] if "/" in test_sf else test_sf

        # test_foo.py → "foo", test_foo_bar.py → "foo_bar"
        base = test_filename.removesuffix(".py")
        if base.startswith("test_"):
            target_name = base[5:]  # strip "test_"
        elif base.endswith("_test"):
            target_name = base[:-5]  # strip "_test"
        else:
            continue

        # Strip common suffixes added to avoid name collisions: _coverage, _unit, _integration
        for suffix in ("_coverage", "_unit", "_integration", "_e2e"):
            if target_name.endswith(suffix):
                target_name = target_name[: -len(suffix)]
                break

        # Match against module basenames
        if target_name in module_by_basename:
            for sf in module_by_basename[target_name]:
                tested_modules.add(sf)
