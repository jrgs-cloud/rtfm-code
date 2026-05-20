"""Extract code structure nodes and edges from Python source files using tree-sitter."""

from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path
from typing import Any

import tree_sitter_python as tsp
from tree_sitter import Language, Node, Parser

from rtfm.core.types import (
    ET_CALLS,
    ET_DEPENDS_ENV,
    ET_DOCUMENTS,
    ET_IMPORTS,
    ET_INHERITS,
    ET_READS,
    ET_WRITES,
    NT_CLASS,
    NT_DOC,
    NT_FUNCTION,
    NT_MODULE,
    NT_VARIABLE,
    ExtractionResult,
    make_edge,
    make_node,
)

_LANGUAGE = Language(tsp.language())
_thread_local = threading.local()

def _get_parser() -> Parser:
    if not hasattr(_thread_local, "parser"):
        _thread_local.parser = Parser(_LANGUAGE)
    return _thread_local.parser


def extract(file_path: Path, project_root: Path, config: dict) -> ExtractionResult:
    """Extract Python AST nodes and edges from a source file using tree-sitter.

    Produces ModuleNode, FunctionNode, ClassNode with import/call/inherits edges.
    Handles syntax errors gracefully — returns partial results unless allow_partial_parse is False.

    Args:
        file_path: Absolute path to the Python source file.
        project_root: Project root for computing relative paths.
        config: Extractor config (supports allow_partial_parse, skip_private).

    Returns:
        ExtractionResult with nodes and edges for the file.
    """
    result = ExtractionResult()

    try:
        source = file_path.read_bytes()
    except OSError:
        return result

    tree = _get_parser().parse(source)
    if tree.root_node.has_error and not config.get("allow_partial_parse", False):
        return result

    rel_path = str(file_path.relative_to(project_root))
    text = source.decode("utf-8", errors="replace")
    checksum = hashlib.sha256(source).hexdigest()

    _extract_module(rel_path, tree.root_node, text, checksum, result)
    _extract_top_level(rel_path, tree.root_node, checksum, result)
    import_map = _extract_import_edges(rel_path, tree.root_node, result)
    _extract_call_edges(rel_path, tree.root_node, result, import_map)
    _extract_env_var_deps(rel_path, tree.root_node, result)
    _extract_docstring_edges(rel_path, tree.root_node, checksum, result)
    _extract_var_reads_writes(rel_path, source, result)

    return result


# ---------------------------------------------------------------------------
# Module-level extraction
# ---------------------------------------------------------------------------


def _extract_module(
    rel_path: str,
    root: Node,
    text: str,
    checksum: str,
    result: ExtractionResult,
) -> None:
    imports = _collect_imports(root)
    exports = _collect_exports(root)
    env_deps = _collect_env_var_names(root)
    line_count = text.count("\n") + 1

    result.nodes.append(make_node(
        id=rel_path,
        node_type=NT_MODULE,
        source_file=rel_path,
        checksum=checksum,
        imports=imports,
        exports=exports,
        env_var_deps=env_deps,
        line_count=line_count,
    ))


def _extract_top_level(
    rel_path: str,
    root: Node,
    checksum: str,
    result: ExtractionResult,
) -> None:
    for child in root.children:
        if child.type == "function_definition":
            _extract_function(rel_path, child, checksum, result)
        elif child.type == "class_definition":
            _extract_class(rel_path, child, checksum, result)
        elif child.type == "decorated_definition":
            inner = _get_decorated_inner(child)
            if inner and inner.type == "function_definition":
                _extract_function(rel_path, inner, checksum, result, decorators=_get_decorators(child))
            elif inner and inner.type == "class_definition":
                _extract_class(rel_path, inner, checksum, result, decorators=_get_decorators(child))
        elif child.type == "expression_statement":
            assign = child.children[0] if child.children else None
            if assign and assign.type == "assignment":
                _extract_variable(rel_path, assign, "module", checksum, result)
        elif child.type == "assignment":
            _extract_variable(rel_path, child, "module", checksum, result)


# ---------------------------------------------------------------------------
# Function extraction
# ---------------------------------------------------------------------------


