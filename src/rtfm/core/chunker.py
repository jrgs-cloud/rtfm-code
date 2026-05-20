"""Convert graph nodes into embeddable text chunks for semantic search."""

from __future__ import annotations

from pathlib import Path

from .types import NT_CLASS, NT_CONFIG, NT_DOC, NT_FUNCTION, NT_MODULE, NodeDict

MAX_CHUNK_SIZE = 2000


def _get_attrs(node: NodeDict) -> dict:
    """Get attrs dict, handling both extraction format and serialized JSON."""
    return node.get("attrs", node)


def _get_id(node: NodeDict) -> str:
    """Get node ID, handling both 'id' and 'node_id' keys."""
    return node.get("id", node.get("node_id", ""))


def chunk_nodes(nodes: list[NodeDict], project_root: Path) -> list[dict]:
    """Convert graph nodes into text chunks suitable for embedding.

    Each chunk has: node_id, source_file, node_type, content, start_line, end_line
    """
    chunks: list[dict] = []
    for node in nodes:
        chunk = _node_to_chunk(node, project_root)
        if chunk:
            chunks.append(chunk)
    return chunks


def _read_lines(file_path: Path, start: int, end: int) -> str | None:
    try:
        text = file_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    # start/end are 1-based inclusive
    selected = lines[start - 1:end]
    return "\n".join(selected)


def _format_function(node: NodeDict, source: str | None) -> str:
    attrs = _get_attrs(node)
    name = _get_id(node).rsplit("::", 1)[-1]
    params = attrs.get("params", [])
    param_str = ", ".join(p.get("name", "?") for p in params) if params else ""
    return_type = attrs.get("return_type", "")
    sig = f"function {name}({param_str})"
    if return_type:
        sig += f" -> {return_type}"
    if source:
        return f"{sig}\n{source}"
    return sig


def _format_class(node: NodeDict, source: str | None) -> str:
    attrs = _get_attrs(node)
    name = _get_id(node).rsplit("::", 1)[-1]
    bases = attrs.get("bases", [])
    methods = attrs.get("methods", [])
    bases_str = ", ".join(bases) if bases else ""
    header = f"class {name}({bases_str})" if bases_str else f"class {name}"
    parts = [header]
    if methods:
        parts.append(f"  methods: {', '.join(methods)}")
    docstring = attrs.get("docstring", "")
    if docstring:
        parts.append(f"  {docstring}")
    if source:
        parts.append(source)
    return "\n".join(parts)


def _format_module(node: NodeDict) -> str:
    attrs = _get_attrs(node)
    path = node.get("source_file", "")
    imports = attrs.get("imports", [])
    exports = attrs.get("exports", [])
    parts = [f"module {path}"]
    if imports:
        parts.append(f"  imports: {', '.join(imports[:30])}")
    if exports:
        parts.append(f"  defines: {', '.join(exports[:30])}")
    return "\n".join(parts)


def _format_doc(node: NodeDict) -> str:
    attrs = node.get("attrs", node)
    title = attrs.get("title", "")
    content_type = attrs.get("content_type", "other")
    sections = attrs.get("sections", [])
    parts = [f"doc: {title} ({content_type})"]
    parts.append(f"  file: {node['source_file']}")
    if sections:
        parts.append(f"  sections: {', '.join(sections[:20])}")
    return "\n".join(parts)


def _format_config(node: NodeDict) -> str:
    attrs = node.get("attrs", node)
    config_type = attrs.get("config_type", "")
    key = attrs.get("key", "")
    value = attrs.get("value", "")
    path = node["source_file"]
    if key:
        content = f"config: {path}::{key}"
        if value:
            content += f" = {str(value)[:200]}"
    else:
        content = f"config: {path} ({config_type})"
    return content


def _node_to_chunk(node: NodeDict, project_root: Path) -> dict | None:
    node_type = node.get("node_type", "")
    attrs = _get_attrs(node)
    line_range = attrs.get("line_range")
    start_line = line_range[0] if line_range else 0
    end_line = line_range[1] if line_range else 0

    source_file = node.get("source_file", "")
    source: str | None = None
    if line_range and source_file:
        source = _read_lines(project_root / source_file, start_line, end_line)

    if node_type == NT_FUNCTION:
        content = _format_function(node, source)
    elif node_type == NT_CLASS:
        content = _format_class(node, source)
    elif node_type == NT_MODULE:
        content = _format_module(node)
    elif node_type == NT_DOC:
        content = _format_doc(node)
    elif node_type == NT_CONFIG:
        content = _format_config(node)
    else:
        return None

    if not content.strip():
        return None

    content = content[:MAX_CHUNK_SIZE]

    return {
        "node_id": _get_id(node),
        "source_file": source_file,
        "node_type": node_type,
        "content": content,
        "start_line": start_line,
        "end_line": end_line,
    }
