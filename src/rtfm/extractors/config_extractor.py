"""Extract configuration nodes from config files."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

from rtfm.core.types import (
    ET_CONFIGURES,
    NT_CONFIG,
    ExtractionResult,
    make_edge,
    make_node,
)

CONFIG_PATTERNS: dict[str, str] = {
    "pyproject.toml": "toml",
    "package.json": "json",
    "Cargo.toml": "toml",
    "go.mod": "gomod",
}


def extract(file_path: Path, project_root: Path, config: dict) -> ExtractionResult:
    """Extract configuration nodes from JSON, YAML, and TOML files.

    Produces ConfigNode with top-level keys, nested structure summary,
    and depends_env edges for environment variable references.

    Args:
        file_path: Absolute path to the config file.
        project_root: Project root for computing relative paths.
        config: Extractor config.

    Returns:
        ExtractionResult with ConfigNode and any environment dependency edges.
    """
    result = ExtractionResult()
    rel_path = str(file_path.relative_to(project_root))
    name = file_path.name

    patterns = config.get("config_patterns", CONFIG_PATTERNS)

    try:
        raw = file_path.read_bytes()
    except OSError:
        return result

    checksum = hashlib.sha256(raw).hexdigest()

    if name in patterns:
        fmt = patterns[name]
        if fmt == "json":
            _extract_json_config(raw, rel_path, checksum, result)
        elif fmt == "toml":
            _extract_toml_config(raw, rel_path, checksum, result)
        elif fmt == "gomod":
            _extract_gomod(raw, rel_path, checksum, result)
    elif file_path.suffix == ".json":
        _extract_json_config(raw, rel_path, checksum, result, expand_keys=False)
    elif file_path.suffix in (".yaml", ".yml"):
        _extract_yaml_config(raw, rel_path, checksum, result)
    elif file_path.suffix == ".toml":
        _extract_toml_config(raw, rel_path, checksum, result)
    else:
        return result

    return result


def _extract_json_config(raw: bytes, rel_path: str, checksum: str, result: ExtractionResult, expand_keys: bool = True) -> None:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return

    if not isinstance(data, dict):
        return

    file_node_id = f"config::{rel_path}"
    result.nodes.append(make_node(
        id=file_node_id,
        node_type=NT_CONFIG,
        source_file=rel_path,
        checksum=checksum,
        config_type="package_manifest" if expand_keys else "config_file",
        scope="project",
        key=Path(rel_path).name,
        value=data.get("name", rel_path),
        config_path=rel_path,
        affects=[],
    ))

    if not expand_keys:
        return

    for key, value in _flatten_dict(data):
        node_id = f"config::{rel_path}::{key}"
        result.nodes.append(make_node(
            id=node_id,
            node_type=NT_CONFIG,
            source_file=rel_path,
            checksum=checksum,
            config_type="setting",
            scope="project",
            key=key,
            value=str(value)[:200],
            config_path=rel_path,
            affects=[],
        ))
        result.edges.append(make_edge(
            source=node_id,
            target=file_node_id,
            edge_type=ET_CONFIGURES,
        ))


def _extract_toml_config(raw: bytes, rel_path: str, checksum: str, result: ExtractionResult) -> None:
    try:
        import tomllib
    except ModuleNotFoundError:
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ModuleNotFoundError:
            print(f"[config_extractor] skipping {rel_path}: no TOML parser available", file=sys.stderr)
            return

    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except Exception:
        return

    file_node_id = f"config::{rel_path}"
    result.nodes.append(make_node(
        id=file_node_id,
        node_type=NT_CONFIG,
        source_file=rel_path,
        checksum=checksum,
        config_type="package_manifest",
        scope="project",
        key=Path(rel_path).name,
        value=rel_path,
        config_path=rel_path,
        affects=[],
    ))

    for key, value in _flatten_dict(data):
        node_id = f"config::{rel_path}::{key}"
        result.nodes.append(make_node(
            id=node_id,
            node_type=NT_CONFIG,
            source_file=rel_path,
            checksum=checksum,
            config_type="setting",
            scope="project",
            key=key,
            value=str(value)[:200],
            config_path=rel_path,
            affects=[],
        ))
        result.edges.append(make_edge(
            source=node_id,
            target=file_node_id,
            edge_type=ET_CONFIGURES,
        ))


def _extract_gomod(raw: bytes, rel_path: str, checksum: str, result: ExtractionResult) -> None:
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return

    file_node_id = f"config::{rel_path}"
    result.nodes.append(make_node(
        id=file_node_id,
        node_type=NT_CONFIG,
        source_file=rel_path,
        checksum=checksum,
        config_type="package_manifest",
        scope="project",
        key="go.mod",
        value=rel_path,
        config_path=rel_path,
        affects=[],
    ))

    for m in re.finditer(r"^\s*(\S+)\s+v([\d.]+)", content, re.MULTILINE):
        dep_name = m.group(1)
        node_id = f"config::{rel_path}::require::{dep_name}"
        result.nodes.append(make_node(
            id=node_id,
            node_type=NT_CONFIG,
            source_file=rel_path,
            checksum=checksum,
            config_type="dependency",
            scope="project",
            key=dep_name,
            value=m.group(2),
            config_path=rel_path,
            affects=[],
        ))
        result.edges.append(make_edge(
            source=node_id,
            target=file_node_id,
            edge_type=ET_CONFIGURES,
        ))


def _extract_yaml_config(raw: bytes, rel_path: str, checksum: str, result: ExtractionResult) -> None:
    """Extract config nodes from YAML files."""
    try:
        import yaml
        data = yaml.safe_load(raw.decode("utf-8"))
    except Exception:
        result.nodes.append(make_node(
            id=f"config::{rel_path}",
            node_type=NT_CONFIG,
            source_file=rel_path,
            checksum=checksum,
        ))
        return

    if not isinstance(data, dict):
        result.nodes.append(make_node(
            id=f"config::{rel_path}",
            node_type=NT_CONFIG,
            source_file=rel_path,
            checksum=checksum,
        ))
        return

    file_node_id = f"config::{rel_path}"
    result.nodes.append(make_node(
        id=file_node_id,
        node_type=NT_CONFIG,
        source_file=rel_path,
        checksum=checksum,
        config_type="yaml",
        scope="project",
    ))

    for key, value in _flatten_dict(data, max_depth=2):
        if value is None or value == "":
            continue
        node_id = f"config::{rel_path}::{key}"
        result.nodes.append(make_node(
            id=node_id,
            node_type=NT_CONFIG,
            source_file=rel_path,
            checksum=checksum,
            config_type="setting",
            scope="project",
            key=key,
            value=str(value)[:200],
            config_path=rel_path,
            affects=[],
        ))
        result.edges.append(make_edge(
            source=node_id,
            target=file_node_id,
            edge_type=ET_CONFIGURES,
        ))


def _flatten_dict(
    d: dict,
    prefix: str = "",
    max_depth: int = 5,
    _depth: int = 0,
) -> list[tuple[str, object]]:
    items: list[tuple[str, object]] = []
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict) and _depth < max_depth:
            items.extend(_flatten_dict(v, key, max_depth, _depth + 1))
        else:
            items.append((key, v))
    return items