def _extract_function(
    rel_path: str,
    node: Node,
    checksum: str,
    result: ExtractionResult,
    decorators: list[str] | None = None,
) -> None:
    name = _get_child_text(node, "name")
    if not name:
        return

    func_id = f"{rel_path}::{name}"
    params = _extract_params(node)
    return_type = _get_return_type(node)
    is_async = node.type == "function_definition" and _is_async_function(node)
    line_range = [node.start_point[0] + 1, node.end_point[0] + 1]

    result.nodes.append(make_node(
        id=func_id,
        node_type=NT_FUNCTION,
        source_file=rel_path,
        checksum=checksum,
        name=name,
        module=rel_path,
        params=params,
        return_type=return_type,
        decorators=decorators or [],
        line_range=line_range,
        is_async=is_async,
    ))


# ---------------------------------------------------------------------------
# Class extraction
# ---------------------------------------------------------------------------


def _extract_class(
    rel_path: str,
    node: Node,
    checksum: str,
    result: ExtractionResult,
    decorators: list[str] | None = None,  # noqa: ARG001 — reserved for future use
) -> None:
    name = _get_child_text(node, "name")
    if not name:
        return

    class_id = f"{rel_path}::{name}"
    bases = _get_bases(node)
    methods: list[str] = []
    attributes: list[str] = []

    body = node.child_by_field_name("body")
    if body:
        for item in body.children:
            if item.type == "function_definition":
                method_name = _get_child_text(item, "name")
                if method_name:
                    methods.append(method_name)
                    _extract_function(rel_path, item, checksum, result)
            elif item.type == "decorated_definition":
                inner = _get_decorated_inner(item)
                if inner and inner.type == "function_definition":
                    method_name = _get_child_text(inner, "name")
                    if method_name:
                        methods.append(method_name)
                        _extract_function(rel_path, inner, checksum, result, decorators=_get_decorators(item))
            elif item.type == "expression_statement":
                assign = item.children[0] if item.children else None
                if assign and assign.type == "assignment":
                    attr_name = _get_assignment_name(assign)
                    if attr_name:
                        attributes.append(attr_name)
            elif item.type == "assignment":
                attr_name = _get_assignment_name(item)
                if attr_name:
                    attributes.append(attr_name)

    line_range = [node.start_point[0] + 1, node.end_point[0] + 1]

    result.nodes.append(make_node(
        id=class_id,
        node_type=NT_CLASS,
        source_file=rel_path,
        checksum=checksum,
        name=name,
        module=rel_path,
        bases=bases,
        methods=methods,
        attributes=attributes,
        line_range=line_range,
    ))

    for base_name in bases:
        if base_name:
            result.edges.append(make_edge(
                source=class_id,
                target=f"{rel_path}::{base_name}",
                edge_type=ET_INHERITS,
            ))


# ---------------------------------------------------------------------------
# Import resolution helpers
# ---------------------------------------------------------------------------


def _resolve_relative_import(rel_path: str, module_name: str) -> str:
    if not module_name.startswith("."):
        # Absolute import: convert dotted module to file path
        # e.g. "src.config" → "src/config.py"
        # Single-segment names (os, pathlib) stay as-is — they're stdlib/third-party
        if "." in module_name:
            return "/".join(module_name.split(".")) + ".py"
        return module_name

    dots = 0
    for ch in module_name:
        if ch == ".":
            dots += 1
        else:
            break

    remainder = module_name[dots:]
    parts = rel_path.replace("\\", "/").split("/")
    dir_parts = parts[:-1]

    levels_up = dots - 1
    if levels_up > len(dir_parts):
        return module_name

    dir_parts = dir_parts[: len(dir_parts) - levels_up]

    if remainder:
        target_parts = dir_parts + remainder.split(".")
    else:
        target_parts = dir_parts + ["__init__"]

    return "/".join(target_parts) + ".py"


def _get_imported_names(node: Node) -> list[tuple[str, str]]:
    names: list[tuple[str, str]] = []
    found_import = False
    for child in node.children:
        if child.type == "import":
            found_import = True
            continue
        if not found_import:
            continue
        if child.type == "dotted_name" and child.text:
            name = child.text.decode("utf-8")
            names.append((name, name))
        elif child.type == "aliased_import":
            original = None
            alias = None
            for sub in child.children:
                if sub.type == "dotted_name" and sub.text and original is None:
                    original = sub.text.decode("utf-8")
                elif sub.type == "identifier" and sub.text:
                    alias = sub.text.decode("utf-8")
            if original:
                names.append((original, alias or original))
    return names


