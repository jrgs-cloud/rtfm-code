"""rtfm CLI — structural + semantic code intelligence."""

import json
import os
import pickle
import sys
from pathlib import Path

import click

from rtfm import __version__
from .init import init
from rtfm.core.scope import get_effective_scope

SKIP_DIRS = {"__pycache__", "node_modules", ".venv", "dist", ".git", ".hg", ".svn"}


@click.group()
@click.version_option(version=__version__, package_name="rtfm-code")
@click.option("--state-dir", default="status/", show_default=True, help="State directory for graph artifacts.")
@click.pass_context
def main(ctx: click.Context, state_dir: str) -> None:
    """Structural and semantic code intelligence for Claude projects."""
    ctx.ensure_object(dict)
    ctx.obj["state_dir"] = state_dir


main.add_command(init)


CONFIG_FILE = "rtfm.json"
DOT_CONFIG_FILE = ".rtfm.json"
DEFAULT_EXTRACTORS = ["code", "config", "doc"]


def _load_project_config(root: Path) -> dict:
    """Load rtfm.json or .rtfm.json from project root if either exists."""
    config_path = root / CONFIG_FILE
    if config_path.exists():
        try:
            return json.loads(config_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[rtfm] warning: could not parse {config_path}: {e}", file=sys.stderr)
            return {}
    dot_config_path = root / DOT_CONFIG_FILE
    if dot_config_path.exists():
        try:
            return json.loads(dot_config_path.read_text())
        except (json.JSONDecodeError, OSError) as e:
            print(f"[rtfm] warning: could not parse {dot_config_path}: {e}", file=sys.stderr)
            return {}
    return {}


def _discover_all_files(root: Path) -> list[Path]:
    """Recursively find all files, skipping hidden and noise directories."""
    files: list[Path] = []
    for item in sorted(root.iterdir()):
        if item.name.startswith(".") or item.name in SKIP_DIRS:
            continue
        if item.is_dir():
            files.extend(_discover_all_files(item))
        elif item.is_file():
            files.append(item)
    return files


def _save_pickle(graph, pickle_path: Path) -> None:
    pickle_path.parent.mkdir(parents=True, exist_ok=True)
    with open(pickle_path, "wb") as f:
        pickle.dump(graph, f, protocol=pickle.HIGHEST_PROTOCOL)


def _run_enrich(root: Path, graph_path: Path, verbose: bool = False, enricher: str = "auto", workers: int | None = None) -> dict:
    """Run enrichment pipeline (Pyright + Jedi). Returns stats or skip message."""
    try:
        from rtfm.core.enricher import enrich_graph
    except Exception:
        # Fallback to jedi-only if orchestrator not available
        try:
            from rtfm.core.jedi_enricher import enrich_graph_parallel
        except Exception:
            return {"status": "skipped", "reason": "no enricher available"}
        try:
            return enrich_graph_parallel(
                project_root=root,
                graph_path=graph_path,
                merge=True,
                verbose=verbose,
                workers=workers,
            )
        except RuntimeError as e:
            return {"status": "skipped", "reason": str(e)}
    try:
        return enrich_graph(
            project_root=root,
            graph_json=graph_path,
            merge=True,
            verbose=verbose,
            enricher=enricher,
            workers=workers,
        )
    except RuntimeError as e:
        return {"status": "skipped", "reason": str(e)}


@main.command("build-all")
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--workers", default=None, type=int, help="Parallel Jedi enrichment workers. Default: cpu_count.")
@click.option("--threads", default=None, type=int, help="ONNX threads for semantic embedding. Default: cpu_count.")
@click.option("--force", is_flag=True, help="Force full rebuild, ignore cache.")
@click.option("--no-heuristics", is_flag=True, help="Disable heuristic domain detection.")
@click.option("--no-enrich", is_flag=True, help="Skip enrichment (Jedi/Pyright) for faster builds.")
@click.pass_context
def build_all(ctx: click.Context, path: str, workers: int | None, threads: int | None, force: bool, no_heuristics: bool, no_enrich: bool) -> None:
    """Index a directory and build the full graph.

    Uses recursive domain discovery: walks the directory tree, classifies
    files using .rtfm.json domain rules + heuristic detection, then routes
    to the appropriate extractor per file.

    Nested .rtfm.json files override parent config at any depth.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from rtfm.core.discovery import discover
    from rtfm.core.graph_builder import (
        build_graph,
        inject_packages,
        merge_results,
        run_leiden,
        serialize,
    )
    from rtfm.core.manifest import check_freshness, compute_manifest, save_manifest
    from rtfm.extractors.domain_extractor import extract_domain_batch

    state_dir = Path(ctx.obj["state_dir"])
    root = Path(path).resolve()

    project_config = _load_project_config(root)
    active_extractors = project_config.get("extractors", DEFAULT_EXTRACTORS)

    # --- Phase 1: Recursive discovery ---
    discovery_result = discover(
        root=root,
        config=project_config,
        use_heuristics=not no_heuristics,
    )

    click.echo(
        f"[build-all] discovered {len(discovery_result.files)} files "
        f"across {len(discovery_result.domains_found)} domains"
        + (f" ({len(discovery_result.overrides_applied)} nested overrides)" if discovery_result.overrides_applied else ""),
        err=True,
    )

    # --- Phase 2: Route files to extractors ---
    extractor_fns = {}
    if "code" in active_extractors:
        from rtfm.extractors.code_extractor import extract as code_extract
        extractor_fns["code"] = code_extract
    if "typescript" in active_extractors:
        try:
            from rtfm.extractors.typescript_extractor import extract as ts_extract
            extractor_fns["typescript"] = ts_extract
        except ImportError:
            pass
    if "config" in active_extractors:
        from rtfm.extractors.config_extractor import extract as config_extract
        extractor_fns["config"] = config_extract
    if "doc" in active_extractors:
        from rtfm.extractors.doc_extractor import extract as doc_extract
        extractor_fns["doc"] = doc_extract

    # Group discovered files by extractor
    files_by_extractor = discovery_result.by_extractor()

    # Collect all source files for cache check
    all_source_files = [cf.path for cf in discovery_result.files]

    # Cache check — skip rebuild if nothing changed
    if not force:
        freshness, changed_files = check_freshness(state_dir, all_source_files)
        if freshness == "fresh":
            graph_path = state_dir / "rtfm-graph.json"
            if graph_path.exists():
                data = json.loads(graph_path.read_text())
                click.echo(json.dumps({
                    "status": "fresh",
                    "nodes": len(data.get("nodes", [])),
                    "edges": len(data.get("edges", [])),
                    "message": "Graph is up to date. Use --force to rebuild.",
                }))
                return

    # --- Phase 3: Parallel extraction ---
    extraction_config = {"allow_partial_parse": True}
    results = []
    file_count = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {}

        # Route standard extractor files (code, config, doc, typescript)
        for ext_name, extract_fn in extractor_fns.items():
            classified_files = files_by_extractor.get(ext_name, [])
            for cf in classified_files:
                future = pool.submit(extract_fn, cf.path, root, extraction_config)
                futures[future] = (ext_name, cf.path)
                file_count += 1

        for future in as_completed(futures):
            try:
                result = future.result()
                if result.nodes or result.edges:
                    results.append(result)
            except Exception as e:
                ext_name, f = futures[future]
                click.echo(f"[build-all] {ext_name} failed on {f}: {e}", err=True)

    # --- Phase 4: Domain extraction (non-code files with typed nodes) ---
    domain_files = files_by_extractor.get("domain", [])
    if domain_files:
        domain_result = extract_domain_batch(domain_files, root, config=project_config)
        if domain_result.nodes or domain_result.edges:
            results.append(domain_result)
        file_count += len(domain_files)

    if not results:
        click.echo(json.dumps({"error": "no_results", "message": "Extraction produced no nodes"}), err=True)
        sys.exit(1)

    # --- Phase 5: Graph assembly ---
    nodes, edges = merge_results(results)
    nodes, edges = inject_packages(nodes, edges, str(root))

    # --- Phase 5b: Post-merge smart edges (needs full node set) ---
    from rtfm.extractors.domain_extractor import apply_post_merge_edges
    post_edges = apply_post_merge_edges(nodes, edges, project_config)
    edges.extend(post_edges)

    graph = build_graph(nodes, edges)
    graph = run_leiden(graph)

    json_path = state_dir / "rtfm-graph.json"
    pickle_path = state_dir / "graph.pkl"

    serialize(graph, str(json_path), str(root))
    _save_pickle(graph, pickle_path)

    enrich_stats = _run_enrich(root, json_path, verbose=False, workers=workers) if not no_enrich else {"status": "skipped", "reason": "no_enrich flag"}

    semantic_stats: dict = {"status": "skipped", "reason": "semantic_unavailable"}
    try:
        from rtfm.core.vector_store import create_index, is_semantic_available
        if threads:
            os.environ["RTFM_EMBED_THREADS"] = str(threads)
        if is_semantic_available():
            from rtfm.core.chunker import chunk_nodes
            chunks = chunk_nodes(nodes, root)
            if chunks:
                index_result = create_index(chunks, state_dir / "lance")
                if isinstance(index_result, dict) and "error" in index_result:
                    semantic_stats = index_result
                else:
                    semantic_stats = {"status": "indexed", "chunks": index_result}
            else:
                semantic_stats = {"status": "skipped", "reason": "no_chunks_produced"}
    except Exception as e:
        semantic_stats = {"status": "skipped", "reason": str(e)}

    cluster_ids = {graph.vs[i]["cluster_id"] for i in range(graph.vcount())}

    # Save manifest for cache freshness checks — only source files, not build artifacts
    save_manifest(state_dir, compute_manifest(all_source_files))

    summary = {
        "nodes": graph.vcount(),
        "edges": graph.ecount(),
        "files": file_count,
        "extractors": list(extractor_fns.keys()),
        "clusters": len(cluster_ids),
        "enrich": enrich_stats,
        "semantic": semantic_stats,
    }
    click.echo(json.dumps(summary))


@main.command("enrich")
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--merge/--no-merge", default=False, help="Merge edges into the graph JSON in-place.")
@click.option("--dry-run", is_flag=True, help="Report stats without writing output.")
@click.option("--verbose", is_flag=True, help="Print per-file progress to stderr.")
@click.option("--scope", default=None, help="Limit to files under this prefix (e.g. 'src/').")
@click.option("--enricher", default="auto", type=click.Choice(["auto", "pyright", "jedi"]), help="Which enricher to use.")
@click.option("--incremental", "-i", multiple=True, type=click.Path(), help="Incremental mode: only enrich these files + dependents. Repeatable.")
@click.pass_context
def enrich(ctx: click.Context, path: str, merge: bool, dry_run: bool, verbose: bool, scope: str | None, enricher: str, incremental: tuple[str, ...]) -> None:
    """Run type-resolution enrichment (Pyright + Jedi) on an existing graph."""
    state_dir = Path(ctx.obj["state_dir"])
    root = Path(path).resolve()
    graph_path = state_dir / "rtfm-graph.json"

    if not graph_path.exists():
        click.echo(json.dumps({"error": "graph_not_found",
                               "message": f"No graph at {graph_path}. Run build-all first."}), err=True)
        sys.exit(1)

    if incremental:
        from rtfm.core.jedi_enricher import enrich_incremental
        changed = [Path(f).resolve() for f in incremental]
        stats = enrich_incremental(
            project_root=root,
            graph_path=graph_path,
            changed_files=changed,
            merge=merge,
            verbose=verbose,
        )
    else:
        stats = _run_enrich(root, graph_path, verbose=verbose, enricher=enricher)
    click.echo(json.dumps(stats))


@main.command("reindex")
@click.option("--force", is_flag=True, help="Rebuild even if index exists.")
@click.option("--verbose", is_flag=True)
@click.pass_context
def reindex(ctx: click.Context, force: bool, verbose: bool) -> None:
    """Rebuild pickle + vector index from existing rtfm-graph.json (no re-extraction)."""
    from rtfm.core.graph_builder import build_graph

    state_dir = Path(ctx.obj["state_dir"])
    graph_json = state_dir / "rtfm-graph.json"

    if not graph_json.exists():
        click.echo(json.dumps({"error": "graph_not_found", "message": "Run build-all first"}), err=True)
        sys.exit(1)

    data = json.loads(graph_json.read_text())
    nodes = data.get("nodes", [])
    edges = data.get("edges", [])

    # Rebuild graph + pickle
    graph = build_graph(nodes, edges)
    pickle_path = state_dir / "graph.pkl"
    _save_pickle(graph, pickle_path)
    if verbose:
        click.echo(f"Rebuilt pickle: {graph.vcount()} nodes, {graph.ecount()} edges", err=True)

    # Rebuild vector index
    semantic_stats = {"status": "skipped"}
    try:
        from rtfm.core.vector_store import create_index, is_semantic_available
        from rtfm.core.chunker import chunk_nodes

        if is_semantic_available():
            index_path = state_dir / "lance"
            if force or not index_path.exists():
                chunks = chunk_nodes(nodes, Path.cwd())
                result = create_index(chunks, index_path)
                if isinstance(result, int):
                    semantic_stats = {"status": "indexed", "chunks": result}
                else:
                    semantic_stats = result
            else:
                semantic_stats = {"status": "exists", "message": "Use --force to rebuild"}
        else:
            semantic_stats = {"status": "unavailable", "message": "Install fastembed + lancedb"}
    except ImportError:
        semantic_stats = {"status": "unavailable", "message": "Install fastembed + lancedb"}

    click.echo(json.dumps({
        "nodes": graph.vcount(),
        "edges": graph.ecount(),
        "pickle": str(pickle_path),
        "semantic": semantic_stats,
    }))


@main.command("gate")
@click.argument("files", nargs=-1, required=True, type=click.Path())
@click.option("--scope", default=None, help="Limit results to files under this path prefix.")
@click.pass_context
def gate(ctx: click.Context, files: tuple[str, ...], scope: str | None) -> None:
    """Run the governance gate check on target files."""
    from rtfm.core.graph_analysis import impact_analysis
    from rtfm.core.scope import detect_scope

    scope = get_effective_scope(scope, Path.cwd())
    state_dir = Path(ctx.obj["state_dir"])

    # Load config for locked files and thresholds
    locked_files: list[str] = []
    warning_threshold = 5
    block_on_locked_deps = True

    local_config = detect_scope(Path.cwd())
    if local_config:
        gate_config = local_config.get("gate", {})
        locked_files = gate_config.get("locked_files", [])
        warning_threshold = gate_config.get("warning_threshold", 5)
        block_on_locked_deps = gate_config.get("block_on_locked_deps", True)

    # Also check skill config (relative to CWD — CLI is always invoked from project root)
    skill_config_path = Path.cwd() / ".claude" / "skills" / "rtfm" / "config.json"
    if skill_config_path.exists() and not locked_files:
        try:
            skill_config = json.loads(skill_config_path.read_text())
            gate_config = skill_config.get("gate", {})
            locked_files = gate_config.get("locked_files", [])
            warning_threshold = gate_config.get("warning_threshold", 5)
            block_on_locked_deps = gate_config.get("block_on_locked_deps", True)
        except (json.JSONDecodeError, OSError):
            pass

    # Check direct locked file hits
    locked_hits: list[str] = []
    for file_path in files:
        for locked in locked_files:
            if file_path.startswith(locked) or file_path == locked:
                locked_hits.append(file_path)
                break

    if locked_hits:
        click.echo(json.dumps({
            "level": "block",
            "reason": "locked file",
            "locked_hits": locked_hits,
            "dependents_count": 0,
            "affected_files": [],
        }))
        return

    # Load graph for impact analysis
    try:
        from ._graph_loader import load_graph
        graph, node_index = load_graph(state_dir)
    except FileNotFoundError as e:
        click.echo(json.dumps({"error": "graph_not_found", "message": str(e)}), err=True)
        sys.exit(1)

    all_dependents: list[str] = []

    for file_path in files:
        result = impact_analysis(graph, node_index, file_path, depth=2)
        if not result.get("kb_miss"):
            for dep in result.get("primary_impact", []):
                sf = dep.get("source_file", "")
                if sf and sf not in all_dependents:
                    all_dependents.append(sf)

    if scope:
        all_dependents = [f for f in all_dependents if f.startswith(scope)]

    # Check if any dependent is a locked file
    locked_dep_hits: list[str] = []
    if block_on_locked_deps:
        for dep in all_dependents:
            for locked in locked_files:
                if dep.startswith(locked) or dep == locked:
                    locked_dep_hits.append(dep)
                    break

    if locked_dep_hits:
        click.echo(json.dumps({
            "level": "block",
            "reason": "change affects locked dependency",
            "locked_hits": locked_dep_hits,
            "dependents_count": len(all_dependents),
            "affected_files": all_dependents,
        }))
        return

    count = len(all_dependents)
    if count > warning_threshold:
        level = "warning"
    else:
        level = "info"

    output = {
        "level": level,
        "dependents_count": count,
        "affected_files": all_dependents,
    }
    click.echo(json.dumps(output))


@main.command("dark-spots")
@click.option("--scope", default=None, help="Limit to files under this path prefix.")
@click.option("--min-severity", default=1, type=int, help="Minimum signals to report (1-5).")
@click.option("--suggest", is_flag=True, help="Include actionable fix suggestions per signal.")
@click.pass_context
def dark_spots(ctx: click.Context, scope: str | None, min_severity: int, suggest: bool) -> None:
    """Surface files with structural quality concerns.

    Detects: no test coverage, undocumented functions, high fan-out,
    high coupling, and orphan modules — purely from graph topology.
    """
    from rtfm.core.graph_analysis import detect_dark_spots
    from rtfm.core.scope import get_effective_scope

    scope = get_effective_scope(scope, Path.cwd())
    state_dir = Path(ctx.obj["state_dir"])
    try:
        from ._graph_loader import load_graph
        graph, node_index = load_graph(state_dir)
    except FileNotFoundError as e:
        click.echo(json.dumps({"error": "graph_not_found", "message": str(e)}), err=True)
        sys.exit(1)

    spots = detect_dark_spots(graph, node_index, scope=scope, min_severity=min_severity)

    if suggest:
        from rtfm.core.suggestions import generate_suggestions
        generate_suggestions(spots, graph, node_index)

    output = {
        "dark_spots": spots,
        "total": len(spots),
        "scope": scope,
    }
    click.echo(json.dumps(output))


@main.command("index-semantic")
@click.option("--verbose", is_flag=True, help="Print progress to stderr.")
@click.option("--threads", default=None, type=int, help="ONNX threads for embedding. Default: cpu_count.")
@click.pass_context
def index_semantic(ctx: click.Context, verbose: bool, threads: int | None) -> None:
    """Build semantic vector index from the cached graph.

    Run after build-all to enable semantic search and hybrid queries.
    Requires: pip install rtfm[vector]
    """
    state_dir = Path(ctx.obj["state_dir"])
    if threads:
        os.environ["RTFM_EMBED_THREADS"] = str(threads)
    json_path = state_dir / "rtfm-graph.json"

    if not json_path.exists():
        click.echo(json.dumps({"error": "graph_not_found", "message": "Run build-all first"}), err=True)
        sys.exit(1)

    from rtfm.core.vector_store import create_index, is_semantic_available
    if not is_semantic_available():
        click.echo(json.dumps({"error": "semantic_unavailable", "message": "Install rtfm[vector]: pip install -e 'plugins/rtfm/[vector]'"}), err=True)
        sys.exit(1)

    import time
    start = time.time()
    data = json.loads(json_path.read_text())
    nodes = data.get("nodes", [])

    from rtfm.core.chunker import chunk_nodes
    root = Path(ctx.obj.get("root", "."))
    chunks = chunk_nodes(nodes, root)

    if not chunks:
        click.echo(json.dumps({"error": "no_chunks", "message": "No embeddable content found"}), err=True)
        sys.exit(1)

    if verbose:
        print(f"[index-semantic] embedding {len(chunks)} chunks...", file=sys.stderr)

    lance_path = state_dir / "lance"
    result = create_index(chunks, lance_path)

    elapsed = time.time() - start
    if isinstance(result, dict) and "error" in result:
        click.echo(json.dumps(result), err=True)
        sys.exit(1)

    output = {
        "status": "indexed",
        "chunks": result,
        "elapsed_seconds": round(elapsed, 1),
        "index_path": str(lance_path),
    }
    click.echo(json.dumps(output))


@main.command("query")
@click.argument("name")
@click.option("--scope", default=None, help="Limit results to files under this path prefix.")
@click.pass_context
def query(ctx: click.Context, name: str, scope: str | None) -> None:
    """Look up a node by name (exact or prefix match)."""
    from rtfm.core.graph_analysis import structural_query
    from rtfm.core.graph_store import search_nodes, vertex_to_dict

    scope = get_effective_scope(scope, Path.cwd())
    state_dir = Path(ctx.obj["state_dir"])
    try:
        from ._graph_loader import load_graph
        graph, node_index = load_graph(state_dir)
    except FileNotFoundError as e:
        click.echo(json.dumps({"error": "graph_not_found", "message": str(e)}), err=True)
        sys.exit(1)

    result = structural_query(graph, node_index, name)
    if result.get("kb_miss"):
        matches = search_nodes(graph, node_index, name, max_results=10)
        if matches:
            results = [vertex_to_dict(v) for v in matches]
            result = {"results": results, "result_count": len(results), "kb_miss": False}

    if scope and "results" in result:
        filtered = [r for r in result["results"] if r.get("source_file", "").startswith(scope)]
        result["results"] = filtered
        result["result_count"] = len(filtered)

    click.echo(json.dumps(result))


@main.command("impact")
@click.argument("files", nargs=-1, required=True, type=click.Path())
@click.option("--depth", default=2, show_default=True, help="BFS traversal depth (1-3).")
@click.option("--scope", default=None, help="Limit results to files under this path prefix.")
@click.pass_context
def impact(ctx: click.Context, files: tuple[str, ...], depth: int, scope: str | None) -> None:
    """Blast-radius analysis for changed files."""
    from rtfm.core.graph_analysis import impact_analysis

    scope = get_effective_scope(scope, Path.cwd())
    state_dir = Path(ctx.obj["state_dir"])
    try:
        from ._graph_loader import load_graph
        graph, node_index = load_graph(state_dir)
    except FileNotFoundError as e:
        click.echo(json.dumps({"error": "graph_not_found", "message": str(e)}), err=True)
        sys.exit(1)

    results = []
    for file_path in files:
        result = impact_analysis(graph, node_index, file_path, depth=depth)
        if scope:
            for key in ("primary_impact", "secondary_impact"):
                if key in result:
                    result[key] = [r for r in result[key] if r.get("source_file", "").startswith(scope)]
        results.append({"file": file_path, **result})

    if len(results) == 1:
        click.echo(json.dumps(results[0]))
    else:
        click.echo(json.dumps(results))


@main.command("neighbors")
@click.argument("name")
@click.option("--depth", default=1, show_default=True, help="Traversal depth.")
@click.option("--edge-types", default=None, help="Comma-separated edge type filter.")
@click.option("--direction", default="both", type=click.Choice(["in", "out", "both"]), help="Edge direction.")
@click.option("--scope", default=None, help="Limit results to files under this path prefix.")
@click.pass_context
def neighbors(ctx: click.Context, name: str, depth: int, edge_types: str | None, direction: str, scope: str | None) -> None:
    """List neighbours of a node up to the given depth."""
    from rtfm.core.graph_analysis import get_neighbors

    scope = get_effective_scope(scope, Path.cwd())
    state_dir = Path(ctx.obj["state_dir"])
    try:
        from ._graph_loader import load_graph
        graph, node_index = load_graph(state_dir)
    except FileNotFoundError as e:
        click.echo(json.dumps({"error": "graph_not_found", "message": str(e)}), err=True)
        sys.exit(1)

    types_list = edge_types.split(",") if edge_types else None
    result = get_neighbors(graph, node_index, name, edge_types=types_list, direction=direction, depth=depth)

    if scope and "neighbors" in result:
        result["neighbors"] = [n for n in result["neighbors"] if n.get("source_file", "").startswith(scope)]
        result["count"] = len(result["neighbors"])

    click.echo(json.dumps(result))


@main.command("node")
@click.argument("name")
@click.option("--scope", default=None, help="Limit results to files under this path prefix.")
@click.pass_context
def node(ctx: click.Context, name: str, scope: str | None) -> None:
    """Show full details for a named node."""
    from rtfm.core.graph_analysis import get_node_detail

    scope = get_effective_scope(scope, Path.cwd())
    state_dir = Path(ctx.obj["state_dir"])
    try:
        from ._graph_loader import load_graph
        graph, node_index = load_graph(state_dir)
    except FileNotFoundError as e:
        click.echo(json.dumps({"error": "graph_not_found", "message": str(e)}), err=True)
        sys.exit(1)

    result = get_node_detail(graph, node_index, name)

    if scope and "node" in result and result["node"]:
        sf = result["node"].get("source_file", "")
        if sf and not sf.startswith(scope):
            result = {"node": None, "kb_miss": True}
        else:
            for key in ("edges_in", "edges_out"):
                if key in result["node"]:
                    result["node"][key] = [e for e in result["node"][key] if e.get("source_file", "").startswith(scope)]

    click.echo(json.dumps(result))


@main.command("cluster")
@click.argument("name")
@click.option("--scope", default=None, help="Limit results to files under this path prefix.")
@click.pass_context
def cluster(ctx: click.Context, name: str, scope: str | None) -> None:
    """Show the cluster a node belongs to."""
    from rtfm.core.graph_analysis import get_cluster

    scope = get_effective_scope(scope, Path.cwd())
    state_dir = Path(ctx.obj["state_dir"])
    try:
        from ._graph_loader import load_graph
        graph, node_index = load_graph(state_dir)
    except FileNotFoundError as e:
        click.echo(json.dumps({"error": "graph_not_found", "message": str(e)}), err=True)
        sys.exit(1)

    try:
        cluster_id = int(name)
        result = get_cluster(graph, node_index, cluster_id=cluster_id)
    except ValueError:
        result = get_cluster(graph, node_index, node_id=name)

    if scope and "nodes" in result:
        result["nodes"] = [n for n in result["nodes"] if n.get("source_file", "").startswith(scope)]
        result["size"] = len(result["nodes"])

    click.echo(json.dumps(result))


@main.command("search")
@click.argument("text")
@click.option("--top-k", default=10, show_default=True, help="Maximum results.")
@click.option("--threshold", default=None, type=float, help="Minimum similarity score (0-1).")
@click.option("--scope", default=None, help="Limit results to files under this path prefix.")
@click.pass_context
def search(ctx: click.Context, text: str, top_k: int, threshold: float | None, scope: str | None) -> None:
    """Semantic search across the graph."""
    from rtfm.core.vector_store import index_exists, is_semantic_available, search as vector_search

    if not is_semantic_available():
        click.echo(json.dumps({"error": "semantic_unavailable", "message": "Install fastembed and lancedb"}))
        sys.exit(1)

    state_dir = Path(ctx.obj["state_dir"])
    index_path = state_dir / "lance"

    # --- Two-phase tiered search ---
    #
    # Phase 1 (pre-warm): Search the full pre-built "chunks" table.
    # Backward-compatible path: build-all and index-semantic both write all
    # nodes into a single "chunks" table. If it exists, use it directly.
    #
    # Phase 2 (on-demand, future): If only a "modules" table exists, search
    # module-level nodes first, then expand to child functions via graph edges
    # on cache miss. Child embeddings are written to a per-module sub-table
    # keyed by the module's checksum. Invalidation: checksum change → sub-table
    # dropped and re-embedded on next hit.
    # TODO: implement tier-2 on-demand expansion here once module-level indexing
    # is wired into build-all (plug in after the index_exists("chunks") check).

    if index_exists(index_path, "chunks"):
        # Phase 1: full pre-built index available — use it directly.
        results = vector_search(text, index_path, top_k=top_k, threshold=threshold, table_name="chunks")
    else:
        # No pre-built index found. Phase 2 (on-demand module expansion) is not
        # yet implemented — return empty results so callers can fall back gracefully.
        results = []

    if isinstance(results, dict) and "error" in results:
        click.echo(json.dumps(results))
        sys.exit(1)

    output_results = []
    for r in results:
        entry = {
            "node_id": r.node_id,
            "source_file": r.source_file,
            "node_type": r.node_type,
            "score": round(r.score, 4),
            "chunk_preview": r.chunk_preview,
        }
        if scope and not entry["source_file"].startswith(scope):
            continue
        output_results.append(entry)

    click.echo(json.dumps({"results": output_results, "result_count": len(output_results), "query": text, "mode": "semantic"}))


@main.command("hybrid")
@click.argument("text")
@click.option("--top-k", default=10, show_default=True, help="Maximum results.")
@click.option("--structural-weight", default=1.0, show_default=True, help="Weight for structural results in RRF.")
@click.option("--semantic-weight", default=1.0, show_default=True, help="Weight for semantic results in RRF.")
@click.option("--scope", default=None, help="Limit results to files under this path prefix.")
@click.pass_context
def hybrid(ctx: click.Context, text: str, top_k: int, structural_weight: float, semantic_weight: float, scope: str | None) -> None:
    """Hybrid structural + semantic search."""
    from rtfm.core.fusion import rrf_merge
    from rtfm.core.graph_store import search_nodes, vertex_to_dict
    from rtfm.core.vector_store import is_semantic_available, search as vector_search

    if not is_semantic_available():
        click.echo(json.dumps({"error": "semantic_unavailable", "message": "Install fastembed and lancedb"}))
        sys.exit(1)

    scope = get_effective_scope(scope, Path.cwd())
    state_dir = Path(ctx.obj["state_dir"])

    # Structural results
    try:
        from ._graph_loader import load_graph
        graph, node_index = load_graph(state_dir)
    except FileNotFoundError as e:
        click.echo(json.dumps({"error": "graph_not_found", "message": str(e)}), err=True)
        sys.exit(1)

    structural_matches = search_nodes(graph, node_index, text, max_results=top_k)
    structural_formatted = [vertex_to_dict(v) for v in structural_matches]

    # Semantic results
    index_path = state_dir / "lance"
    semantic_raw = vector_search(text, index_path, top_k=top_k)

    if isinstance(semantic_raw, dict) and "error" in semantic_raw:
        # Semantic unavailable at search time — fall back to structural only
        semantic_formatted: list[dict] = []
    else:
        semantic_formatted = [
            {
                "node_id": r.node_id,
                "source_file": r.source_file,
                "node_type": r.node_type,
                "score": r.score,
                "chunk_preview": r.chunk_preview,
            }
            for r in semantic_raw
        ]

    merged = rrf_merge(
        structural_formatted,
        semantic_formatted,
        structural_weight=structural_weight,
        semantic_weight=semantic_weight,
        top_k=top_k,
    )

    if scope:
        merged = [r for r in merged if r.get("source_file", "").startswith(scope)]

    click.echo(json.dumps({"results": merged, "result_count": len(merged), "query": text, "mode": "hybrid"}))


@main.command("export-vault")
@click.option("--output", "-o", default="vault", help="Output directory for Obsidian vault.")
@click.option("--include-semantic", is_flag=True, help="Include semantic similarity links.")
@click.option("--max-nodes", default=500, help="Maximum nodes to export.")
@click.option("--scope", default=None, help="Limit to files under this path prefix.")
@click.pass_context
def export_vault(ctx: click.Context, output: str, include_semantic: bool, max_nodes: int, scope: str | None) -> None:
    """Export graph as an Obsidian markdown vault with wikilinks."""
    from rtfm.export.obsidian import export_vault as do_export

    state_dir = Path(ctx.obj["state_dir"])
    graph_json = state_dir / "rtfm-graph.json"
    index_path = state_dir / "lance" if include_semantic else None

    if not graph_json.exists():
        click.echo(json.dumps({"error": "graph_not_found", "message": "Run build-all first"}), err=True)
        sys.exit(1)

    output_dir = Path(output)
    try:
        stats = do_export(
            graph_json=graph_json,
            output_dir=output_dir,
            index_path=index_path,
            include_semantic=include_semantic,
            max_nodes=max_nodes,
        )
    except Exception as e:
        click.echo(json.dumps({"error": "export_failed", "message": str(e)}), err=True)
        sys.exit(1)

    if scope:
        # Post-export filtering: remove vault files whose source_file is outside scope
        removed = 0
        for md_file in output_dir.rglob("*.md"):
            try:
                text = md_file.read_text(encoding="utf-8")
                # Check frontmatter for source_file
                if text.startswith("---"):
                    parts = text.split("---", 2)
                    if len(parts) >= 3:
                        for line in parts[1].splitlines():
                            if line.startswith("source_file:"):
                                sf = line.split(":", 1)[1].strip().strip('"')
                                if sf and not sf.startswith(scope):
                                    md_file.unlink()
                                    removed += 1
                                break
            except (OSError, UnicodeDecodeError):
                continue
        if removed:
            stats["scope_filtered"] = removed

    click.echo(json.dumps(stats))


@main.command("watch")
@click.argument("path", default=".", type=click.Path(exists=True))
@click.option("--enrich", is_flag=True, help="Run enrichers on changed files after graph update.")
@click.pass_context
def watch(ctx: click.Context, path: str, enrich: bool) -> None:
    """Watch for file changes and incrementally update the graph."""
    import asyncio

    state_dir = Path(ctx.obj["state_dir"])
    root = Path(path).resolve()
    graph_path = state_dir / "rtfm-graph.json"

    if not graph_path.exists():
        click.echo(
            f"[watch] No graph found at {graph_path}. Run 'rtfm build-all' first.",
            err=True,
        )
        sys.exit(1)

    project_config = _load_project_config(root)

    from rtfm.core.watcher import watch_loop

    asyncio.run(watch_loop(
        path=root,
        state_dir=state_dir,
        enrich=enrich,
        config=project_config,
    ))


@main.command("validate")
@click.option("--coverage-file", required=True, type=click.Path(exists=True), help="Path to coverage data (.coverage or coverage-final.json).")
@click.option("--project-root", default=".", type=click.Path(exists=True), help="Project root for path relativization.")
@click.pass_context
def validate(ctx: click.Context, coverage_file: str, project_root: str) -> None:
    """Compare graph edges against test coverage data.

    Validates which edges in the graph are exercised by tests,
    identifies phantom edges (never executed), and finds blind spots.
    """
    from rtfm.core.validator import validate as run_validate

    state_dir = Path(ctx.obj["state_dir"])
    graph_path = state_dir / "rtfm-graph.json"

    if not graph_path.exists():
        click.echo(
            json.dumps({"error": "graph_not_found", "message": f"No graph at {graph_path}. Run 'rtfm build-all' first."}),
            err=True,
        )
        sys.exit(1)

    report = run_validate(
        graph_path=graph_path,
        coverage_path=Path(coverage_file),
        project_root=Path(project_root).resolve(),
    )
    click.echo(json.dumps(report, indent=2))
