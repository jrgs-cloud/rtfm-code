"""Core type definitions for rtfm."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypedDict


# ---------------------------------------------------------------------------
# Edge type constants (used by core modules)
# ---------------------------------------------------------------------------

ET_CALLS = "calls"
ET_CONFIGURES = "configures"
ET_CONSUMES_API = "consumes_api"
ET_CONTAINS = "contains"
ET_CROSS_REFERENCES = "cross_references"
ET_DECORATES = "decorates"
ET_DEPENDS_ENV = "depends_env"
ET_DOCUMENTS = "documents"
ET_GENERATED_BY = "generated_by"
ET_IMPORTS = "imports"
ET_INHERITS = "inherits"
ET_PROVIDES_API = "provides_api"
ET_READS = "reads"
ET_SHARED_CONFIG = "shared_config"
ET_WRITES = "writes"

# ---------------------------------------------------------------------------
# Node type constants (used by core modules)
# ---------------------------------------------------------------------------

NT_CLASS = "ClassNode"
NT_CONFIG = "ConfigNode"
NT_DOC = "DocNode"
NT_FUNCTION = "FunctionNode"
NT_MODULE = "ModuleNode"
NT_PACKAGE = "PackageNode"
NT_TYPE = "TypeNode"
NT_VARIABLE = "VariableNode"

# ---------------------------------------------------------------------------
# Typed dicts
# ---------------------------------------------------------------------------


class NodeDict(TypedDict):
    id: str
    node_type: str
    cluster_id: int
    source_file: str
    last_updated: str
    checksum: str
    attrs: dict[str, Any]


class EdgeDict(TypedDict):
    source: str
    target: str
    edge_type: str
    metadata: dict[str, Any]


@dataclass
class ExtractionResult:
    nodes: list[NodeDict] = field(default_factory=list)
    edges: list[EdgeDict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Checksum cache (module-level, single-run lifecycle)
# ---------------------------------------------------------------------------

_checksum_cache: dict[str, str] = {}


def _file_checksum(path: str) -> str:
    if path in _checksum_cache:
        return _checksum_cache[path]
    p = Path(path)
    if not p.is_file():
        return "synthetic"
    digest = hashlib.sha256(p.read_bytes()).hexdigest()
    _checksum_cache[path] = digest
    return digest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_node(
    id: str,
    node_type: str,
    source_file: str,
    **attrs: Any,
) -> NodeDict:
    """Create a node dict with universal metadata auto-filled.

    ``last_updated`` is set to the current UTC time in ISO-8601.
    ``checksum`` is computed as SHA-256 of *source_file* unless already
    present in *attrs*.
    """
    checksum = attrs.pop("checksum", None) or _file_checksum(source_file)
    return NodeDict(
        id=id,
        node_type=node_type,
        cluster_id=attrs.pop("cluster_id", 0),
        source_file=source_file,
        last_updated=datetime.now(timezone.utc).isoformat(),
        checksum=checksum,
        attrs=attrs,
    )


def make_edge(
    source: str,
    target: str,
    edge_type: str,
    **metadata: Any,
) -> EdgeDict:
    return EdgeDict(
        source=source,
        target=target,
        edge_type=edge_type,
        metadata=metadata,
    )