# ---------------------------------------------------------------------------
# Import edges
# ---------------------------------------------------------------------------


def _extract_import_edges(
    rel_path: str,
    root: Node,
    result: ExtractionResult,
) -> dict[str, str]:
    import_map: dict[str, str] = {}

    for node in _iter_type(root, "import_from_statement"):
        module_name = _get_import_from_module(node)
        if not module_name:
            continue

        resolved = _resolve_relative_import(rel_path, module_name)
        result.edges.append(make_edge(
            source=rel_path,
            target=resolved,
            edge_type=ET_IMPORTS,
            line=node.start_point[0] + 1,
            module_name=module_name,
        ))

        if module_name.startswith("."):
            for original_name, local_name in _get_imported_names(node):
                import_map[local_name] = f"{resolved}::{original_name}"

    for node in _iter_type(root, "import_statement"):
        for child in node.children:
            if child.type == "dotted_name" and child.text:
                module_name = child.text.decode("utf-8")
                result.edges.append(make_edge(
                    source=rel_path,
                    target=module_name,
                    edge_type=ET_IMPORTS,
                    line=node.start_point[0] + 1,
                    module_name=module_name,
                ))

    return import_map


# ---------------------------------------------------------------------------
# Call edges
# ---------------------------------------------------------------------------


def _extract_call_edges(
    rel_path: str,
    root: Node,
    result: ExtractionResult,
    import_map: dict[str, str],
) -> None:
    for func_node in root.children:
        actual = func_node
        if func_node.type == "decorated_definition":
            actual = _get_decorated_inner(func_node)
            if not actual:
                continue

        if actual.type == "function_definition":
            func_name = _get_child_text(actual, "name")
            if func_name:
                caller_id = f"{rel_path}::{func_name}"
                _collect_calls(rel_path, caller_id, actual, result, import_map)
        elif actual.type == "class_definition":
            body = actual.child_by_field_name("body")
            if body:
                for method in body.children:
                    m = method
                    if method.type == "decorated_definition":
                        m = _get_decorated_inner(method)
                        if not m:
                            continue
                    if m.type == "function_definition":
                        method_name = _get_child_text(m, "name")
                        if method_name:
                            caller_id = f"{rel_path}::{method_name}"
                            _collect_calls(rel_path, caller_id, m, result, import_map)


def _collect_calls(
    rel_path: str,
    caller_id: str,
    func_node: Node,
    result: ExtractionResult,
    import_map: dict[str, str],
) -> None:
    seen: set[str] = set()
    for call_node in _iter_type(func_node, "call"):
        callee = _call_target_name(call_node)
        if not callee:
            continue
        target_id = import_map.get(callee, f"{rel_path}::{callee}")
        if target_id not in seen:
            seen.add(target_id)
            result.edges.append(make_edge(
                source=caller_id,
                target=target_id,
                edge_type=ET_CALLS,
                line=call_node.start_point[0] + 1,
            ))


# ---------------------------------------------------------------------------
# Environment variable dependencies
# ---------------------------------------------------------------------------


def _extract_env_var_deps(
    rel_path: str,
    root: Node,
    result: ExtractionResult,
) -> None:
    env_vars = _collect_env_var_names(root)
    for var_name in env_vars:
        env_node_id = f"config::env_var::{var_name}"
        # Create the env var node so the edge survives graph building
        result.nodes.append(make_node(
            id=env_node_id,
            node_type="ConfigNode",
            source_file=rel_path,
            checksum="",
            config_type="env_var",
            key=var_name,
        ))
        result.edges.append(make_edge(
            source=rel_path,
            target=env_node_id,
            edge_type=ET_DEPENDS_ENV,
        ))


# ---------------------------------------------------------------------------
# File I/O edges (reads / writes)
# ---------------------------------------------------------------------------


def _extract_file_io_edges(
    rel_path: str,
    text: str,
    result: ExtractionResult,
) -> None:
    """Detect file path references and open/write patterns.

    Strategy: find string literals that look like project file paths,
    then classify as reads or writes based on surrounding context.
    """
    seen: set[str] = set()

    # Find all string literals that look like file paths
    for match in re.finditer(r'["\']([^"\']{3,80})["\']', text):
        path = match.group(1)
        if not _is_project_path(path) or path in seen:
            continue
        seen.add(path)

        # Check surrounding context (±2 lines) for write indicators
        start = max(0, text.rfind('\n', 0, match.start()) - 200)
        end = min(len(text), text.find('\n', match.end()) + 200)
        context = text[start:end]

        is_write = bool(re.search(
            r'write_text|write_bytes|"w"|\'w\'|json\.dump|yaml\.dump|\.write\(',
            context,
        ))

        edge_type = ET_WRITES if is_write else ET_READS
        result.edges.append(make_edge(
            source=rel_path,
            target=path,
            edge_type=edge_type,
            line=text[:match.start()].count('\n') + 1,
        ))


