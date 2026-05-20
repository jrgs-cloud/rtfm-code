"""Enricher orchestrator — picks the best available enricher.

Priority: Pyright (diagnostics) + Jedi (call resolution)
Both can run together: Pyright validates, Jedi resolves.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


def enrich_graph(
    project_root: Path,
    graph_json: Path,
    *,
    merge: bool = False,
    scope: str = "",
    verbose: bool = False,
    enricher: str = "auto",
    workers: int | None = None,
) -> dict:
    """Run the best available enricher(s).

    Args:
        enricher: "auto" (both if available), "pyright", "jedi", or "none"
        workers: Number of parallel workers for jedi enrichment (None = auto)

    Returns combined stats from all enrichers that ran.
    """
    results: dict[str, Any] = {"status": "complete", "enrichers_used": []}

    # Jedi: call resolution (type-resolved edges)
    if enricher in ("auto", "jedi"):
        jedi_stats = _run_jedi(project_root, graph_json, merge=merge, scope=scope, verbose=verbose, workers=workers)
        if jedi_stats.get("status") == "complete":
            results["enrichers_used"].append("jedi")
            results["jedi"] = jedi_stats

    # Pyright: diagnostic validation
    if enricher in ("auto", "pyright"):
        pyright_stats = _run_pyright(project_root, graph_json, merge=merge, scope=scope, verbose=verbose)
        if pyright_stats.get("status") == "complete":
            results["enrichers_used"].append("pyright")
            results["pyright"] = pyright_stats

    # TypeScript: type-resolved call resolution
    if enricher in ("auto", "typescript"):
        ts_stats = _run_typescript(project_root, graph_json, merge=merge, scope=scope, verbose=verbose)
        if ts_stats.get("status") == "complete":
            results["enrichers_used"].append("typescript")
            results["typescript"] = ts_stats

    if not results["enrichers_used"]:
        results["status"] = "skipped"
        results["reason"] = "No enricher available (install pyright or jedi)"

    return results


def _run_jedi(project_root: Path, graph_json: Path, *, workers: int | None = None, **kwargs) -> dict:
    """Attempt Jedi enrichment — uses parallel mode by default."""
    try:
        from rtfm.core.jedi_enricher import enrich_graph_parallel
        return enrich_graph_parallel(project_root, graph_json, workers=workers, **kwargs)
    except ImportError:
        return {"status": "skipped", "reason": "jedi not installed"}
    except Exception as e:
        print(f"[enricher] jedi failed: {e}", file=sys.stderr)
        return {"status": "error", "reason": str(e)}


def _run_pyright(project_root: Path, graph_json: Path, **kwargs) -> dict:
    """Attempt Pyright enrichment."""
    try:
        from rtfm.core.pyright_enricher import enrich_with_pyright
        return enrich_with_pyright(project_root, graph_json, **kwargs)
    except ImportError:
        return {"status": "skipped", "reason": "pyright_enricher not available"}
    except Exception as e:
        print(f"[enricher] pyright failed: {e}", file=sys.stderr)
        return {"status": "error", "reason": str(e)}


def _run_typescript(project_root: Path, graph_json: Path, **kwargs) -> dict:
    """Attempt TypeScript type resolution enrichment."""
    try:
        from rtfm.core.typescript_enricher import enrich_graph as ts_enrich
        return ts_enrich(project_root, graph_json, **kwargs)
    except ImportError:
        return {"status": "skipped", "reason": "typescript_enricher not available"}
    except Exception as e:
        print(f"[enricher] typescript failed: {e}", file=sys.stderr)
        return {"status": "error", "reason": str(e)}
