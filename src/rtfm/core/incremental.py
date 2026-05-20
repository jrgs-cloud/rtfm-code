"""Incremental graph update — surgical node/edge replacement per file."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import pickle
import tempfile
import time
from pathlib import Path
from typing import Any

from .types import ExtractionResult, NodeDict, EdgeDict

logger = logging.getLogger(__name__)

LOCK_TIMEOUT_S = 10
LOCK_RETRY_INTERVAL_S = 0.2

SOURCE_EXTENSIONS = {
    ".py": "code",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".json": "config",
    ".yaml": "config",
    ".yml": "config",
    ".toml": "config",
    ".md": "doc",
}


def _acquire_lock(lock_path: Path) -> int:
    """Acquire an exclusive file lock with timeout. Returns the fd."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
    deadline = time.monotonic() + LOCK_TIMEOUT_S
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd
        except OSError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise TimeoutError(f"Could not acquire lock {lock_path} within {LOCK_TIMEOUT_S}s")
            time.sleep(LOCK_RETRY_INTERVAL_S)


def _release_lock(fd: int) -> None:
    """Release file lock and close fd."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _extract_file(file_path: Path, project_root: Path, config: dict) -> ExtractionResult:
    """Route a file to the appropriate extractor based on extension."""
    ext = file_path.suffix.lower()
    extractor_type = SOURCE_EXTENSIONS.get(ext)
    if not extractor_type:
        return ExtractionResult()

    if extractor_type == "code":
        from rtfm.extractors.code_extractor import extract
    elif extractor_type == "typescript":
        try:
            from rtfm.extractors.typescript_extractor import extract
        except ImportError:
            return ExtractionResult()
    elif extractor_type == "config":
        from rtfm.extractors.config_extractor import extract
    elif extractor_type == "doc":
        from rtfm.extractors.doc_extractor import extract
    else:
        return ExtractionResult()

    try:
        return extract(file_path, project_root, config)
    except Exception as e:
        logger.warning("Extraction failed for %s: %s", file_path, e)
        return ExtractionResult()


def _load_graph_json(json_path: Path) -> dict[str, Any]:
    """Load the graph JSON file."""
    with open(json_path) as f:
        return json.load(f)


def _save_graph_json(data: dict[str, Any], json_path: Path) -> None:
    """Atomically save graph JSON (write to temp, rename)."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(json_path.parent), suffix=".tmp", prefix="rtfm-graph-"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
        os.replace(tmp_path, str(json_path))
    except Exception:
        os.unlink(tmp_path)
        raise


def _save_pickle_atomic(graph: Any, pickle_path: Path) -> None:
    """Atomically save pickle (write to temp, rename)."""
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(pickle_path.parent), suffix=".tmp", prefix="rtfm-graph-"
    )
    try:
        with os.fdopen(tmp_fd, "wb") as f:
            pickle.dump(graph, f, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp_path, str(pickle_path))
    except Exception:
        os.unlink(tmp_path)
        raise


def _mark_semantic_dirty(state_dir: Path, source_files: list[str]) -> None:
    """Mark changed files as dirty in lance metadata for re-embedding."""
    try:
        import lancedb
    except ImportError:
        return

    lance_path = state_dir / "rtfm-lance"
    if not lance_path.exists():
        return

    try:
        db = lancedb.connect(str(lance_path))
        table_name = "rtfm_embeddings"
        if table_name not in db.table_names():
            return
        table = db.open_table(table_name)
        for sf in source_files:
            table.delete(f'source_file = "{sf}"')
        logger.info("Marked %d files as dirty in semantic index", len(source_files))
    except Exception as e:
        logger.debug("Semantic dirty-marking skipped: %s", e)


def update_graph(
    changed_files: list[Path],
    root: Path,
    state_dir: Path,
    config: dict | None = None,
) -> dict[str, int]:
    """Incrementally update the graph for changed files.

    Always re-enriches changed files + dependents to keep type-resolved
    edges consistent. A graph with stale edges is worse than a slightly
    slower update.

    Returns dict with keys: nodes_added, nodes_removed, edges_delta, enrich_edges.
    """
    config = config or {}
    json_path = state_dir / "rtfm-graph.json"
    pickle_path = state_dir / "rtfm-graph.pkl"
    lock_path = state_dir / ".rtfm.lock"

    if not json_path.exists():
        raise FileNotFoundError(
            f"Graph not found at {json_path}. Run 'rtfm build-all' first."
        )

    lock_fd = _acquire_lock(lock_path)
    try:
        data = _load_graph_json(json_path)
        nodes: list[dict[str, Any]] = data.get("nodes", [])
        edges: list[dict[str, Any]] = data.get("edges", [])

        # Relativize changed file paths for matching
        rel_paths: set[str] = set()
        for fp in changed_files:
            try:
                rel_paths.add(str(fp.resolve().relative_to(root.resolve())))
            except ValueError:
                rel_paths.add(str(fp))

        # Remove old nodes for changed files
        old_node_ids: set[str] = set()
        new_nodes: list[dict[str, Any]] = []
        for node in nodes:
            sf = node.get("source_file", "")
            if sf in rel_paths:
                old_node_ids.add(node["id"])
            else:
                new_nodes.append(node)

        nodes_removed = len(nodes) - len(new_nodes)

        # Remove edges referencing removed nodes
        old_edge_count = len(edges)
        new_edges: list[dict[str, Any]] = [
            e for e in edges
            if e["source"] not in old_node_ids and e["target"] not in old_node_ids
        ]

        # Re-extract changed files (skip deleted ones)
        extracted_nodes: list[NodeDict] = []
        extracted_edges: list[EdgeDict] = []
        for fp in changed_files:
            if not fp.exists():
                continue
            result = _extract_file(fp, root, config)
            extracted_nodes.extend(result.nodes)
            extracted_edges.extend(result.edges)

        # Relativize source_file in extracted nodes
        for node in extracted_nodes:
            try:
                node["source_file"] = str(
                    Path(node["source_file"]).resolve().relative_to(root.resolve())
                )
            except (ValueError, TypeError):
                pass

        # Merge new extractions
        nodes_added = len(extracted_nodes)
        edges_delta = len(extracted_edges) - (old_edge_count - len(new_edges))

        final_nodes = new_nodes + [dict(n) for n in extracted_nodes]
        final_edges = new_edges + [dict(e) for e in extracted_edges]

        # Rebuild graph and re-cluster
        from .graph_builder import build_graph, run_leiden, serialize

        graph = build_graph(final_nodes, final_edges)
        graph = run_leiden(graph)

        # Serialize back to JSON
        serialize(graph, str(json_path), project_root=str(root))

        # Save pickle
        _save_pickle_atomic(graph, pickle_path)

        # Re-enrich changed files + dependents (keeps type-resolved edges consistent)
        enrich_stats = {"edges_found": 0}
        existing_files = [fp for fp in changed_files if fp.exists()]
        if existing_files:
            try:
                from .jedi_enricher import enrich_incremental
                enrich_stats = enrich_incremental(
                    project_root=root,
                    graph_path=json_path,
                    changed_files=existing_files,
                    merge=True,
                    verbose=False,
                    config=config,
                )
            except (ImportError, RuntimeError) as e:
                logger.debug("Enrichment skipped: %s", e)

        # Mark semantic index dirty
        _mark_semantic_dirty(state_dir, list(rel_paths))

        return {
            "nodes_added": nodes_added,
            "nodes_removed": nodes_removed,
            "edges_delta": edges_delta,
            "enrich_edges": enrich_stats.get("edges_found", 0),
        }
    finally:
        _release_lock(lock_fd)