def _is_project_path(path: str) -> bool:
    """Check if a string looks like a project-relative file reference."""
    if not path:
        return False
    # Must have a file extension
    parts = path.split("/")
    if "." not in parts[-1]:
        return False
    # Skip URLs, absolute system paths, template strings
    if path.startswith(("http://", "https://", "ftp://", "/dev/", "/tmp/", "/proc/", "/etc/")):
        return False
    if "{" in path or "$" in path:
        return False
    # Must look like a relative path with known extensions
    ext = "." + parts[-1].rsplit(".", 1)[-1]
    known_exts = {".py", ".json", ".yaml", ".yml", ".toml", ".md", ".txt", ".sh", ".cfg", ".ini", ".env"}
    return ext in known_exts


# ---------------------------------------------------------------------------
# Docstring edges
# ---------------------------------------------------------------------------


def _extract_docstring_edges(
    rel_path: str,
    root: Node,
    checksum: str,
    result: ExtractionResult,
) -> None:
    for node in root.children:
        actual = node
        if node.type == "decorated_definition":
            actual = _get_decorated_inner(node)
            if not actual:
                continue

        if actual.type in ("function_definition", "class_definition"):
            name = _get_child_text(actual, "name")
            docstring = _get_docstring(actual)
            if name and docstring:
                target_id = f"{rel_path}::{name}"
                doc_node_id = f"doc::{rel_path}::{name}"
                result.nodes.append(make_node(
                    id=doc_node_id,
                    node_type=NT_DOC,
                    source_file=rel_path,
                    checksum=checksum,
                    title=f"Docstring for {name}",
                    content_type="docstring",
                    docstring=docstring,
                ))
                result.edges.append(make_edge(
                    source=doc_node_id,
                    target=target_id,
                    edge_type=ET_DOCUMENTS,
                    line=actual.start_point[0] + 1,
                ))


# ---------------------------------------------------------------------------
# Tree-sitter helpers
# ---------------------------------------------------------------------------


def _iter_type(node: Node, type_name: str):
    """Recursively yield all descendant nodes of a given type."""
    for child in node.children:
        if child.type == type_name:
            yield child
        yield from _iter_type(child, type_name)


def _get_child_text(node: Node, field_name: str) -> str | None:
    child = node.child_by_field_name(field_name)
    if child and child.text:
        return child.text.decode("utf-8")
    return None


def _get_decorated_inner(node: Node) -> Node | None:
    for child in node.children:
        if child.type in ("function_definition", "class_definition"):
            return child
    return None


def _get_decorators(node: Node) -> list[str]:
    decorators: list[str] = []
    for child in node.children:
        if child.type == "decorator":
            text = child.text.decode("utf-8").lstrip("@").strip() if child.text else ""
            decorators.append(text)
    return decorators


def _is_async_function(node: Node) -> bool:
    if node.parent and node.parent.type == "decorated_definition":
        for child in node.parent.children:
            if child.type == "async":
                return True
    for child in node.children:
        if child.type == "async":
            return True
    return bool(node.type == "function_definition" and node.parent and any(
        c.type == "async" for c in node.parent.children if c.end_byte <= node.start_byte
    ))


def _get_return_type(node: Node) -> str | None:
    ret = node.child_by_field_name("return_type")
    if ret and ret.text:
        return ret.text.decode("utf-8")
    return None


def _get_bases(node: Node) -> list[str]:
    bases: list[str] = []
    superclasses = node.child_by_field_name("superclasses")
    if superclasses:
        for child in superclasses.children:
            if child.type in ("identifier", "attribute") and child.text:
                bases.append(child.text.decode("utf-8"))
    return bases


def _get_assignment_name(node: Node) -> str | None:
    left = node.child_by_field_name("left")
    if left and left.type == "identifier" and left.text:
        return left.text.decode("utf-8")
    return None


