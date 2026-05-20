"""Extract documentation nodes from markdown files."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from rtfm.core.types import (
    ET_CROSS_REFERENCES,
    NT_DOC,
    ExtractionResult,
    make_edge,
    make_node,
)


def extract(file_path: Path, project_root: Path, config: dict) -> ExtractionResult:
    """Extract documentation nodes from markdown/RST files.

    Produces DocNode with title, sections, and heading structure.
    Respects excluded_filenames in config to skip specific files.

    Args:
        file_path: Absolute path to the documentation file.
        project_root: Project root for computing relative paths.
        config: Extractor config (supports excluded_filenames list).

    Returns:
        ExtractionResult with DocNode and any section sub-nodes.
    """
    result = ExtractionResult()
    excluded = config.get("excluded_filenames", [])
    if file_path.name in excluded:
        return result

    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result

    rel_path = str(file_path.relative_to(project_root))
    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
    node_id = f"doc::{rel_path}"
    title = _extract_title(content, file_path)
    content_type = _classify(file_path)
    sections = _extract_sections(content)

    result.nodes.append(make_node(
        id=node_id,
        node_type=NT_DOC,
        source_file=rel_path,
        checksum=checksum,
        path=rel_path,
        title=title,
        content_type=content_type,
        sections=sections,
    ))

    for ref_path in _extract_references(content, rel_path):
        result.edges.append(make_edge(
            source=node_id,
            target=_ref_to_node_id(ref_path, project_root),
            edge_type=ET_CROSS_REFERENCES,
        ))

    return result


def _extract_title(content: str, file_path: Path) -> str:
    m = re.match(r"^#\s+(.+)", content)
    if m:
        return m.group(1).strip()
    return file_path.stem


def _classify(file_path: Path) -> str:
    lower = file_path.name.lower()
    if "readme" in lower:
        return "readme"
    if "changelog" in lower or "changes" in lower:
        return "changelog"
    if "spec" in lower or "design" in lower or "architecture" in lower:
        return "design_doc"
    return "other"


def _extract_sections(content: str) -> list[str]:
    sections: list[str] = []
    for m in re.finditer(r"^#+\s+(.+)", content, re.MULTILINE):
        sections.append(m.group(1).strip())
    return sections


def _extract_references(content: str, rel_source: str) -> list[str]:
    """Extract relative file paths from markdown links in *content*.

    Returns resolved relative paths (relative to project root) suitable for
    node ID construction.  External URLs, anchors, and mailto links are
    skipped.
    """
    refs: list[str] = []
    source_dir = str(Path(rel_source).parent)

    for m in re.finditer(r"\[.*?\]\(([^)]+)\)", content):
        link = m.group(1)
        if link.startswith(("http://", "https://", "#", "mailto:")):
            continue
        link = link.split("#")[0]
        if not link:
            continue
        resolved = str(Path(source_dir) / link)
        refs.append(resolved)

    return refs


_CONFIG_SUFFIXES = {".json", ".toml", ".yaml", ".yml"}


def _ref_to_node_id(rel_path: str, project_root: Path) -> str:
    """Return the most appropriate node ID for a referenced path.

    Priority:
    1. ``doc::{rel_path}``   — markdown / text docs
    2. ``config::{rel_path}`` — recognised config file extensions
    3. bare ``{rel_path}``   — Python source files

    If the resolved file does not exist the ``doc::`` prefix is used as a
    forward-reference (graph_builder drops orphan edges gracefully).
    """
    p = Path(rel_path)
    suffix = p.suffix.lower()

    if suffix == ".py":
        return rel_path
    if suffix in _CONFIG_SUFFIXES:
        return f"config::{rel_path}"
    return f"doc::{rel_path}"
