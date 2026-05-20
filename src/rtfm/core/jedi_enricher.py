"""Jedi-based graph enrichment — adds type-resolved edges to rtfm-graph.json."""
from __future__ import annotations

import ast
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any


class _FileTimeout(Exception):
    pass


def _call_col(node: ast.Call) -> int:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.end_col_offset - len(func.attr) if func.end_col_offset else func.col_offset
    return func.col_offset


def _setup_timeout(seconds: int):
    """Return (arm, disarm) callables. Uses SIGALRM on Unix, threading on Windows."""
    if (
        os.name != "nt"
        and hasattr(__import__("signal"), "SIGALRM")
        and threading.current_thread() is threading.main_thread()
    ):
        import signal

        def _handler(_signum, _frame):
            raise _FileTimeout()

        old = signal.signal(signal.SIGALRM, _handler)

        def arm():
            signal.alarm(seconds)

        def disarm():
            signal.alarm(0)

        def restore():
            signal.signal(signal.SIGALRM, old)
            signal.alarm(0)

        return arm, disarm, restore
    else:
        # Windows fallback: no per-file timeout (threading cannot interrupt C extensions)
        return (lambda: None), (lambda: None), (lambda: None)


# ---------------------------------------------------------------------------
# Standalone _process_file (no closures) — returns list of edge dicts
# ---------------------------------------------------------------------------

def _to_rel(abs_path: str, project_root_str: str) -> str | None:
    """Convert absolute path to project-relative path."""
    try:
        return str(Path(abs_path).relative_to(project_root_str))
    except (ValueError, TypeError):
        return None


def _resolve_node(module_path: str, name: str | None, project_root_str: str, node_index: dict) -> str | None:
    """Resolve a jedi definition to a node ID in the graph."""
    rel = _to_rel(module_path, project_root_str)
    if not rel:
        return None
    if name and f"{rel}::{name}" in node_index:
        return f"{rel}::{name}"
    return rel if rel in node_index else None


def _process_file_standalone(
    rel_path: str,
    abs_path_str: str,
    project_root_str: str,
    node_index_data: dict,
    file_timeout: int,
    verbose: bool,
) -> list[dict]:
    """Process a single file with Jedi — standalone, no closures.

    Returns a list of edge dicts. Suitable for use in multiprocessing workers.
    Each worker must have its own jedi.Project (created in the initializer).
    """
    import jedi

    # Use the per-worker project if available (set by _worker_init), else create one
    jedi_project = getattr(_process_file_standalone, "_jedi_project", None)
    if jedi_project is None:
        jedi_project = jedi.Project(path=project_root_str)

    abs_path = Path(abs_path_str)
    edges: list[dict] = []
    edges_seen: set[tuple[str, str, str]] = set()

    def _add(src: str, tgt: str, etype: str, jpath: str) -> None:
        key = (src, tgt, etype)
        if key not in edges_seen:
            edges_seen.add(key)
            edges.append({"source": src, "target": tgt, "edge_type": etype,
                          "metadata": {"confidence": "high", "jedi_module_path": jpath}})

    # Setup per-file timeout (SIGALRM works in workers — each is main thread)
    arm, disarm, restore = _setup_timeout(file_timeout)
    try:
        arm()
        source = abs_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=abs_path_str)
        script = jedi.Script(source, path=abs_path, project=jedi_project)

        for ast_node in ast.walk(tree):
            if isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func_id = f"{rel_path}::{ast_node.name}"
                if func_id not in node_index_data:
                    continue
                for child in ast.walk(ast_node):
                    if not isinstance(child, ast.Call):
                        continue
                    try:
                        names = script.goto(child.lineno, _call_col(child), follow_imports=True)
                    except Exception:
                        continue
                    for defn in names:
                        if not defn.module_path:
                            continue
                        dr = _to_rel(str(defn.module_path), project_root_str)
                        if not dr or dr == rel_path:
                            continue
                        tid = _resolve_node(str(defn.module_path), defn.name, project_root_str, node_index_data)
                        if tid and tid != func_id:
                            _add(func_id, tid, "type_resolved_call", dr)
                            if verbose:
                                print(f"  call: {func_id} -> {tid}", file=sys.stderr)
                            break

            elif isinstance(ast_node, ast.ClassDef):
                class_id = f"{rel_path}::{ast_node.name}"
                if class_id not in node_index_data:
                    continue
                for base in ast_node.bases:
                    try:
                        names = script.goto(base.lineno, base.col_offset, follow_imports=True)
                    except Exception:
                        continue
                    for defn in names:
                        if not defn.module_path:
                            continue
                        dr = _to_rel(str(defn.module_path), project_root_str)
                        if not dr or dr == rel_path:
                            continue
                        tid = _resolve_node(str(defn.module_path), defn.name, project_root_str, node_index_data)
                        if tid and tid != class_id:
                            _add(class_id, tid, "cross_file_inheritance", dr)
                            if verbose:
                                print(f"  inherit: {class_id} -> {tid}", file=sys.stderr)
                            break

            elif isinstance(ast_node, ast.ImportFrom) and ast_node.module:
                direct_module = ast_node.module.replace(".", "/") + ".py"
                for alias in ast_node.names:
                    try:
                        defs = script.goto(ast_node.lineno, alias.col_offset, follow_imports=True)
                    except Exception:
                        continue
                    for defn in defs:
                        if not defn.module_path:
                            continue
                        dr = _to_rel(str(defn.module_path), project_root_str)
                        if not dr or dr == rel_path:
                            continue
                        if dr != direct_module:
                            tid = _resolve_node(str(defn.module_path), defn.name, project_root_str, node_index_data)
                            if tid:
                                _add(rel_path, tid, "reexport_resolution", dr)
                                if verbose:
                                    print(f"  reexport: {rel_path} -> {tid}", file=sys.stderr)
                            break

        # --- File I/O resolution ---
        _resolve_file_io(source, tree, script, rel_path, node_index_data, _add, verbose)

    except _FileTimeout:
        if verbose:
            print(f"[jedi] timeout: {rel_path}", file=sys.stderr)
        return edges  # return whatever we collected before timeout
    except Exception as e:
        if verbose:
            print(f"[jedi] error in {rel_path}: {e}", file=sys.stderr)
        return edges
    finally:
        disarm()
        restore()

    return edges