def _extract_variable(
    rel_path: str,
    node: Node,
    scope: str,
    checksum: str,
    result: ExtractionResult,
) -> None:
    name = _get_assignment_name(node)
    if not name:
        return
    var_id = f"{rel_path}::{scope}::{name}"
    result.nodes.append(make_node(
        id=var_id,
        node_type=NT_VARIABLE,
        source_file=rel_path,
        checksum=checksum,
        name=name,
        scope=scope,
        type_hint=None,
    ))


def _extract_params(node: Node) -> list[dict[str, Any]]:
    params: list[dict[str, Any]] = []
    parameters = node.child_by_field_name("parameters")
    if not parameters:
        return params
    for child in parameters.children:
        if child.type in ("identifier",):
            params.append({"name": child.text.decode("utf-8") if child.text else "?", "type": None})
        elif child.type == "typed_parameter":
            pname = child.child_by_field_name("name")
            ptype = child.child_by_field_name("type")
            params.append({
                "name": pname.text.decode("utf-8") if pname and pname.text else "?",
                "type": ptype.text.decode("utf-8") if ptype and ptype.text else None,
            })
        elif child.type == "default_parameter":
            pname = child.child_by_field_name("name")
            if pname and pname.text:
                params.append({"name": pname.text.decode("utf-8"), "type": None})
        elif child.type == "typed_default_parameter":
            pname = child.child_by_field_name("name")
            ptype = child.child_by_field_name("type")
            params.append({
                "name": pname.text.decode("utf-8") if pname and pname.text else "?",
                "type": ptype.text.decode("utf-8") if ptype and ptype.text else None,
            })
    return params


def _call_target_name(node: Node) -> str | None:
    func = node.child_by_field_name("function")
    if not func:
        return None
    if func.type == "identifier":
        return func.text.decode("utf-8") if func.text else None
    if func.type == "attribute":
        attr = func.child_by_field_name("attribute")
        if attr and attr.text:
            return attr.text.decode("utf-8")
    return None


def _get_import_from_module(node: Node) -> str | None:
    for child in node.children:
        if child.type == "dotted_name" and child.text:
            return child.text.decode("utf-8")
        if child.type == "relative_import" and child.text:
            return child.text.decode("utf-8")
    return None


def _collect_imports(root: Node) -> list[str]:
    imports: set[str] = set()
    for node in _iter_type(root, "import_from_statement"):
        module_name = _get_import_from_module(node)
        if module_name:
            imports.add(module_name)
    for node in _iter_type(root, "import_statement"):
        for child in node.children:
            if child.type == "dotted_name" and child.text:
                imports.add(child.text.decode("utf-8"))
    return sorted(imports)


def _collect_exports(root: Node) -> list[str]:
    for node in root.children:
        if node.type in ("expression_statement",):
            assign = node.children[0] if node.children else None
            if assign and assign.type == "assignment":
                left = assign.child_by_field_name("left")
                if left and left.type == "identifier" and left.text == b"__all__":
                    right = assign.child_by_field_name("right")
                    if right and right.type == "list":
                        return [
                            el.text.decode("utf-8").strip("\"'")
                            for el in right.children
                            if el.type == "string" and el.text
                        ]
        elif node.type == "assignment":
            left = node.child_by_field_name("left")
            if left and left.type == "identifier" and left.text == b"__all__":
                right = node.child_by_field_name("right")
                if right and right.type == "list":
                    return [
                        el.text.decode("utf-8").strip("\"'")
                        for el in right.children
                        if el.type == "string" and el.text
                    ]

    names: list[str] = []
    for node in root.children:
        actual = node
        if node.type == "decorated_definition":
            actual = _get_decorated_inner(node)
            if not actual:
                continue
        if actual.type == "function_definition":
            name = _get_child_text(actual, "name")
            if name and not name.startswith("_"):
                names.append(name)
        elif actual.type == "class_definition":
            name = _get_child_text(actual, "name")
            if name and not name.startswith("_"):
                names.append(name)
    return sorted(names)


