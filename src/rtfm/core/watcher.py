"""File watcher for incremental graph updates."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "dist", ".git", ".hg", ".svn"}
SKIP_EXTENSIONS = {".pyc", ".pyo", ".so", ".o", ".a", ".class", ".jar"}
SOURCE_EXTENSIONS = {".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".yaml", ".yml", ".toml", ".md"}

DEBOUNCE_MS = 500


def _should_skip(path: Path) -> bool:
    """Check if a path should be skipped."""
    parts = path.parts
    for part in parts:
        if part in SKIP_DIRS or part.startswith("."):
            return True
    if path.suffix in SKIP_EXTENSIONS:
        return True
    if path.suffix not in SOURCE_EXTENSIONS:
        return True
    return False


def _emit_event(event: dict) -> None:
    """Emit a JSON event to stdout (one per line)."""
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _warm_semantic_model() -> bool:
    """Warm the fastembed model singleton. Returns True if model is ready."""
    try:
        from .vector_store import is_semantic_available, _get_model_manager

        if not is_semantic_available():
            return False

        # Force model load by calling _get_model_manager (creates singleton)
        mgr = _get_model_manager()
        if mgr.available:
            # Trigger actual model warmup with a dummy embed
            mgr.embed(["warmup"])
            return True
    except Exception as e:
        logger.debug("Semantic model warm failed: %s", e)
    return False


def _index_changed_files(
    changed_files: list[Path],
    root: Path,
    state_dir: Path,
    graph_path: Path,
) -> dict | None:
    """Incrementally update the semantic index for changed files.

    1. Load graph to get nodes for changed files
    2. Chunk those nodes
    3. Delete old rows from LanceDB where source_file in changed
    4. Insert new chunks

    Returns stats dict or None if semantic is unavailable.
    """
    try:
        from .vector_store import is_semantic_available, update_index, index_exists
        from .chunker import chunk_nodes
    except ImportError:
        return None

    if not is_semantic_available():
        return None

    lance_path = state_dir / "lance"

    # Only proceed if an index already exists (don't build from scratch here)
    if not index_exists(lance_path):
        return None

    if not graph_path.exists():
        return None

    # Load graph JSON to get nodes for changed files
    try:
        with open(graph_path) as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.debug("Failed to load graph for incremental indexing: %s", e)
        return None

    # Relativize changed file paths
    rel_paths: set[str] = set()
    for fp in changed_files:
        try:
            rel_paths.add(str(fp.resolve().relative_to(root.resolve())))
        except ValueError:
            rel_paths.add(str(fp))

    # Filter graph nodes to only those belonging to changed files
    nodes_for_changed = [
        node for node in data.get("nodes", [])
        if node.get("source_file", "") in rel_paths
    ]

    # Chunk the filtered nodes
    chunks = chunk_nodes(nodes_for_changed, root)

    # Update the index (delete old rows for these files, insert new)
    result = update_index(
        chunks=chunks,
        index_path=lance_path,
        source_files=list(rel_paths),
    )

    if isinstance(result, dict) and "error" in result:
        logger.debug("Incremental index update failed: %s", result)
        return None

    return {
        "files_indexed": len(rel_paths),
        "chunks_inserted": result if isinstance(result, int) else 0,
    }


async def _run_index_background(
    changed_files: list[Path],
    root: Path,
    state_dir: Path,
) -> None:
    """Run incremental semantic indexing in the background after structural update.

    Emits an 'indexed' event on completion or 'index_error' on failure.
    """
    graph_path = state_dir / "rtfm-graph.json"
    if not graph_path.exists():
        return

    try:
        loop = asyncio.get_running_loop()
        stats = await loop.run_in_executor(
            None, _index_changed_files, changed_files, root, state_dir, graph_path
        )
        if stats:
            _emit_event({
                "event": "indexed",
                "files": [str(f) for f in changed_files],
                "chunks_inserted": stats.get("chunks_inserted", 0),
            })
    except Exception as e:
        logger.debug("Background indexing failed: %s", e)
        _emit_event({
            "event": "index_error",
            "files": [str(f) for f in changed_files],
            "error": str(e),
        })


async def _run_enrich_background(
    changed_files: list[Path],
    root: Path,
    state_dir: Path,
    jedi_project: "Any | None" = None,
) -> None:
    """Run enrichment in the background after structural update.

    Enriches only the changed files' source paths. Emits an 'enriched' event
    on completion or an 'enrich_error' event on failure.
    """
    graph_path = state_dir / "rtfm-graph.json"
    if not graph_path.exists():
        return

    # Only enrich Python files (Jedi) — TS enrichment requires Node subprocess
    py_files = [f for f in changed_files if f.suffix == ".py"]
    if not py_files:
        return

    try:
        # Run in executor to avoid blocking the event loop
        loop = asyncio.get_running_loop()
        stats = await loop.run_in_executor(None, _enrich_sync, root, graph_path, py_files, jedi_project)
        if stats and stats.get("status") == "complete":
            _emit_event({
                "event": "enriched",
                "files": [str(f.relative_to(root)) for f in py_files],
                "edges_found": stats.get("edges_found", 0),
            })
    except Exception as e:
        logger.debug("Background enrichment failed: %s", e)
        _emit_event({
            "event": "enrich_error",
            "files": [str(f.relative_to(root)) for f in py_files],
            "error": str(e),
        })


def _enrich_sync(root: Path, graph_path: Path, py_files: list[Path], jedi_project: "Any | None" = None) -> dict | None:
    """Synchronous enrichment wrapper — runs incremental Jedi on changed files + dependents."""
    try:
        from .jedi_enricher import enrich_incremental
    except ImportError:
        return None

    return enrich_incremental(
        project_root=root,
        graph_path=graph_path,
        changed_files=py_files,
        merge=True,
        jedi_project=jedi_project,
    )


async def watch_loop(
    path: Path,
    state_dir: Path,
    enrich: bool = False,
    config: dict | None = None,
) -> None:
    """Main watch loop — monitors path for changes, updates graph incrementally.

    When enrich=True, enrichment runs asynchronously in the background after
    each structural update completes. The structural update emits immediately;
    enrichment emits a separate 'enriched' event when done.

    Semantic model is warmed at startup if available, and incremental indexing
    runs after each structural update.
    """
    try:
        from watchfiles import awatch, Change
    except ImportError:
        print(
            "watchfiles not installed. Install with: pip install 'rtfm[watch]'",
            file=sys.stderr,
        )
        sys.exit(0)

    from .incremental import update_graph

    config = config or {}
    root = path.resolve()
    enrich_tasks: set[asyncio.Task] = set()
    index_tasks: set[asyncio.Task] = set()

    # Warm Jedi Project cache: persistent instance reuses inference cache across cycles
    _jedi_project = None
    _enrich_cycle_count = 0
    _INVALIDATION_INTERVAL = 50  # Recreate project every N cycles
    if enrich:
        try:
            import jedi
            _jedi_project = jedi.Project(path=root)
        except ImportError:
            pass

    # Warm the semantic embedding model at startup (0.84s one-time cost)
    semantic_ready = _warm_semantic_model()

    _emit_event({
        "event": "started",
        "path": str(root),
        "state_dir": str(state_dir),
        "auto_enrich": enrich,
        "semantic_ready": semantic_ready,
    })

    try:
        async for changes in awatch(root, stop_event=asyncio.Event()):
            # Debounce: collect changes for DEBOUNCE_MS
            await asyncio.sleep(DEBOUNCE_MS / 1000.0)

            # Filter to source files, skip noise
            changed_files: list[Path] = []
            for change_type, change_path in changes:
                p = Path(change_path)
                if _should_skip(p.relative_to(root)):
                    continue
                changed_files.append(p)

            if not changed_files:
                continue

            try:
                stats = update_graph(
                    changed_files=changed_files,
                    root=root,
                    state_dir=state_dir,
                    config=config,
                    enrich=False,  # Never block on enrich — it runs in background
                )
                _emit_event({
                    "event": "updated",
                    "files": [str(f.relative_to(root)) for f in changed_files],
                    "nodes_added": stats["nodes_added"],
                    "nodes_removed": stats["nodes_removed"],
                    "edges_delta": stats["edges_delta"],
                })

                # Queue background enrichment if enabled
                if enrich:
                    # Invalidate jedi.Project if __init__.py changed or interval reached
                    _enrich_cycle_count += 1
                    if _jedi_project is not None:
                        init_changed = any(
                            f.name == "__init__.py" for f in changed_files
                        )
                        if init_changed or _enrich_cycle_count >= _INVALIDATION_INTERVAL:
                            try:
                                import jedi
                                _jedi_project = jedi.Project(path=root)
                                _enrich_cycle_count = 0
                            except Exception:
                                pass

                    task = asyncio.create_task(
                        _run_enrich_background(changed_files, root, state_dir, _jedi_project)
                    )
                    enrich_tasks.add(task)
                    task.add_done_callback(enrich_tasks.discard)

                # Queue background incremental indexing if semantic is ready
                if semantic_ready:
                    idx_task = asyncio.create_task(
                        _run_index_background(changed_files, root, state_dir)
                    )
                    index_tasks.add(idx_task)
                    idx_task.add_done_callback(index_tasks.discard)

            except Exception as e:
                logger.error("Incremental update failed: %s", e)
                _emit_event({
                    "event": "error",
                    "files": [str(f.relative_to(root)) for f in changed_files],
                    "error": str(e),
                })
    except KeyboardInterrupt:
        pass
    finally:
        # Cancel pending enrichment and indexing tasks on shutdown
        all_tasks = enrich_tasks | index_tasks
        for task in all_tasks:
            task.cancel()
        if all_tasks:
            await asyncio.gather(*all_tasks, return_exceptions=True)
        _emit_event({"event": "stopped"})
