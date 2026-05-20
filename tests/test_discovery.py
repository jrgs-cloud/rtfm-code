"""Tests for auto-recursive domain discovery and domain extractor."""

import json
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rtfm.core.discovery import (
    ClassifiedFile,
    DiscoveryResult,
    DomainRule,
    _classify_by_extension,
    _load_config,
    _parse_domain_rules,
    discover,
)
from rtfm.extractors.domain_extractor import (
    _extract_description,
    _extract_references,
    _extract_title,
    _parse_frontmatter,
    extract_domain_file,
    extract_domain_batch,
)


# ---------------------------------------------------------------------------
# Discovery tests
# ---------------------------------------------------------------------------


class TestDomainRule:
    def test_glob_match(self):
        rule = DomainRule(name="skills", node_type="SkillNode", match="*/SKILL.md")
        assert rule.matches("deploy/SKILL.md")
        assert not rule.matches("deploy/README.md")

    def test_filename_match(self):
        rule = DomainRule(name="agents", node_type="AgentNode", match="*.md")
        assert rule.matches("project-guru.md")
        assert rule.matches("deep/nested/agent.md")
        assert not rule.matches("code.py")

    def test_exact_match(self):
        rule = DomainRule(name="config", node_type="ConfigNode", match="settings.json")
        assert rule.matches("settings.json")
        assert not rule.matches("other.json")


class TestParseConfig:
    def test_load_rtfm_json(self, tmp_path):
        config = {"extractors": ["code"], "domains": {"skills": "*/SKILL.md"}}
        (tmp_path / ".rtfm.json").write_text(json.dumps(config))
        result = _load_config(tmp_path)
        assert result == config

    def test_load_missing(self, tmp_path):
        result = _load_config(tmp_path)
        assert result is None

    def test_parse_short_form(self):
        config = {"domains": {"skills": "*/SKILL.md"}}
        rules = _parse_domain_rules(config)
        assert len(rules) == 1
        assert rules[0].name == "skills"
        assert rules[0].node_type == "SkillNode"
        assert rules[0].match == "*/SKILL.md"

    def test_parse_full_form(self):
        config = {"domains": {"agents": {
            "match": "*.md",
            "node_type": "AgentNode",
            "extractor": "domain",
            "metadata": {"runtime": ["ide"]},
        }}}
        rules = _parse_domain_rules(config)
        assert len(rules) == 1
        assert rules[0].node_type == "AgentNode"
        assert rules[0].metadata == {"runtime": ["ide"]}


