"""Dark-spots suggestions — generate actionable fix suggestions per signal type."""

from __future__ import annotations

from typing import Any

from ._backend import GraphProtocol


def generate_suggestions(
    spots: list[dict[str, Any]],
    graph: GraphProtocol,
    node_index: dict[str, int],
) -> list[dict[str, Any]]:
    """Augment dark-spot results with actionable suggestions.

    Mutates each spot dict in-place, adding a 'suggestions' list to each signal.
    Returns the same list for convenience.
    """
    for spot in spots:
        sf = spot["file"]
        for signal in spot["signals"]:
            signal["suggestion"] = _suggest_for_signal(signal, sf, graph, node_index)
    return spots


def _suggest_for_signal(
    signal: dict[str, str],
    source_file: str,
    graph: GraphProtocol,
    node_index: dict[str, int],
) -> dict[str, Any]:
    """Generate a suggestion for a single signal."""
    sig_type = signal["type"]
    if sig_type == "no_test_coverage":
        return _suggest_test(source_file, graph, node_index)
    elif sig_type == "undocumented":
        return _suggest_docstrings(source_file, graph, node_index)
    elif sig_type == "orphan":
        return _suggest_orphan(source_file, graph, node_index)
    elif sig_type == "high_fan_out":
        return _suggest_fan_out(source_file, graph)
    elif sig_type == "high_coupling":
        return _suggest_coupling(source_file, graph)
    return {"action": "investigate", "detail": "No automated suggestion available."}


def _suggest_test(
    source_file: str,
    graph: GraphProtocol,
    node_index: dict[str, int],
) -> dict[str, Any]:
    """Generate a test skeleton for an untested module."""
    # Collect public functions in this module
    funcs = [
        v for v in graph.vs
        if v["node_type"] == "FunctionNode"
        and (v["source_file"] or "") == source_file
        and not v["node_id"].rsplit("::", 1)[-1].startswith("_")
    ]

    module_name = source_file.replace("/", ".").removesuffix(".py")
    # Derive import path: strip leading src/ or similar
    import_path = module_name
    for prefix in ("src.", "lib."):
        if import_path.startswith(prefix):
            import_path = import_path[len(prefix):]
            break

    func_names = [v["node_id"].rsplit("::", 1)[-1] for v in funcs]

    # Build test file path
    parts = source_file.rsplit("/", 1)
    if len(parts) == 2:
        test_path = f"tests/test_{parts[1]}"
    else:
        test_path = f"tests/test_{source_file}"

    # Generate skeleton
    imports = ", ".join(func_names[:10]) if func_names else module_name.rsplit(".", 1)[-1]
    lines = [f"import pytest", f"from {import_path} import {imports}", "", ""]

    for fname in func_names[:10]:
        lines.append(f"class Test{_to_class_name(fname)}:")
        lines.append(f"    def test_{fname}_basic(self):")
        lines.append(f"        ...")
        lines.append("")
        lines.append(f"    def test_{fname}_edge_case(self):")
        lines.append(f"        ...")
        lines.append("")

    return {
        "action": "create_test",
        "target_path": test_path,
        "functions": func_names[:10],
        "skeleton": "\n".join(lines),
    }


def _suggest_docstrings(
    source_file: str,
    graph: GraphProtocol,
    node_index: dict[str, int],
) -> dict[str, Any]:
    """Generate docstring templates for undocumented functions."""
    # Find documented functions
    documented: set[str] = set()
    for v in graph.vs:
        if v["node_type"] == "DocNode":
            for eid in graph.incident(v.index, mode="out"):
                edge = graph.es[eid]
                if edge["edge_type"] == "documents":
                    documented.add(graph.vs[edge.target]["node_id"])

    # Find undocumented public functions in this module
    undoc_funcs = [
        v for v in graph.vs
        if v["node_type"] == "FunctionNode"
        and (v["source_file"] or "") == source_file
        and not v["node_id"].rsplit("::", 1)[-1].startswith("_")
        and v["node_id"] not in documented
    ]

    templates = []
    for func in undoc_funcs[:10]:
        fname = func["node_id"].rsplit("::", 1)[-1]
        # Check what it calls (for Raises section)
        raises = []
        for eid in graph.incident(func.index, mode="out"):
            edge = graph.es[eid]
            if edge["edge_type"] in ("calls", "type_resolved_call"):
                target_name = graph.vs[edge.target]["node_id"].rsplit("::", 1)[-1]
                if "Error" in target_name or "Exception" in target_name:
                    raises.append(target_name)

        # Get params from attrs if available
        attrs = func["attrs"] if isinstance(func["attrs"], dict) else {}
        params = attrs.get("params", [])

        docstring = _build_docstring(fname, params, raises)
        templates.append({"name": fname, "template": docstring})

    return {
        "action": "add_docstrings",
        "functions": templates,
    }


