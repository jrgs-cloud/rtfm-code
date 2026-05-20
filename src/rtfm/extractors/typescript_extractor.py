"""Extract code structure nodes and edges from TypeScript/TSX source files using tree-sitter."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from typing import Any

try:
    import tree_sitter_typescript as tsts
    from tree_sitter import Language, Node, Parser
except ImportError as e:
    raise ImportError(
        "pip install rtfm[typescript]"
    ) from e

from rtfm.core.types import (
    ET_CALLS,
    ET_IMPORTS,
    ET_INHERITS,
    NT_CLASS,
    NT_FUNCTION,
    NT_MODULE,
    NT_TYPE,
    NT_VARIABLE,
    ExtractionResult,
    make_edge,
    make_node,
)

_LANG_TS = Language(tsts.language_typescript())
_LANG_TSX = Language(tsts.language_tsx())
_thread_local = threading.local()

def _get_parser(tsx: bool = False) -> Parser:
    attr = "parser_tsx" if tsx else "parser_ts"
    if not hasattr(_thread_local, attr):
        setattr(_thread_local, attr, Parser(_LANG_TSX if tsx else _LANG_TS))
    return getattr(_thread_local, attr)


def extract(file_path: Path, project_root: Path, config: dict) -> ExtractionResult:
    """Extract TypeScript/TSX AST nodes and edges from a source file using tree-sitter.

    Produces ModuleNode, FunctionNode, ClassNode, TypeNode with import/call/inherits edges.
    Selects TSX parser for .tsx files, TypeScript parser otherwise.

    Args:
        file_path: Absolute path to the TS/TSX source file.
        project_root: Project root for computing relative paths.
        config: Extractor config (supports allow_partial_parse).

    Returns:
        ExtractionResult with nodes and edges for the file.
    """
    result = ExtractionResult()

    try:
        source = file_path.read_bytes()
    except OSError:
        return result

    parser = _get_parser(tsx=file_path.suffix == ".tsx")
    tree = parser.parse(source)
    if tree.root_node.has_error and not config.get("allow_partial_parse", False):
        return result

    rel_path = str(file_path.relative_to(project_root))
    text = source.decode("utf-8", errors="replace")
    checksum = hashlib.sha256(source).hexdigest()

    _extract_module(rel_path, tree.root_node, text, checksum, result)
    _extract_top_level(rel_path, tree.root_node, checksum, result)
    _extract_import_edges(rel_path, tree.root_node, result)
    _extract_call_edges(rel_path, tree.root_node, result)

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
    line_count = text.count("\n") + 1

    result.nodes.append(make_node(
        id=rel_path,
        node_type=NT_MODULE,
        source_file=rel_path,
        checksum=checksum,
        imports=imports,
        exports=exports,
        line_count=line_count,
    ))


# ---------------------------------------------------------------------------
# Top-level dispatch
# ---------------------------------------------------------------------------

_FUNCTION_KINDS = frozenset({
    "function_declaration",
    "generator_function_declaration",
})

_TYPE_KINDS = frozenset({
    "interface_declaration",
    "type_alias_declaration",
    "enum_declaration",
})


def _extract_top_level(
    rel_path: str,
    root: Node,
    checksum: str,
    result: ExtractionResult,
) -> None:
    for child in root.children:
        _dispatch_node(rel_path, child, checksum, result)


def _dispatch_node(
    rel_path: str,
    node: Node,
    checksum: str,
    result: ExtractionResult,
) -> None:
    ntype = node.type

    if ntype in _FUNCTION_KINDS:
        _extract_function(rel_path, node, checksum, result)
    elif ntype == "class_declaration":
        _extract_class(rel_path, node, checksum, result)
    elif ntype in _TYPE_KINDS:
        _extract_type_declaration(rel_path, node, checksum, result)
    elif ntype == "lexical_declaration":
        _extract_lexical_declaration(rel_path, node, checksum, result)
    elif ntype == "variable_declaration":
        _extract_lexical_declaration(rel_path, node, checksum, result)
    elif ntype == "export_statement":
        _dispatch_export(rel_path, node, checksum, result)
    elif ntype == "module" or ntype == "internal_module":
        _extract_namespace(rel_path, node, checksum, result)
    elif ntype == "expression_statement":
        for child in node.children:
            if child.type == "internal_module":
                _extract_namespace(rel_path, child, checksum, result)


def _dispatch_export(
    rel_path: str,
    node: Node,
    checksum: str,
    result: ExtractionResult,
) -> None:
    for child in node.children:
        if child.type in _FUNCTION_KINDS:
            _extract_function(rel_path, child, checksum, result, exported=True)
        elif child.type == "class_declaration":
            _extract_class(rel_path, child, checksum, result, exported=True)
        elif child.type in _TYPE_KINDS:
            _extract_type_declaration(rel_path, child, checksum, result)
        elif child.type == "lexical_declaration":
            _extract_lexical_declaration(rel_path, child, checksum, result)
        elif child.type == "variable_declaration":
            _extract_lexical_declaration(rel_path, child, checksum, result)
        elif child.type == "module" or child.type == "internal_module":
            _extract_namespace(rel_path, child, checksum, result)


# ---------------------------------------------------------------------------
# Function extraction
# ---------------------------------------------------------------------------


def _extract_function(
    rel_path: str,
    node: Node,
    checksum: str,
    result: ExtractionResult,
    decorators: list[str] | None = None,
    exported: bool = False,
) -> None:
    name = _get_child_text(node, "name")
    if not name:
        return

    func_id = f"{rel_path}::{name}"
    params = _extract_params(node)
    return_type = _get_return_type(node)
    is_async = any(c.type == "async" for c in node.children)
    is_generator = node.type == "generator_function_declaration"
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
        is_generator=is_generator,
        exported=exported,
    ))


def _extract_arrow_function(
    rel_path: str,
    name: str,
    node: Node,
    checksum: str,
    result: ExtractionResult,
) -> None:
    func_id = f"{rel_path}::{name}"
    params = _extract_params(node)
    return_type = _get_return_type(node)
    is_async = node.parent and any(
        c.type == "async" for c in node.parent.children if c.end_byte <= node.start_byte
    )
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
        decorators=[],
        line_range=line_range,
        is_async=bool(is_async),
        is_generator=False,
        exported=False,
    ))


# ---------------------------------------------------------------------------
# Class extraction
# ---------------------------------------------------------------------------


def _extract_class(
    rel_path: str,
    node: Node,
    checksum: str,
    result: ExtractionResult,
    decorators: list[str] | None = None,
    exported: bool = False,
) -> None:
    name = _get_child_text(node, "name")
    if not name:
        return

    class_id = f"{rel_path}::{name}"
    bases = _get_heritage(node)
    methods: list[str] = []
    attributes: list[str] = []

    body = node.child_by_field_name("body")
    if body:
        for item in body.children:
            if item.type == "method_definition":
                method_name = _get_child_text(item, "name")
                if method_name:
                    methods.append(method_name)
                    method_decorators = _get_decorators(item)
                    _extract_method(rel_path, item, checksum, result, method_decorators)
            elif item.type in ("public_field_definition", "property_signature"):
                attr_name = _get_child_text(item, "name")
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
        decorators=decorators or [],
        line_range=line_range,
        exported=exported,
    ))

    for base_name in bases:
        if base_name:
            result.edges.append(make_edge(
                source=class_id,
                target=f"{rel_path}::{base_name}",
                edge_type=ET_INHERITS,
            ))


def _extract_method(
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
    is_async = any(c.type == "async" for c in node.children)
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
# Type declarations (interface, type alias, enum)
# ---------------------------------------------------------------------------


def _extract_type_declaration(
    rel_path: str,
    node: Node,
    checksum: str,
    result: ExtractionResult,
) -> None:
    name = _get_child_text(node, "name")
    if not name:
        return

    type_id = f"{rel_path}::{name}"
    kind = node.type.replace("_declaration", "").replace("_alias", "")
    line_range = [node.start_point[0] + 1, node.end_point[0] + 1]

    heritage: list[str] = []
    if node.type == "interface_declaration":
        heritage = _get_heritage(node)

    members: list[str] = []
    body = node.child_by_field_name("body")
    if body:
        for item in body.children:
            member_name = _get_child_text(item, "name")
            if member_name:
                members.append(member_name)

    result.nodes.append(make_node(
        id=type_id,
        node_type=NT_TYPE,
        source_file=rel_path,
        checksum=checksum,
        name=name,
        module=rel_path,
        kind=kind,
        members=members,
        line_range=line_range,
    ))

    for base_name in heritage:
        if base_name:
            result.edges.append(make_edge(
                source=type_id,
                target=f"{rel_path}::{base_name}",
                edge_type=ET_INHERITS,
            ))


# ---------------------------------------------------------------------------
# Namespace / internal module
# ---------------------------------------------------------------------------


def _extract_namespace(
    rel_path: str,
    node: Node,
    checksum: str,
    result: ExtractionResult,
) -> None:
    name = _get_child_text(node, "name")
    if not name:
        return

    ns_id = f"{rel_path}::{name}"
    line_range = [node.start_point[0] + 1, node.end_point[0] + 1]

    result.nodes.append(make_node(
        id=ns_id,
        node_type=NT_MODULE,
        source_file=rel_path,
        checksum=checksum,
        name=name,
        module=rel_path,
        kind="namespace",
        line_range=line_range,
    ))

    body = node.child_by_field_name("body")
    if body:
        for child in body.children:
            _dispatch_node(rel_path, child, checksum, result)


# ---------------------------------------------------------------------------
# Lexical declarations (const/let/var — may contain arrow functions)
# ---------------------------------------------------------------------------


def _extract_lexical_declaration(
    rel_path: str,
    node: Node,
    checksum: str,
    result: ExtractionResult,
) -> None:
    for child in node.children:
        if child.type == "variable_declarator":
            name = _get_child_text(child, "name")
            if not name:
                continue
            value = child.child_by_field_name("value")
            if value and value.type == "arrow_function":
                _extract_arrow_function(rel_path, name, value, checksum, result)
            elif value and value.type == "function":
                _extract_function(rel_path, value, checksum, result)
            else:
                var_id = f"{rel_path}::module::{name}"
                result.nodes.append(make_node(
                    id=var_id,
                    node_type=NT_VARIABLE,
                    source_file=rel_path,
                    checksum=checksum,
                    name=name,
                    scope="module",
                    type_hint=_get_type_annotation(child),
                ))


# ---------------------------------------------------------------------------
# Import edges
# ---------------------------------------------------------------------------


def _extract_import_edges(
    rel_path: str,
    root: Node,
    result: ExtractionResult,
) -> None:
    for node in _iter_type(root, "import_statement"):
        source_node = node.child_by_field_name("source")
        if source_node and source_node.text:
            module_name = _unquote(source_node.text.decode("utf-8"))
            result.edges.append(make_edge(
                source=rel_path,
                target=module_name,
                edge_type=ET_IMPORTS,
                line=node.start_point[0] + 1,
                module_name=module_name,
            ))


# ---------------------------------------------------------------------------
# Call edges (best-effort, same-file + explicit imports)
# ---------------------------------------------------------------------------


def _extract_call_edges(
    rel_path: str,
    root: Node,
    result: ExtractionResult,
) -> None:
    for child in root.children:
        actual = child
        if child.type == "export_statement":
            for sub in child.children:
                if sub.type in _FUNCTION_KINDS or sub.type == "class_declaration":
                    actual = sub
                    break
            else:
                continue

        if actual.type in _FUNCTION_KINDS:
            func_name = _get_child_text(actual, "name")
            if func_name:
                caller_id = f"{rel_path}::{func_name}"
                _collect_calls(rel_path, caller_id, actual, result)
        elif actual.type == "class_declaration":
            body = actual.child_by_field_name("body")
            if body:
                for method in body.children:
                    if method.type == "method_definition":
                        method_name = _get_child_text(method, "name")
                        if method_name:
                            caller_id = f"{rel_path}::{method_name}"
                            _collect_calls(rel_path, caller_id, method, result)
        elif actual.type == "lexical_declaration" or actual.type == "variable_declaration":
            for decl in actual.children:
                if decl.type == "variable_declarator":
                    vname = _get_child_text(decl, "name")
                    value = decl.child_by_field_name("value")
                    if vname and value and value.type == "arrow_function":
                        caller_id = f"{rel_path}::{vname}"
                        _collect_calls(rel_path, caller_id, value, result)


def _collect_calls(
    rel_path: str,
    caller_id: str,
    func_node: Node,
    result: ExtractionResult,
) -> None:
    seen: set[str] = set()
    for call_node in _iter_type(func_node, "call_expression"):
        callee = _call_target_name(call_node)
        if not callee:
            continue
        target_id = f"{rel_path}::{callee}"
        if target_id not in seen:
            seen.add(target_id)
            result.edges.append(make_edge(
                source=caller_id,
                target=target_id,
                edge_type=ET_CALLS,
                line=call_node.start_point[0] + 1,
            ))


# ---------------------------------------------------------------------------
# Tree-sitter helpers
# ---------------------------------------------------------------------------


def _iter_type(node: Node, type_name: str):
    for child in node.children:
        if child.type == type_name:
            yield child
        yield from _iter_type(child, type_name)


def _get_child_text(node: Node, field_name: str) -> str | None:
    child = node.child_by_field_name(field_name)
    if child and child.text:
        return child.text.decode("utf-8")
    return None


def _get_decorators(node: Node) -> list[str]:
    decorators: list[str] = []
    if node.parent and node.parent.type == "export_statement":
        node = node.parent
    prev = node.prev_sibling
    while prev and prev.type == "decorator":
        text = prev.text.decode("utf-8").lstrip("@").strip() if prev.text else ""
        decorators.insert(0, text)
        prev = prev.prev_sibling
    return decorators


def _get_heritage(node: Node) -> list[str]:
    bases: list[str] = []
    for child in node.children:
        if child.type in ("extends_clause", "extends_type_clause"):
            for sub in child.children:
                if sub.type in ("identifier", "type_identifier", "nested_identifier") and sub.text:
                    bases.append(sub.text.decode("utf-8"))
        elif child.type == "implements_clause":
            for sub in child.children:
                if sub.type in ("identifier", "type_identifier", "nested_identifier") and sub.text:
                    bases.append(sub.text.decode("utf-8"))
    return bases


def _get_return_type(node: Node) -> str | None:
    ret = node.child_by_field_name("return_type")
    if ret and ret.text:
        text = ret.text.decode("utf-8")
        return text.lstrip(":").strip()
    return None


def _get_type_annotation(node: Node) -> str | None:
    ta = node.child_by_field_name("type")
    if ta and ta.text:
        return ta.text.decode("utf-8")
    return None


def _extract_params(node: Node) -> list[dict[str, Any]]:
    params: list[dict[str, Any]] = []
    parameters = node.child_by_field_name("parameters")
    if not parameters:
        return params
    for child in parameters.children:
        if child.type == "required_parameter" or child.type == "optional_parameter":
            pname = child.child_by_field_name("pattern") or child.child_by_field_name("name")
            ptype = child.child_by_field_name("type")
            params.append({
                "name": pname.text.decode("utf-8") if pname and pname.text else "?",
                "type": ptype.text.decode("utf-8").lstrip(":").strip() if ptype and ptype.text else None,
            })
        elif child.type == "rest_parameter":
            pname = child.child_by_field_name("name") or child.child_by_field_name("pattern")
            ptype = child.child_by_field_name("type")
            params.append({
                "name": f"...{pname.text.decode('utf-8')}" if pname and pname.text else "...?",
                "type": ptype.text.decode("utf-8").lstrip(":").strip() if ptype and ptype.text else None,
            })
    return params


def _call_target_name(node: Node) -> str | None:
    func = node.child_by_field_name("function")
    if not func:
        return None
    if func.type == "identifier":
        return func.text.decode("utf-8") if func.text else None
    if func.type == "member_expression":
        prop = func.child_by_field_name("property")
        if prop and prop.text:
            return prop.text.decode("utf-8")
    return None


def _unquote(s: str) -> str:
    if len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]:
        return s[1:-1]
    return s


def _collect_imports(root: Node) -> list[str]:
    imports: set[str] = set()
    for node in _iter_type(root, "import_statement"):
        source_node = node.child_by_field_name("source")
        if source_node and source_node.text:
            imports.add(_unquote(source_node.text.decode("utf-8")))
    return sorted(imports)


def _collect_exports(root: Node) -> list[str]:
    exports: set[str] = set()
    for node in root.children:
        if node.type == "export_statement":
            for child in node.children:
                name = _get_child_text(child, "name")
                if name:
                    exports.add(name)
    return sorted(exports)
