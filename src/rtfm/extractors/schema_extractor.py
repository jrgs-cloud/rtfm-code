"""Schema extractor — detects cross-language edges from API schemas and codegen markers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from rtfm.core.types import (
    ET_CONSUMES_API,
    ET_DEPENDS_ENV,
    ET_GENERATED_BY,
    ET_PROVIDES_API,
    ET_SHARED_CONFIG,
    NT_CONFIG,
    ExtractionResult,
    make_edge,
    make_node,
)

OPENAPI_MARKERS = ("openapi", "swagger")
GENERATED_MARKERS = ("@generated", "DO NOT EDIT", "auto-generated", "THIS FILE IS GENERATED")
TS_ENV_PATTERNS = [
    re.compile(r"process\.env\.(\w+)"),
    re.compile(r"import\.meta\.env\.(\w+)"),
]

# Route decorator patterns (Python frameworks)
ROUTE_PATTERNS = [
    # FastAPI / Starlette: @app.get("/path"), @router.post("/path")
    re.compile(r"@\w+\.(get|post|put|delete|patch|head|options)\(\s*[\"']([^\"']+)[\"']"),
    # Flask: @app.route("/path", methods=["GET"])
    re.compile(r"@\w+\.route\(\s*[\"']([^\"']+)[\"'](?:.*?methods\s*=\s*\[([^\]]+)\])?"),
    # Django: path("route/", view)
    re.compile(r"path\(\s*[\"']([^\"']+)[\"']"),
]

# HTTP client patterns (consumers)
HTTP_CLIENT_PATTERNS = [
    # Python requests/httpx: requests.get("/path"), httpx.post("http://...")
    re.compile(r"(?:requests|httpx|aiohttp|urllib)\.(get|post|put|delete|patch)\(\s*[\"']([^\"']+)[\"']"),
    # Python requests/httpx with f-string: requests.get(f"/users/{id}")
    re.compile(r"(?:requests|httpx|aiohttp)\.(get|post|put|delete|patch)\(\s*f[\"']([^\"']+)[\"']"),
    # TypeScript fetch: fetch("/path"), fetch(`/path`)
    re.compile(r"fetch\(\s*[\"'`]([^\"'`]+)[\"'`]"),
    # Axios: axios.get("/path"), axios.post("/path")
    re.compile(r"axios\.(get|post|put|delete|patch)\(\s*[\"'`]([^\"'`]+)[\"'`]"),
]


def extract(file_path: Path, project_root: Path, config: dict) -> ExtractionResult:
    """Extract cross-language edges from schema files.

    Handles:
    - OpenAPI/Swagger YAML/JSON → endpoint nodes
    - Python/TS source files → provides_api (route decorators) and consumes_api (HTTP clients)
    - Generated file markers → generated_by edges
    - TypeScript process.env → depends_env edges (extends Python detection)
    """
    result = ExtractionResult()
    rel_path = str(file_path.relative_to(project_root))
    suffix = file_path.suffix.lower()

    if suffix in (".yaml", ".yml", ".json"):
        _try_openapi(file_path, rel_path, config, result)
    elif suffix == ".py":
        _extract_providers(file_path, rel_path, result)
        _extract_consumers(file_path, rel_path, result)
    elif suffix in (".ts", ".tsx", ".js", ".jsx"):
        _extract_ts_env(file_path, rel_path, result)
        _extract_consumers(file_path, rel_path, result)
        _check_generated_marker(file_path, rel_path, config, result)
    elif suffix == ".proto":
        _extract_proto(file_path, rel_path, config, result)
    elif suffix == ".graphql":
        _extract_graphql(file_path, rel_path, config, result)

    return result


def _try_openapi(file_path: Path, rel_path: str, config: dict, result: ExtractionResult) -> None:
    """Parse OpenAPI/Swagger schema and emit endpoint nodes."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    data = None
    if file_path.suffix.lower() == ".json":
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            return
    else:
        try:
            import yaml
            data = yaml.safe_load(content)
        except ImportError:
            return
        except Exception:
            return

    if not isinstance(data, dict):
        return

    # Check if this is an OpenAPI/Swagger file
    if not any(marker in data for marker in OPENAPI_MARKERS):
        return

    paths = data.get("paths", {})
    if not isinstance(paths, dict):
        return

    # Create a schema node for the file
    schema_id = f"schema::{rel_path}"
    result.nodes.append(make_node(
        id=schema_id,
        node_type=NT_CONFIG,
        source_file=rel_path,
        name=file_path.stem,
        schema_type="openapi",
    ))

    # Create endpoint nodes and edges
    for path_str, methods in paths.items():
        if not isinstance(methods, dict):
            continue
        for method, details in methods.items():
            if method.startswith("x-") or method == "parameters":
                continue
            endpoint_id = f"schema::{method.upper()} {path_str}"
            result.nodes.append(make_node(
                id=endpoint_id,
                node_type=NT_CONFIG,
                source_file=rel_path,
                name=f"{method.upper()} {path_str}",
                schema_type="endpoint",
                operation_id=details.get("operationId", "") if isinstance(details, dict) else "",
            ))

            # Link schema → endpoint
            result.edges.append(make_edge(
                source=schema_id,
                target=endpoint_id,
                edge_type="contains",
            ))