def _suggest_orphan(
    source_file: str,
    graph: GraphProtocol,
    node_index: dict[str, int],
) -> dict[str, Any]:
    """Generate investigation options for orphan modules."""
    # Find the module's cluster to suggest nearest neighbors
    mod_idx = node_index.get(source_file)
    cluster_id = None
    if mod_idx is not None:
        cluster_id = graph.vs[mod_idx]["cluster_id"]

    # Find other modules in same cluster
    neighbors = []
    if cluster_id is not None:
        for v in graph.vs:
            if (v["node_type"] == "ModuleNode"
                    and v["cluster_id"] == cluster_id
                    and v["source_file"] != source_file
                    and not v["source_file"].startswith("test")):
                neighbors.append(v["source_file"])
                if len(neighbors) >= 3:
                    break

    options = []
    if neighbors:
        options.append(f"Import from {neighbors[0]} (nearest by cluster)")
    options.append("Register as entry point in pyproject.toml")
    options.append("Add to __init__.py exports")
    options.append("Delete if unused")

    return {
        "action": "investigate",
        "detail": "0 inbound edges — possible dead code.",
        "options": options,
        "nearest_cluster_members": neighbors,
    }


def _suggest_fan_out(source_file: str, graph: GraphProtocol) -> dict[str, Any]:
    """Suggest refactoring for high fan-out."""
    # Find the function with highest fan-out
    max_func = None
    max_calls = 0
    for v in graph.vs:
        if v["node_type"] == "FunctionNode" and (v["source_file"] or "") == source_file:
            out_calls = sum(
                1 for eid in graph.incident(v.index, mode="out")
                if graph.es[eid]["edge_type"] in ("calls", "type_resolved_call")
            )
            if out_calls > max_calls:
                max_calls = out_calls
                max_func = v["node_id"].rsplit("::", 1)[-1]

    return {
        "action": "refactor",
        "detail": f"Function '{max_func}' has {max_calls} outbound calls. Consider extracting sub-functions.",
        "function": max_func,
        "call_count": max_calls,
    }


def _suggest_coupling(source_file: str, graph: GraphProtocol) -> dict[str, Any]:
    """Suggest interface extraction for high coupling."""
    # Count unique importers
    importers: list[str] = []
    for v in graph.vs:
        if v["node_type"] == "ModuleNode" and v["source_file"] == source_file:
            for eid in graph.incident(v.index, mode="in"):
                edge = graph.es[eid]
                src_v = graph.vs[edge.source]
                if src_v["node_type"] == "ModuleNode" and src_v["source_file"] != source_file:
                    importers.append(src_v["source_file"])
            break

    return {
        "action": "extract_interface",
        "detail": f"{len(importers)} modules depend on this. Consider splitting into a public API and internal implementation.",
        "importers": importers[:5],
    }


def _to_class_name(func_name: str) -> str:
    """Convert function_name to ClassName."""
    return "".join(word.capitalize() for word in func_name.split("_"))


def _build_docstring(fname: str, params: list, raises: list[str]) -> str:
    """Build a docstring template from function metadata."""
    lines = [f'"""TODO: describe {fname}.']
    if params:
        lines.append("")
        lines.append("    Args:")
        for p in params[:8]:
            if isinstance(p, str):
                lines.append(f"        {p}: TODO")
            elif isinstance(p, dict):
                lines.append(f"        {p.get('name', '?')}: TODO")
    if raises:
        lines.append("")
        lines.append("    Raises:")
        for r in raises[:4]:
            lines.append(f"        {r}: TODO")
    lines.append('    """')
    return "\n".join(lines)
