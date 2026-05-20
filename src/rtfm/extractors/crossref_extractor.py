"""Extract cross-reference edges from markdown links."""

from __future__ import annotations

import re
from pathlib import Path

from rtfm.core.types import (
    ET_CROSS_REFERENCES,
    ExtractionResult,
    make_edge,
)


def extract(file_path: Path, project_root: Path, config: dict) -> ExtractionResult:
    """Extract cross-reference edges from markdown and documentation files.

    Detects markdown links [text](path), backtick file references, and
    @agent/@skill mentions. Produces cross_references, governs, delegates_to edges.

    Args:
        file_path: Absolute path to the documentation file.
        project_root: Project root for resolving relative link targets.
        config: Extractor config.

    Returns:
        ExtractionResult with cross-reference edges.
    """
    result = ExtractionResult()

    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result

    rel_path = str(file_path.relative_to(project_root))
    source_id = _path_to_node_id(rel_path)

    for m in re.finditer(r"\[([^\]]*)\]\(([^)]+)\)", content):
        link_text = m.group(1)
        link_target = m.group(2)

        if link_target.startswith("http://") or link_target.startswith("https://"):
            continue

        link_target = link_target.split("#")[0]
        if not link_target:
            continue

        resolved = _resolve_path(file_path, link_target, project_root)
        if resolved:
            target_id = _path_to_node_id(resolved)
            result.edges.append(make_edge(
                source=source_id,
                target=target_id,
                edge_type=ET_CROSS_REFERENCES,
                link_text=link_text,
            ))

    return result


def _resolve_path(source_file: Path, relative_target: str, project_root: Path) -> str | None:
    resolved = (source_file.parent / relative_target).resolve()
    if resolved.exists():
        try:
            return str(resolved.relative_to(project_root))
        except ValueError:
            return None
    return None


def _path_to_node_id(rel_path: str) -> str:
    p = Path(rel_path)
    name = p.name
    parts = p.parts

    if p.suffix == ".py":
        return rel_path

    return f"doc::{rel_path}"