def _extract_ts_env(file_path: Path, rel_path: str, result: ExtractionResult) -> None:
    """Extract process.env and import.meta.env references from TypeScript files."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    seen: set[str] = set()
    for pattern in TS_ENV_PATTERNS:
        for match in pattern.finditer(content):
            key = match.group(1)
            if key in seen:
                continue
            seen.add(key)
            env_node_id = f"config::env_var::{key}"
            result.edges.append(make_edge(
                source=rel_path,
                target=env_node_id,
                edge_type=ET_DEPENDS_ENV,
                language="typescript",
            ))


def _check_generated_marker(
    file_path: Path, rel_path: str, config: dict, result: ExtractionResult
) -> None:
    """Check if a file has codegen markers and emit generated_by edge."""
    try:
        # Only read first 2KB for marker detection
        with open(file_path, "r", encoding="utf-8") as f:
            header = f.read(2048)
    except (OSError, UnicodeDecodeError):
        return

    is_generated = any(marker in header for marker in GENERATED_MARKERS)

    # Also check configured generated paths
    cross_lang = config.get("cross_language", {})
    generated_paths = cross_lang.get("generated_paths", [])
    if not is_generated:
        is_generated = any(rel_path.startswith(gp) for gp in generated_paths)

    if not is_generated:
        return

    # Try to find the generator from the header comment
    generator = _find_generator_in_header(header)
    if generator:
        result.edges.append(make_edge(
            source=rel_path,
            target=generator,
            edge_type=ET_GENERATED_BY,
        ))
    else:
        # Emit edge to a synthetic "codegen" node
        result.edges.append(make_edge(
            source=rel_path,
            target="codegen::unknown",
            edge_type=ET_GENERATED_BY,
        ))


def _find_generator_in_header(header: str) -> str | None:
    """Try to extract the generator script path from file header comments."""
    # Common patterns: "Generated by scripts/codegen.py", "Source: tools/generate.ts"
    patterns = [
        re.compile(r"[Gg]enerated (?:by|from|using)\s+['\"]?([^\s'\"]+\.\w+)"),
        re.compile(r"[Ss]ource:\s*['\"]?([^\s'\"]+\.\w+)"),
    ]
    for pattern in patterns:
        match = pattern.search(header)
        if match:
            path = match.group(1)
            # Sanity check: looks like a file path
            if "/" in path or path.endswith((".py", ".ts", ".js", ".sh")):
                return path
    return None


def _extract_proto(file_path: Path, rel_path: str, config: dict, result: ExtractionResult) -> None:
    """Extract service/rpc definitions from protobuf files."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    schema_id = f"schema::{rel_path}"
    result.nodes.append(make_node(
        id=schema_id,
        node_type=NT_CONFIG,
        source_file=rel_path,
        name=file_path.stem,
        schema_type="protobuf",
    ))

    # Extract service and rpc definitions
    service_pattern = re.compile(r"service\s+(\w+)\s*\{")
    rpc_pattern = re.compile(r"rpc\s+(\w+)\s*\(")

    current_service = None
    for line in content.splitlines():
        service_match = service_pattern.search(line)
        if service_match:
            current_service = service_match.group(1)
            continue

        rpc_match = rpc_pattern.search(line)
        if rpc_match and current_service:
            rpc_name = rpc_match.group(1)
            endpoint_id = f"schema::{current_service}/{rpc_name}"
            result.nodes.append(make_node(
                id=endpoint_id,
                node_type=NT_CONFIG,
                source_file=rel_path,
                name=f"{current_service}/{rpc_name}",
                schema_type="rpc",
            ))
            result.edges.append(make_edge(
                source=schema_id,
                target=endpoint_id,
                edge_type="contains",
            ))