# ---------------------------------------------------------------------------
# Worker initializer for multiprocessing
# ---------------------------------------------------------------------------

def _worker_init(project_root_str: str) -> None:
    """Per-worker initializer: creates a jedi.Project for this process."""
    import jedi
    _process_file_standalone._jedi_project = jedi.Project(path=project_root_str)


# ---------------------------------------------------------------------------
# Parallel entry point
# ---------------------------------------------------------------------------

def enrich_graph_parallel(
    project_root: Path,
    graph_path: Path,
    output_path: Path | None = None,
    merge: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    scope: str | None = None,
    config: dict | None = None,
    workers: int | None = None,
) -> dict:
    """Run jedi enrichment in parallel using multiprocessing.Pool.

    Falls back to sequential processing when workers=1.
    """
    try:
        import jedi  # noqa: F401 — validate availability
    except ImportError as exc:
        raise RuntimeError("jedi not installed — run: pip install 'jedi>=0.19'") from exc

    cfg = config or {}
    skip_dirs = set(cfg.get("skip_dirs", []))
    file_timeout = max(1, cfg.get("file_timeout_seconds", 10))
    effective_scope = scope if scope is not None else cfg.get("default_scope")

    if workers is None:
        from .concurrency import adaptive_workers
        workers = adaptive_workers()

    with open(graph_path) as f:
        graph_data = json.load(f)

    nodes = graph_data.get("nodes", [])
    node_index = {n["id"]: n for n in nodes}

    seen_files: set[str] = set()
    py_files: list[tuple[str, str]] = []
    for n in nodes:
        sf = n.get("source_file", "")
        if sf.endswith(".py") and sf not in seen_files:
            if effective_scope and not sf.startswith(effective_scope):
                continue
            if any(skip in sf for skip in skip_dirs):
                continue
            full = project_root / sf
            if full.exists():
                py_files.append((sf, str(full)))
                seen_files.add(sf)

    project_root_str = str(project_root.resolve())

    # Sequential fallback
    if workers <= 1 or len(py_files) <= 1:
        all_edges: list[dict] = []
        files_failed: list[str] = []
        for rel_path, abs_path_str in py_files:
            try:
                file_edges = _process_file_standalone(
                    rel_path, abs_path_str, project_root_str,
                    node_index, file_timeout, verbose,
                )
                all_edges.extend(file_edges)
            except Exception as e:
                files_failed.append(rel_path)
                if verbose:
                    print(f"[jedi] error in {rel_path}: {e}", file=sys.stderr)
    else:
        # Parallel execution with spawn context
        import multiprocessing
        ctx = multiprocessing.get_context("spawn")

        # Build args for starmap
        args_list = [
            (rel_path, abs_path_str, project_root_str, node_index, file_timeout, verbose)
            for rel_path, abs_path_str in py_files
        ]

        all_edges = []
        files_failed = []
        with ctx.Pool(processes=workers, initializer=_worker_init, initargs=(project_root_str,)) as pool:
            results = pool.starmap(_process_file_standalone, args_list)

        for i, file_edges in enumerate(results):
            if file_edges is None:
                files_failed.append(py_files[i][0])
            else:
                all_edges.extend(file_edges)

    # Deduplicate edges
    supplemental = _deduplicate_edges(all_edges)

    stats = {
        "status": "complete",
        "edges_found": len(supplemental),
        "type_resolved_call": sum(1 for e in supplemental if e["edge_type"] == "type_resolved_call"),
        "cross_file_inheritance": sum(1 for e in supplemental if e["edge_type"] == "cross_file_inheritance"),
        "reexport_resolution": sum(1 for e in supplemental if e["edge_type"] == "reexport_resolution"),
        "files_processed": len(py_files),
        "files_failed": files_failed,
        "workers_used": workers if workers > 1 and len(py_files) > 1 else 1,
    }
    if dry_run:
        stats["output_path"] = None
        stats["merged"] = False
        return stats
    if merge:
        graph_data["edges"].extend(supplemental)
        graph_data.setdefault("metadata", {})["edge_count"] = len(graph_data["edges"])
        with open(graph_path, "w") as f:
            json.dump(graph_data, f, indent=2)
        stats["output_path"] = str(graph_path)
        stats["merged"] = True
    else:
        out = output_path or (project_root / "status" / "jedi-supplemental-edges.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(supplemental, f, indent=2)
        stats["output_path"] = str(out)
        stats["merged"] = False
    return stats


def _deduplicate_edges(edges: list[dict]) -> list[dict]:
    """Deduplicate edges by (source, target, edge_type) key."""
    seen: set[tuple[str, str, str]] = set()
    result: list[dict] = []
    for edge in edges:
        key = (edge["source"], edge["target"], edge["edge_type"])
        if key not in seen:
            seen.add(key)
            result.append(edge)
    return result



# ---------------------------------------------------------------------------
# Incremental entry point — scope-limited enrichment for changed files
# ---------------------------------------------------------------------------

JEDI_EDGE_TYPES = frozenset({
    "type_resolved_call",
    "cross_file_inheritance",
    "reexport_resolution",
})


def enrich_incremental(
    project_root: Path,
    graph_path: Path,
    changed_files: list[Path],
    *,
    merge: bool = True,
    verbose: bool = False,
    config: dict | None = None,
    jedi_project: "Any | None" = None,
) -> dict:
    """Scope-limited incremental Jedi enrichment.

    Only enriches changed_files plus their direct dependents (files that have
    edges pointing TO nodes in the changed files). Removes stale Jedi edges
    from scope files before re-enriching.

    Returns stats dict with keys: status, edges_found, files_in_scope, etc.
    """
    try:
        import jedi  # noqa: F401
    except ImportError as exc:
        raise RuntimeError("jedi not installed — run: pip install 'jedi>=0.19'") from exc

    cfg = config or {}
    file_timeout = max(1, cfg.get("file_timeout_seconds", 10))

    with open(graph_path) as f:
        graph_data = json.load(f)

    nodes = graph_data.get("nodes", [])
    edges = graph_data.get("edges", [])
    node_index = {n["id"]: n for n in nodes}

    # Resolve changed files to relative paths
    project_root_resolved = project_root.resolve()
    changed_rel: set[str] = set()
    for fp in changed_files:
        try:
            rel = str(fp.resolve().relative_to(project_root_resolved))
            changed_rel.add(rel)
        except ValueError:
            # File not under project root — skip
            pass

    if not changed_rel:
        return {
            "status": "complete",
            "edges_found": 0,
            "files_in_scope": 0,
            "files_processed": 0,
            "files_failed": [],
            "skipped_reason": "no valid changed files",
        }

    # Build reverse index: for each node, which source_file does it belong to?
    # Then find dependents: files that have edges pointing TO nodes in changed files
    changed_node_ids: set[str] = set()
    for n in nodes:
        sf = n.get("source_file", "")
        if sf in changed_rel:
            changed_node_ids.add(n["id"])

    # Find dependent files: files with edges whose target is in changed_node_ids
    dependent_rel: set[str] = set()
    for edge in edges:
        if edge.get("target") in changed_node_ids:
            src_id = edge.get("source", "")
            # Find source_file for this source node
            src_node = node_index.get(src_id)
            if src_node:
                sf = src_node.get("source_file", "")
                if sf and sf not in changed_rel:
                    dependent_rel.add(sf)

    # Scope = changed + dependents
    scope_rel = changed_rel | dependent_rel

    # Filter to Python files that exist
    py_files: list[tuple[str, str]] = []
    for rel in sorted(scope_rel):
        if not rel.endswith(".py"):
            continue
        full = project_root / rel
        if full.exists():
            py_files.append((rel, str(full)))

    if not py_files:
        return {
            "status": "complete",
            "edges_found": 0,
            "files_in_scope": len(scope_rel),
            "files_processed": 0,
            "files_failed": [],
            "skipped_reason": "no Python files in scope",
        }

    # Remove old Jedi edges FROM scope files (edges whose source node is in a scope file)
    scope_node_ids: set[str] = set()
    for n in nodes:
        sf = n.get("source_file", "")
        if sf in scope_rel:
            scope_node_ids.add(n["id"])

    # Also include module-level node IDs (source_file itself as an ID)
    for rel in scope_rel:
        if rel in node_index:
            scope_node_ids.add(rel)

    retained_edges: list[dict] = []
    removed_count = 0
    for edge in edges:
        edge_type = edge.get("edge_type", "")
        source = edge.get("source", "")
        if edge_type in JEDI_EDGE_TYPES and source in scope_node_ids:
            removed_count += 1
        else:
            retained_edges.append(edge)

    # Run enrichment on scope files
    project_root_str = str(project_root.resolve())
    all_edges: list[dict] = []
    files_failed: list[str] = []

    # If a warm jedi.Project was provided, inject it into the standalone processor
    _prev_jedi_project = getattr(_process_file_standalone, "_jedi_project", None)
    if jedi_project is not None:
        _process_file_standalone._jedi_project = jedi_project

    for rel_path, abs_path_str in py_files:
        try:
            file_edges = _process_file_standalone(
                rel_path, abs_path_str, project_root_str,
                node_index, file_timeout, verbose,
            )
            all_edges.extend(file_edges)
        except Exception as e:
            files_failed.append(rel_path)
            if verbose:
                print(f"[jedi-incr] error in {rel_path}: {e}", file=sys.stderr)


    # Restore previous jedi_project state (avoid leaking into other callers)
    if jedi_project is not None:
        if _prev_jedi_project is None:
            _process_file_standalone._jedi_project = None
        else:
            _process_file_standalone._jedi_project = _prev_jedi_project
    # Deduplicate new edges
    new_edges = _deduplicate_edges(all_edges)

    stats = {
        "status": "complete",
        "edges_found": len(new_edges),
        "edges_removed": removed_count,
        "files_in_scope": len(scope_rel),
        "files_changed": len(changed_rel),
        "files_dependent": len(dependent_rel),
        "files_processed": len(py_files),
        "files_failed": files_failed,
        "type_resolved_call": sum(1 for e in new_edges if e["edge_type"] == "type_resolved_call"),
        "cross_file_inheritance": sum(1 for e in new_edges if e["edge_type"] == "cross_file_inheritance"),
        "reexport_resolution": sum(1 for e in new_edges if e["edge_type"] == "reexport_resolution"),
    }

    if merge:
        # Merge: retained edges + new edges
        graph_data["edges"] = retained_edges + new_edges
        graph_data.setdefault("metadata", {})["edge_count"] = len(graph_data["edges"])
        with open(graph_path, "w") as f:
            json.dump(graph_data, f, indent=2)
        stats["output_path"] = str(graph_path)
        stats["merged"] = True
    else:
        stats["output_path"] = None
        stats["merged"] = False

    return stats


# ---------------------------------------------------------------------------
# Original sequential entry point (backward compatible)
# ---------------------------------------------------------------------------

def enrich_graph(
    project_root: Path,
    graph_path: Path,
    output_path: Path | None = None,
    merge: bool = False,
    dry_run: bool = False,
    verbose: bool = False,
    scope: str | None = None,
    config: dict | None = None,
) -> dict:
    """Run jedi enrichment on the graph. Returns stats dict with edge counts by type.

    config keys: skip_dirs (list[str]), file_timeout_seconds (int), default_scope (str|None)
    """
    try:
        import jedi
    except ImportError as exc:
        raise RuntimeError("jedi not installed — run: pip install 'jedi>=0.19'") from exc

    cfg = config or {}
    skip_dirs = set(cfg.get("skip_dirs", []))
    file_timeout = max(1, cfg.get("file_timeout_seconds", 10))
    effective_scope = scope if scope is not None else cfg.get("default_scope")

    with open(graph_path) as f:
        graph_data = json.load(f)

    nodes = graph_data.get("nodes", [])
    node_index = {n["id"]: n for n in nodes}
    edges_seen: set[tuple[str, str, str]] = set()
    supplemental: list[dict] = []

    def _to_rel_closure(abs_path: str) -> str | None:
        try:
            return str(Path(abs_path).relative_to(project_root.resolve()))
        except (ValueError, TypeError):
            return None

    def _resolve_closure(module_path: str, name: str | None) -> str | None:
        rel = _to_rel_closure(module_path)
        if not rel:
            return None
        if name and f"{rel}::{name}" in node_index:
            return f"{rel}::{name}"
        return rel if rel in node_index else None

    def _add_closure(src: str, tgt: str, etype: str, jpath: str) -> None:
        key = (src, tgt, etype)
        if key not in edges_seen:
            edges_seen.add(key)
            supplemental.append({"source": src, "target": tgt, "edge_type": etype,
                                 "metadata": {"confidence": "high", "jedi_module_path": jpath}})

    seen_files: set[str] = set()
    py_files: list[tuple[str, Path]] = []
    for n in nodes:
        sf = n.get("source_file", "")
        if sf.endswith(".py") and sf not in seen_files:
            if effective_scope and not sf.startswith(effective_scope):
                continue
            if any(skip in sf for skip in skip_dirs):
                continue
            full = project_root / sf
            if full.exists():
                py_files.append((sf, full))
                seen_files.add(sf)

    jedi_project = jedi.Project(path=project_root)
    arm, disarm, restore = _setup_timeout(file_timeout)
    files_failed: list[str] = []
    try:
        for rel_path, abs_path in py_files:
            arm()
            try:
                _process_file(rel_path, abs_path, jedi, jedi_project,
                              node_index, _to_rel_closure, _resolve_closure, _add_closure, verbose)
            except _FileTimeout:
                files_failed.append(rel_path)
                if verbose:
                    print(f"[jedi] timeout: {rel_path}", file=sys.stderr)
            except Exception as e:
                files_failed.append(rel_path)
                if verbose:
                    print(f"[jedi] error in {rel_path}: {e}", file=sys.stderr)
            finally:
                disarm()
    finally:
        restore()

    stats = {
        "status": "complete",
        "edges_found": len(supplemental),
        "type_resolved_call": sum(1 for e in supplemental if e["edge_type"] == "type_resolved_call"),
        "cross_file_inheritance": sum(1 for e in supplemental if e["edge_type"] == "cross_file_inheritance"),
        "reexport_resolution": sum(1 for e in supplemental if e["edge_type"] == "reexport_resolution"),
        "files_processed": len(py_files),
        "files_failed": files_failed,
    }
    if dry_run:
        stats["output_path"] = None
        stats["merged"] = False
        return stats
    if merge:
        graph_data["edges"].extend(supplemental)
        graph_data.setdefault("metadata", {})["edge_count"] = len(graph_data["edges"])
        with open(graph_path, "w") as f:
            json.dump(graph_data, f, indent=2)
        stats["output_path"] = str(graph_path)
        stats["merged"] = True
    else:
        out = output_path or (project_root / "status" / "jedi-supplemental-edges.json")
        out.parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w") as f:
            json.dump(supplemental, f, indent=2)
        stats["output_path"] = str(out)
        stats["merged"] = False
    return stats


def _process_file(rel_path, abs_path, jedi, jedi_project, node_index,
                  _to_rel, _resolve, _add, verbose):
    source = abs_path.read_text(encoding="utf-8", errors="replace")
    tree = ast.parse(source, filename=str(abs_path))
    script = jedi.Script(source, path=abs_path, project=jedi_project)

    for ast_node in ast.walk(tree):
        if isinstance(ast_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func_id = f"{rel_path}::{ast_node.name}"
            if func_id not in node_index:
                continue
            for child in ast.walk(ast_node):
                if not isinstance(child, ast.Call):
                    continue
                try:
                    names = script.goto(child.lineno, _call_col(child), follow_imports=True)
                except Exception:
                    continue
                for defn in names:
                    if not defn.module_path:
                        continue
                    dr = _to_rel(str(defn.module_path))
                    if not dr or dr == rel_path:
                        continue
                    tid = _resolve(str(defn.module_path), defn.name)
                    if tid and tid != func_id:
                        _add(func_id, tid, "type_resolved_call", dr)
                        if verbose:
                            print(f"  call: {func_id} -> {tid}", file=sys.stderr)
                        break

        elif isinstance(ast_node, ast.ClassDef):
            class_id = f"{rel_path}::{ast_node.name}"
            if class_id not in node_index:
                continue
            for base in ast_node.bases:
                try:
                    names = script.goto(base.lineno, base.col_offset, follow_imports=True)
                except Exception:
                    continue
                for defn in names:
                    if not defn.module_path:
                        continue
                    dr = _to_rel(str(defn.module_path))
                    if not dr or dr == rel_path:
                        continue
                    tid = _resolve(str(defn.module_path), defn.name)
                    if tid and tid != class_id:
                        _add(class_id, tid, "cross_file_inheritance", dr)
                        if verbose:
                            print(f"  inherit: {class_id} -> {tid}", file=sys.stderr)
                        break

        elif isinstance(ast_node, ast.ImportFrom) and ast_node.module:
            direct_module = ast_node.module.replace(".", "/") + ".py"
            for alias in ast_node.names:
                try:
                    defs = script.goto(ast_node.lineno, alias.col_offset, follow_imports=True)
                except Exception:
                    continue
                for defn in defs:
                    if not defn.module_path:
                        continue
                    dr = _to_rel(str(defn.module_path))
                    if not dr or dr == rel_path:
                        continue
                    if dr != direct_module:
                        tid = _resolve(str(defn.module_path), defn.name)
                        if tid:
                            _add(rel_path, tid, "reexport_resolution", dr)
                            if verbose:
                                print(f"  reexport: {rel_path} -> {tid}", file=sys.stderr)
                        break

    # --- File I/O resolution: resolve open() arguments to file paths ---
    _resolve_file_io(source, tree, script, rel_path, node_index, _add, verbose)


def _resolve_file_io(source, tree, script, rel_path, node_index, _add, verbose):
    """Resolve open()/Path() arguments to file paths via AST constant propagation."""
    import re  # noqa: F401

    # Build lookup tables for matching resolved paths to node IDs
    known_paths: set[str] = set()
    config_paths: dict[str, str] = {}  # basename -> full node id
    for nid in node_index:
        if nid.startswith("config::"):
            clean = nid[8:]
            known_paths.add(clean)
            basename = clean.rsplit("/", 1)[-1] if "/" in clean else clean
            if basename not in config_paths:
                config_paths[basename] = nid
        elif "/" in nid and "::" not in nid:
            known_paths.add(nid)
            basename = nid.rsplit("/", 1)[-1]
            if basename not in config_paths:
                config_paths[basename] = nid

    # Pass 1: collect string constant assignments (name -> string value)
    string_vars: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name):
                val = _extract_string_from_expr(node.value)
                if val:
                    string_vars[target.id] = val
        elif isinstance(node, ast.AnnAssign) and node.value and isinstance(node.target, ast.Name):
            val = _extract_string_from_expr(node.value)
            if val:
                string_vars[node.target.id] = val

    # Pass 2: find open()/read_text()/write_text()/json.load() calls
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func_name = _get_call_name(node)
        if func_name not in ("open", "read_text", "write_text", "read_bytes", "write_bytes"):
            continue

        # For open(), resolve first argument
        resolved_path = None
        if func_name == "open" and node.args:
            resolved_path = _resolve_arg_to_path(node.args[0], string_vars)
        elif func_name in ("read_text", "write_text", "read_bytes", "write_bytes"):
            # The path is the object being called on: path.read_text()
            if isinstance(node.func, ast.Attribute):
                resolved_path = _resolve_arg_to_path(node.func.value, string_vars)

        if not resolved_path:
            continue

        # Match against known nodes
        target_id = _match_path_to_node(resolved_path, known_paths, config_paths, node_index)

        if not target_id or target_id == rel_path:
            continue

        is_write = func_name in ("write_text", "write_bytes") or _is_write_context(node, source)
        edge_type = "writes" if is_write else "reads"
        _add(rel_path, target_id, edge_type, rel_path)
        if verbose:
            print(f"  {edge_type}: {rel_path} -> {target_id}", file=sys.stderr)


def _extract_string_from_expr(node) -> str | None:
    """Extract a file path string from an AST expression."""
    # Direct string: x = "settings.json"
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        if "." in node.value and "/" in node.value or node.value.endswith((".json", ".yaml", ".yml", ".toml", ".py", ".md", ".txt")):
            return node.value
    # Path / "file": x = ROOT / "settings.json"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = _extract_string_from_expr(node.right)
        if right:
            return right
    # Path("file"): x = Path("settings.json")
    if isinstance(node, ast.Call):
        name = _get_call_name(node)
        if name == "Path" and node.args:
            return _extract_string_from_expr(node.args[0])
        if name == "join" and node.args:
            # os.path.join(..., "file.json")
            last = node.args[-1]
            return _extract_string_from_expr(last)
    return None


def _resolve_arg_to_path(node, string_vars: dict[str, str]) -> str | None:
    """Resolve a call argument to a file path string."""
    # Direct string literal
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    # Variable reference
    if isinstance(node, ast.Name) and node.id in string_vars:
        return string_vars[node.id]
    # Path / "filename"
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        right = node.right
        if isinstance(right, ast.Constant) and isinstance(right.value, str):
            return right.value
        if isinstance(right, ast.Name) and right.id in string_vars:
            return string_vars[right.id]
    # Path("file")
    if isinstance(node, ast.Call):
        name = _get_call_name(node)
        if name == "Path" and node.args:
            return _resolve_arg_to_path(node.args[0], string_vars)
    return None


def _match_path_to_node(resolved_path: str, known_paths: set, config_paths: dict, node_index: dict) -> str | None:
    """Match a resolved file path to a known graph node ID."""
    # Strip leading ./ or /
    clean = resolved_path.lstrip("./")
    if clean in known_paths:
        return clean
    if f"config::{clean}" in node_index:
        return f"config::{clean}"
    # Try basename match
    basename = clean.rsplit("/", 1)[-1] if "/" in clean else clean
    if basename in config_paths:
        return config_paths[basename]
    return None


def _get_call_name(node: ast.Call) -> str:
    """Extract the function name from a Call node."""
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _is_write_context(call_node: ast.Call, source: str) -> bool:
    """Check if an open() call is in write mode."""
    # Check for mode argument: open(path, "w") or open(path, mode="w")
    if len(call_node.args) >= 2:
        mode_arg = call_node.args[1]
        if isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str):
            return "w" in mode_arg.value or "a" in mode_arg.value
    for kw in call_node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            if isinstance(kw.value.value, str):
                return "w" in kw.value.value or "a" in kw.value.value
    return False
