"""Tests for discovery.py — file classification and domain routing."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rtfm.core.discovery import (
    DomainRule,
    ClassifiedFile,
    DiscoveryResult,
    _parse_gitignore,
    _classify_by_extension,
    _parse_domain_rules,
    discover,
    EXT_TO_EXTRACTOR,
    SKIP_DIRS_DEFAULT,
)


@pytest.fixture
def project_root(tmp_path):
    return tmp_path


class TestParseGitignore:
    def test_no_gitignore_returns_defaults(self, project_root):
        result = _parse_gitignore(project_root)
        assert result == SKIP_DIRS_DEFAULT

    def test_parses_simple_directory_names(self, project_root):
        (project_root / ".gitignore").write_text("node_modules\n__pycache__\ndist\n")
        result = _parse_gitignore(project_root)
        assert "node_modules" in result
        assert "__pycache__" in result
        assert "dist" in result

    def test_strips_trailing_slashes(self, project_root):
        (project_root / ".gitignore").write_text("build/\n.venv/\n")
        result = _parse_gitignore(project_root)
        assert "build" in result
        assert ".venv" in result

    def test_skips_comments_and_empty_lines(self, project_root):
        (project_root / ".gitignore").write_text("# comment\n\nnode_modules\n")
        result = _parse_gitignore(project_root)
        assert "node_modules" in result
        assert "#" not in str(result)

    def test_skips_negation_patterns(self, project_root):
        (project_root / ".gitignore").write_text("dist\n!dist/keep\n")
        result = _parse_gitignore(project_root)
        assert "dist" in result

    def test_always_includes_git(self, project_root):
        (project_root / ".gitignore").write_text("node_modules\n")
        result = _parse_gitignore(project_root)
        assert ".git" in result

    def test_path_based_pattern_extracts_leaf(self, project_root):
        (project_root / ".gitignore").write_text("infrastructure/aws/cdk.out/\n")
        result = _parse_gitignore(project_root)
        assert "cdk.out" in result


class TestClassifyByExtension:
    def test_python_file(self):
        result = _classify_by_extension(Path("src/app.py"), "src/app.py")
        assert result.extractor == "code"
        assert result.domain == "code"

    def test_typescript_file(self):
        result = _classify_by_extension(Path("src/app.ts"), "src/app.ts")
        assert result.extractor == "typescript"

    def test_markdown_file(self):
        result = _classify_by_extension(Path("docs/README.md"), "docs/README.md")
        assert result.extractor == "doc"

    def test_yaml_file(self):
        result = _classify_by_extension(Path("config.yaml"), "config.yaml")
        assert result.extractor == "config"

    def test_unknown_extension_gets_fallback(self):
        result = _classify_by_extension(Path("image.png"), "image.png")
        # Unknown extensions get a fallback classification, not None
        assert result is not None
        assert isinstance(result, ClassifiedFile)


class TestDomainRule:
    def test_simple_glob_match(self):
        rule = DomainRule(name="skills", node_type="SkillNode", match="*.md", root="skills/")
        assert rule.matches("SKILL.md")
        assert not rule.matches("app.py")

    def test_recursive_glob_match(self):
        rule = DomainRule(name="docs", node_type="DocNode", match="**/*.md")
        assert rule.matches("deep/nested/file.md")
        assert rule.matches("README.md")

    def test_no_match(self):
        rule = DomainRule(name="code", node_type="ModuleNode", match="*.py")
        assert not rule.matches("app.ts")


class TestParseDomainRules:
    def test_parses_domains_config(self):
        config = {
            "domains": {
                "services": {
                    "match": "*.yaml",
                    "node_type": "ServiceNode",
                    "extractor": "domain",
                    "root": "services",
                },
            },
        }
        rules = _parse_domain_rules(config)
        assert len(rules) == 1
        assert rules[0].name == "services"
        assert rules[0].node_type == "ServiceNode"
        assert rules[0].root == "services"

    def test_empty_config_returns_empty(self):
        rules = _parse_domain_rules({})
        assert rules == []


class TestDiscover:
    def test_discovers_python_files(self, project_root):
        (project_root / "src").mkdir()
        (project_root / "src" / "app.py").write_text("def main(): pass")
        (project_root / "src" / "__init__.py").write_text("")

        result = discover(project_root)
        py_files = [f for f in result.files if f.extractor == "code"]
        assert len(py_files) >= 1
        paths = [f.rel_path for f in py_files]
        assert "src/app.py" in paths

    def test_skips_gitignore_dirs(self, project_root):
        (project_root / ".gitignore").write_text("node_modules\n")
        (project_root / "node_modules").mkdir()
        (project_root / "node_modules" / "pkg.js").write_text("")
        (project_root / "src").mkdir()
        (project_root / "src" / "app.py").write_text("")

        result = discover(project_root)
        paths = [f.rel_path for f in result.files]
        assert not any("node_modules" in p for p in paths)

    def test_discovers_typescript_files(self, project_root):
        (project_root / "src").mkdir()
        (project_root / "src" / "app.ts").write_text("export function main() {}")

        result = discover(project_root)
        ts_files = [f for f in result.files if f.extractor == "typescript"]
        assert len(ts_files) >= 1

    def test_discovers_config_files(self, project_root):
        (project_root / "config.yaml").write_text("key: value")

        result = discover(project_root)
        config_files = [f for f in result.files if f.extractor == "config"]
        assert len(config_files) >= 1

    def test_empty_directory(self, project_root):
        result = discover(project_root)
        assert isinstance(result, DiscoveryResult)
        assert result.files == []

    def test_with_rtfm_config(self, project_root):
        (project_root / ".rtfm.json").write_text(json.dumps({
            "domains": {
                "agents": {
                    "match": "*.md",
                    "node_type": "AgentNode",
                    "extractor": "domain",
                    "root": "agents",
                },
            },
        }))
        (project_root / "agents").mkdir()
        (project_root / "agents" / "bot.md").write_text("# Bot Agent")

        result = discover(project_root)
        agent_files = [f for f in result.files if f.domain == "agents"]
        assert len(agent_files) >= 1