def _extract_graphql(file_path: Path, rel_path: str, config: dict, result: ExtractionResult) -> None:
    """Extract type and query definitions from GraphQL schema files."""
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    schema_id = f"schema::{rel_path}"
    result.nodes.append(make_node(
        id=schema_id,
        node_type=NT_CONFIG,
        source_file=rel_path,
        name=file_path.stem,
        schema_type="graphql",
    ))

    # Extract Query/Mutation fields as endpoints
    type_pattern = re.compile(r"type\s+(Query|Mutation)\s*\{([^}]+)\}", re.MULTILINE | re.DOTALL)
    field_pattern = re.compile(r"(\w+)\s*(?:\([^)]*\))?\s*:")

    for type_match in type_pattern.finditer(content):
        type_name = type_match.group(1)
        body = type_match.group(2)
        for field_match in field_pattern.finditer(body):
            field_name = field_match.group(1)
            endpoint_id = f"schema::{type_name}.{field_name}"
            result.nodes.append(make_node(
                id=endpoint_id,
                node_type=NT_CONFIG,
                source_file=rel_path,
                name=f"{type_name}.{field_name}",
                schema_type="graphql_field",
            ))
            result.edges.append(make_edge(
                source=schema_id,
                target=endpoint_id,
                edge_type="contains",
            ))


def _normalize_path(path: str) -> str:
    """Normalize an API path for matching.

    Converts path params to a canonical form:
    /users/{id} → /users/{*}
    /users/:id → /users/{*}
    /users/${userId} → /users/{*}
    """
    # OpenAPI style: {param}
    normalized = re.sub(r"\{[^}]+\}", "{*}", path)
    # Express style: :param
    normalized = re.sub(r":(\w+)", "{*}", normalized)
    # Template literal: ${param}
    normalized = re.sub(r"\$\{[^}]+\}", "{*}", normalized)
    # Strip trailing slash
    return normalized.rstrip("/") or "/"


def _extract_providers(file_path: Path, rel_path: str, result: ExtractionResult) -> None:
    """Scan Python source for route decorators and emit provides_api edges.

    Detects FastAPI, Flask, Django, and Starlette route patterns.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    for pattern in ROUTE_PATTERNS:
        for match in pattern.finditer(content):
            groups = match.groups()
            if len(groups) == 2 and groups[0] in ("get", "post", "put", "delete", "patch", "head", "options"):
                # FastAPI/Starlette style: method, path
                method = groups[0].upper()
                path = groups[1]
            elif len(groups) == 2 and groups[1]:
                # Flask style: path, methods string
                path = groups[0]
                methods_str = groups[1]
                # Parse methods like '"GET", "POST"'
                method = re.findall(r'["\'](\w+)["\']', methods_str)
                method = method[0].upper() if method else "GET"
            elif len(groups) >= 1:
                # Django path() or Flask route without methods
                path = groups[0]
                method = "GET"
            else:
                continue

            if not path.startswith("/"):
                path = "/" + path

            endpoint_id = f"schema::{method} {_normalize_path(path)}"
            result.edges.append(make_edge(
                source=rel_path,
                target=endpoint_id,
                edge_type=ET_PROVIDES_API,
                method=method,
                path=path,
            ))


def _extract_consumers(file_path: Path, rel_path: str, result: ExtractionResult) -> None:
    """Scan source files for HTTP client calls and emit consumes_api edges.

    Detects requests, httpx, aiohttp, fetch, and axios patterns.
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    seen: set[tuple[str, str]] = set()

    for pattern in HTTP_CLIENT_PATTERNS:
        for match in pattern.finditer(content):
            groups = match.groups()
            if len(groups) == 2:
                # method, url pattern (requests.get("/path"))
                method = groups[0].upper()
                url = groups[1]
            elif len(groups) == 1:
                # url only (fetch("/path"))
                method = "GET"
                url = groups[0]
            else:
                continue

            # Extract path from URL (strip protocol/host if present)
            path = _extract_path_from_url(url)
            if not path:
                continue

            key = (method, _normalize_path(path))
            if key in seen:
                continue
            seen.add(key)

            endpoint_id = f"schema::{method} {_normalize_path(path)}"
            result.edges.append(make_edge(
                source=rel_path,
                target=endpoint_id,
                edge_type=ET_CONSUMES_API,
                method=method,
                path=path,
            ))


def _extract_path_from_url(url: str) -> str | None:
    """Extract the path component from a URL string.

    Handles:
    - Full URLs: https://api.example.com/users → /users
    - Relative paths: /users/123 → /users/123
    - Template strings: /users/${id} → /users/${id}
    """
    # Skip non-path strings (config vars, empty)
    if not url or url.startswith("$") or url.startswith("{"):
        return None

    # Strip protocol + host
    if "://" in url:
        after_proto = url.split("://", 1)[1]
        slash_idx = after_proto.find("/")
        if slash_idx == -1:
            return "/"
        path = after_proto[slash_idx:]
    elif url.startswith("/"):
        path = url
    else:
        path = "/" + url

    # Strip query string and fragment
    path = path.split("?")[0].split("#")[0]

    # Must look like an API path
    if not path or path == "/":
        return None

    return path
