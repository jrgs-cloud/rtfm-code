"""Tests for Jedi enricher file IO resolution functions."""
import ast
import pytest

from rtfm.core.jedi_enricher import (
    _extract_string_from_expr,
    _resolve_arg_to_path,
    _match_path_to_node,
    _get_call_name,
    _is_write_context,
)


class TestExtractStringFromExpr:
    def test_string_literal_with_extension(self):
        node = ast.Constant(value="settings.json")
        assert _extract_string_from_expr(node) == "settings.json"

    def test_string_literal_no_extension(self):
        node = ast.Constant(value="not_a_file")
        assert _extract_string_from_expr(node) is None

    def test_path_div_operator(self):
        # Simulates: ROOT / "config.json"
        node = ast.BinOp(
            left=ast.Name(id="ROOT", ctx=ast.Load()),
            op=ast.Div(),
            right=ast.Constant(value="config.json"),
        )
        assert _extract_string_from_expr(node) == "config.json"

    def test_path_call(self):
        # Simulates: Path("config.toml")
        node = ast.Call(
            func=ast.Name(id="Path", ctx=ast.Load()),
            args=[ast.Constant(value="config.toml")],
            keywords=[],
        )
        assert _extract_string_from_expr(node) == "config.toml"

    def test_integer_constant(self):
        node = ast.Constant(value=42)
        assert _extract_string_from_expr(node) is None

    def test_string_with_path_separator(self):
        node = ast.Constant(value="src/config.yaml")
        assert _extract_string_from_expr(node) == "src/config.yaml"


class TestResolveArgToPath:
    def test_string_literal(self):
        node = ast.Constant(value="settings.json")
        assert _resolve_arg_to_path(node, {}) == "settings.json"

    def test_variable_in_string_vars(self):
        node = ast.Name(id="config_path", ctx=ast.Load())
        string_vars = {"config_path": "app.yaml"}
        assert _resolve_arg_to_path(node, string_vars) == "app.yaml"

    def test_variable_not_in_string_vars(self):
        node = ast.Name(id="unknown", ctx=ast.Load())
        assert _resolve_arg_to_path(node, {}) is None

    def test_path_div_string(self):
        # ROOT / "file.json"
        node = ast.BinOp(
            left=ast.Name(id="ROOT", ctx=ast.Load()),
            op=ast.Div(),
            right=ast.Constant(value="file.json"),
        )
        assert _resolve_arg_to_path(node, {}) == "file.json"

    def test_path_div_variable(self):
        # ROOT / filename
        node = ast.BinOp(
            left=ast.Name(id="ROOT", ctx=ast.Load()),
            op=ast.Div(),
            right=ast.Name(id="fname", ctx=ast.Load()),
        )
        string_vars = {"fname": "data.json"}
        assert _resolve_arg_to_path(node, string_vars) == "data.json"


class TestMatchPathToNode:
    def test_exact_match(self):
        known = {"src/utils.py"}
        result = _match_path_to_node("src/utils.py", known, {}, {})
        assert result == "src/utils.py"

    def test_config_prefix_match(self):
        node_index = {"config::settings.json": {}}
        result = _match_path_to_node("settings.json", set(), {}, node_index)
        assert result == "config::settings.json"

    def test_basename_match(self):
        config_paths = {"deploy.json": "config::.claude/deploy.json"}
        result = _match_path_to_node("deploy.json", set(), config_paths, {})
        assert result == "config::.claude/deploy.json"

    def test_no_match(self):
        result = _match_path_to_node("nonexistent.xyz", set(), {}, {})
        assert result is None

    def test_strips_leading_dot_slash(self):
        known = {"src/app.py"}
        result = _match_path_to_node("./src/app.py", known, {}, {})
        assert result == "src/app.py"


class TestGetCallName:
    def test_simple_name(self):
        node = ast.Call(
            func=ast.Name(id="open", ctx=ast.Load()),
            args=[],
            keywords=[],
        )
        assert _get_call_name(node) == "open"

    def test_attribute_name(self):
        node = ast.Call(
            func=ast.Attribute(
                value=ast.Name(id="path", ctx=ast.Load()),
                attr="read_text",
                ctx=ast.Load(),
            ),
            args=[],
            keywords=[],
        )
        assert _get_call_name(node) == "read_text"


class TestIsWriteContext:
    def test_write_mode_positional(self):
        source = 'open(x, "w")'
        tree = ast.parse(source)
        call = tree.body[0].value
        assert _is_write_context(call, source) is True

    def test_append_mode_positional(self):
        source = 'open(x, "a")'
        tree = ast.parse(source)
        call = tree.body[0].value
        assert _is_write_context(call, source) is True

    def test_read_mode_positional(self):
        source = 'open(x, "r")'
        tree = ast.parse(source)
        call = tree.body[0].value
        assert _is_write_context(call, source) is False

    def test_no_mode_default_read(self):
        source = "open(x)"
        tree = ast.parse(source)
        call = tree.body[0].value
        assert _is_write_context(call, source) is False

    def test_write_mode_keyword(self):
        source = 'open(x, mode="wb")'
        tree = ast.parse(source)
        call = tree.body[0].value
        assert _is_write_context(call, source) is True

    def test_read_mode_keyword(self):
        source = 'open(x, mode="rb")'
        tree = ast.parse(source)
        call = tree.body[0].value
        assert _is_write_context(call, source) is False