def _collect_env_var_names(root: Node) -> list[str]:
    env_vars: set[str] = set()
    for call_node in _iter_type(root, "call"):
        func = call_node.child_by_field_name("function")
        if not func or not func.text:
            continue
        func_text = func.text.decode("utf-8")
        if func_text in ("os.getenv", "os.environ.get"):
            args = call_node.child_by_field_name("arguments")
            if args:
                first_arg = _get_first_string_arg(args)
                if first_arg:
                    env_vars.add(first_arg)

    for sub_node in _iter_type(root, "subscript"):
        obj = sub_node.child_by_field_name("value")
        if obj and obj.text and obj.text.decode("utf-8") == "os.environ":
            subscript = sub_node.child_by_field_name("subscript")
            if subscript and subscript.type == "string" and subscript.text:
                env_vars.add(subscript.text.decode("utf-8").strip("\"'"))
    return sorted(env_vars)


def _get_first_string_arg(args_node: Node) -> str | None:
    for child in args_node.children:
        if child.type == "string" and child.text:
            return child.text.decode("utf-8").strip("\"'")
    return None


def _get_docstring(node: Node) -> str | None:
    body = node.child_by_field_name("body")
    if not body or not body.children:
        return None
    first = body.children[0]
    if first.type == "expression_statement" and first.children:
        expr = first.children[0]
        if expr.type == "string" and expr.text:
            raw = expr.text.decode("utf-8")
            if raw.startswith('"""') or raw.startswith("'''"):
                return raw[3:-3].strip()
            if raw.startswith('"') or raw.startswith("'"):
                return raw[1:-1].strip()
    return None


def _extract_var_reads_writes(
    rel_path: str,
    source: bytes,
    result: ExtractionResult,
) -> None:
    """Create reads/writes edges from functions to module-level variables.

    Ported from the old knowledge skill extractor. Detects data flow:
    - function reads a module-level variable → reads edge
    - function writes to a module-level variable → writes edge
    """
    import ast as stdlib_ast

    try:
        tree = stdlib_ast.parse(source)
    except SyntaxError:
        return

    # Collect module-level variable names
    module_vars: set[str] = set()
    for node in stdlib_ast.iter_child_nodes(tree):
        if isinstance(node, stdlib_ast.Assign):
            for target in node.targets:
                if isinstance(target, stdlib_ast.Name):
                    module_vars.add(target.id)
        elif isinstance(node, stdlib_ast.AnnAssign) and isinstance(node.target, stdlib_ast.Name):
            module_vars.add(node.target.id)

    if not module_vars:
        return

    # Walk functions and methods, emit reads/writes to module vars
    for func_node in stdlib_ast.iter_child_nodes(tree):
        if isinstance(func_node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
            _collect_var_rw(rel_path, func_node, module_vars, result)
        elif isinstance(func_node, stdlib_ast.ClassDef):
            for method in func_node.body:
                if isinstance(method, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef)):
                    _collect_var_rw(rel_path, method, module_vars, result)


def _collect_var_rw(
    rel_path: str,
    func_node,
    module_vars: set[str],
    result: ExtractionResult,
) -> None:
    import ast as stdlib_ast

    func_id = f"{rel_path}::{func_node.name}"
    local_names: set[str] = {arg.arg for arg in func_node.args.args}

    # Pass 1: collect all locally-assigned names (Python scoping rule:
    # if x is assigned ANYWHERE in a function, ALL references are local)
    # Exception: names declared `global` are explicitly NOT local
    global_names: set[str] = set()
    for child in stdlib_ast.walk(func_node):
        if isinstance(child, stdlib_ast.Global):
            global_names.update(child.names)

    for child in stdlib_ast.walk(func_node):
        if isinstance(child, stdlib_ast.Name) and isinstance(child.ctx, stdlib_ast.Store):
            if child.id not in global_names:
                local_names.add(child.id)

    # Pass 2: only emit edges for names that aren't locally shadowed
    reads_seen: set[str] = set()
    writes_seen: set[str] = set()

    for child in stdlib_ast.walk(func_node):
        if not isinstance(child, stdlib_ast.Name):
            continue
        name = child.id
        if name in local_names or name not in module_vars:
            continue
        var_id = f"{rel_path}::module::{name}"
        if isinstance(child.ctx, stdlib_ast.Store) and name not in writes_seen:
            writes_seen.add(name)
            result.edges.append(make_edge(
                source=func_id,
                target=var_id,
                edge_type="writes",
            ))
        elif isinstance(child.ctx, stdlib_ast.Load) and name not in reads_seen:
            reads_seen.add(name)
            result.edges.append(make_edge(
                source=func_id,
                target=var_id,
                edge_type="reads",
            ))