class TestDiscovery:
    def test_discovers_python_files(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("print('hello')")
        (tmp_path / "src" / "utils.py").write_text("x = 1")

        result = discover(tmp_path)
        py_files = [f for f in result.files if f.extractor == "code"]
        assert len(py_files) == 2

    def test_discovers_with_domain_config(self, tmp_path):
        # Set up config with domain rules
        config = {
            "extractors": ["code", "doc"],
            "domains": {
                "skills": {"match": "*/SKILL.md", "node_type": "SkillNode", "extractor": "domain"},
            },
        }
        (tmp_path / ".rtfm.json").write_text(json.dumps(config))

        # Create skill structure
        (tmp_path / "skills" / "deploy").mkdir(parents=True)
        (tmp_path / "skills" / "deploy" / "SKILL.md").write_text("# Deploy\nDeploys things.")

        result = discover(tmp_path)
        domain_files = [f for f in result.files if f.domain == "skills"]
        assert len(domain_files) == 1
        assert domain_files[0].node_type == "SkillNode"

    def test_heuristic_detection_agents(self, tmp_path):
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "researcher.md").write_text("# Researcher\nDoes research.")
        (tmp_path / "agents" / "engineer.md").write_text("# Engineer\nWrites code.")

        result = discover(tmp_path, use_heuristics=True)
        agent_files = [f for f in result.files if f.domain == "agents"]
        assert len(agent_files) == 2
        assert all(f.node_type == "AgentNode" for f in agent_files)
        assert "agents" in result.domains_found

    def test_heuristic_detection_skills(self, tmp_path):
        (tmp_path / "skills" / "deploy").mkdir(parents=True)
        (tmp_path / "skills" / "deploy" / "SKILL.md").write_text("# Deploy")

        result = discover(tmp_path, use_heuristics=True)
        skill_files = [f for f in result.files if f.domain == "skills"]
        assert len(skill_files) >= 1

    def test_skips_excluded_dirs(self, tmp_path):
        (tmp_path / "node_modules" / "pkg").mkdir(parents=True)
        (tmp_path / "node_modules" / "pkg" / "index.js").write_text("module.exports = {}")
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("print('hi')")

        result = discover(tmp_path)
        paths = [f.rel_path for f in result.files]
        assert not any("node_modules" in p for p in paths)
        assert any("app.py" in p for p in paths)

    def test_nested_override(self, tmp_path):
        # Root config
        root_config = {"extractors": ["code", "doc"]}
        (tmp_path / ".rtfm.json").write_text(json.dumps(root_config))

        # Nested override with domain rules
        (tmp_path / "plugins").mkdir()
        nested_config = {
            "domains": {"plugins": {"match": "*.py", "node_type": "PluginNode", "extractor": "domain"}},
        }
        (tmp_path / "plugins" / ".rtfm.json").write_text(json.dumps(nested_config))
        (tmp_path / "plugins" / "my_plugin.py").write_text("class Plugin: pass")

        result = discover(tmp_path)
        plugin_files = [f for f in result.files if f.domain == "plugins"]
        assert len(plugin_files) == 1
        assert plugin_files[0].node_type == "PluginNode"
        assert "plugins" in result.overrides_applied

    def test_targets_filter(self, tmp_path):
        config = {"targets": ["src"], "extractors": ["code"]}
        (tmp_path / ".rtfm.json").write_text(json.dumps(config))

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("x = 1")
        (tmp_path / "other").mkdir()
        (tmp_path / "other" / "ignored.py").write_text("y = 2")

        result = discover(tmp_path)
        paths = [f.rel_path for f in result.files]
        assert any("src/app.py" in p for p in paths)
        assert not any("other" in p for p in paths)

    def test_no_heuristics_flag(self, tmp_path):
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "bot.md").write_text("# Bot")

        result = discover(tmp_path, use_heuristics=False)
        # Without heuristics, agents/ md files are classified as generic docs
        agent_files = [f for f in result.files if f.domain == "agents"]
        assert len(agent_files) == 0
        doc_files = [f for f in result.files if f.extractor == "doc"]
        assert len(doc_files) == 1


# ---------------------------------------------------------------------------
# Domain extractor tests
# ---------------------------------------------------------------------------


class TestFrontmatter:
    def test_valid_frontmatter(self):
        content = "---\nname: test\nversion: 1.0\n---\n# Hello"
        result = _parse_frontmatter(content)
        assert result == {"name": "test", "version": 1.0}

    def test_no_frontmatter(self):
        assert _parse_frontmatter("# Just a heading") is None

    def test_malformed_yaml(self):
        assert _parse_frontmatter("---\n: bad: [unclosed\n---\n") is None


class TestExtractTitle:
    def test_h1(self):
        assert _extract_title("---\n---\n# My Title\nContent") == "My Title"

    def test_no_h1(self):
        assert _extract_title("No heading here") is None


class TestExtractDescription:
    def test_after_title(self):
        content = "# Title\n\nThis is the description.\n\nMore content."
        desc = _extract_description(content)
        assert "This is the description." in desc

    def test_after_frontmatter(self):
        content = "---\nname: x\n---\n# Title\n\nFirst paragraph here."
        desc = _extract_description(content)
        assert "First paragraph here." in desc


class TestExtractReferences:
    def test_wikilinks(self):
        refs = _extract_references("See [[deploy]] and [[review]]")
        assert "deploy" in refs
        assert "review" in refs

    def test_mentions(self):
        refs = _extract_references("Delegate to @researcher and @engineer")
        assert "researcher" in refs
        assert "engineer" in refs


class TestDomainExtraction:
    def test_extract_skill(self, tmp_path):
        skill_dir = tmp_path / "skills" / "deploy"
        skill_dir.mkdir(parents=True)
        skill_file = skill_dir / "SKILL.md"
        skill_file.write_text(
            "---\nversion: 2.0\n---\n# Deploy Skill\n\n"
            "Deploys the application.\n\n"
            "<description>Handles CDK deployments</description>\n\n"
            "Delegates to @infra for infrastructure.\n"
        )

        result = extract_domain_file(
            file_path=skill_file,
            root=tmp_path,
            domain="skills",
            node_type="SkillNode",
        )

        assert len(result.nodes) == 1
        node = result.nodes[0]
        assert node["id"] == "skill::deploy"
        assert node["node_type"] == "SkillNode"
        assert node["attrs"]["name"] == "Deploy Skill"
        assert "Deploys" in node["attrs"]["description"]

    def test_extract_agent(self, tmp_path):
        agents_dir = tmp_path / "agents"
        agents_dir.mkdir()
        agent_file = agents_dir / "researcher.md"
        agent_file.write_text(
            "---\nmodel: sonnet\nruntime:\n  - ide\n  - sdk\n---\n"
            "# Research Agent\n\nInvestigates unfamiliar tech.\n\n"
            "Delegates to @code-agent for implementation.\n"
        )

        result = extract_domain_file(
            file_path=agent_file,
            root=tmp_path,
            domain="agents",
            node_type="AgentNode",
        )

        assert len(result.nodes) == 1
        node = result.nodes[0]
        assert node["id"] == "agent::researcher"
        assert node["node_type"] == "AgentNode"
        assert node["attrs"]["model"] == "sonnet"
        assert "code-agent" in node["attrs"].get("delegates_to", [])

    def test_extract_batch(self, tmp_path):
        (tmp_path / "rules").mkdir()
        (tmp_path / "rules" / "CORE.md").write_text("# Core Rules\n\nYou MUST follow these.")
        (tmp_path / "rules" / "SAFETY.md").write_text("# Safety\n\nYou SHOULD be careful.")

        from rtfm.core.discovery import ClassifiedFile

        files = [
            ClassifiedFile(
                path=tmp_path / "rules" / "CORE.md",
                rel_path="rules/CORE.md",
                domain="rules",
                node_type="RuleNode",
                extractor="domain",
            ),
            ClassifiedFile(
                path=tmp_path / "rules" / "SAFETY.md",
                rel_path="rules/SAFETY.md",
                domain="rules",
                node_type="RuleNode",
                extractor="domain",
            ),
        ]

        result = extract_domain_batch(files, tmp_path)
        assert len(result.nodes) == 2
        # Check enforcement detection
        core_node = next(n for n in result.nodes if n["id"] == "rule::CORE")
        assert core_node["attrs"]["enforcement"] == "hard"
        safety_node = next(n for n in result.nodes if n["id"] == "rule::SAFETY")
        assert safety_node["attrs"]["enforcement"] == "soft"


class TestEndToEnd:
    """Integration test: discovery → domain extraction → node output."""

    def test_full_pipeline(self, tmp_path):
        # Build a mini project structure
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "app.py").write_text("def main(): pass")
        (tmp_path / "agents").mkdir()
        (tmp_path / "agents" / "bot.md").write_text("---\nmodel: haiku\n---\n# Bot\nA simple bot.")
        (tmp_path / "skills" / "search").mkdir(parents=True)
        (tmp_path / "skills" / "search" / "SKILL.md").write_text("# Search\nSearches things.")
        (tmp_path / "rules").mkdir()
        (tmp_path / "rules" / "SAFETY.md").write_text("# Safety\nYou MUST be safe.")
        (tmp_path / "README.md").write_text("# My Project\nA project.")

        # Run discovery with heuristics
        result = discover(tmp_path, use_heuristics=True)

        # Should find files across multiple domains
        domains = set(f.domain for f in result.files)
        assert "code" in domains  # app.py
        assert "agents" in domains  # bot.md
        assert "rules" in domains  # SAFETY.md

        # Domain files should be routed to domain extractor
        domain_files = [f for f in result.files if f.extractor == "domain"]
        assert len(domain_files) >= 2  # at least agents + rules

        # Run domain extraction on classified files
        batch_result = extract_domain_batch(domain_files, tmp_path)
        assert len(batch_result.nodes) >= 2

        # Verify node types are correct
        node_types = {n["node_type"] for n in batch_result.nodes}
        assert "AgentNode" in node_types
        assert "RuleNode" in node_types
