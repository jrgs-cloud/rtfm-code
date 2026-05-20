"""Tests for doc extractor cross_references edge extraction."""
import pytest
from pathlib import Path

from rtfm.extractors.doc_extractor import extract


class TestExtractReferences:
    def test_relative_link(self, tmp_path):
        doc = tmp_path / "docs" / "guide.md"
        doc.parent.mkdir(parents=True)
        doc.write_text("# Guide\n\nSee [other](./other.md) for details.\n")
        result = extract(doc, tmp_path, {})
        xrefs = [e for e in result.edges if e["edge_type"] == "cross_references"]
        assert len(xrefs) == 1
        assert "other.md" in xrefs[0]["target"]

    def test_url_skipped(self, tmp_path):
        doc = tmp_path / "readme.md"
        doc.write_text("# Readme\n\n[link](https://example.com)\n")
        result = extract(doc, tmp_path, {})
        xrefs = [e for e in result.edges if e["edge_type"] == "cross_references"]
        assert len(xrefs) == 0

    def test_anchor_skipped(self, tmp_path):
        doc = tmp_path / "readme.md"
        doc.write_text("# Readme\n\n[section](#overview)\n")
        result = extract(doc, tmp_path, {})
        xrefs = [e for e in result.edges if e["edge_type"] == "cross_references"]
        assert len(xrefs) == 0

    def test_anchor_stripped_from_file_link(self, tmp_path):
        doc = tmp_path / "readme.md"
        doc.write_text("# Readme\n\n[section](other.md#heading)\n")
        result = extract(doc, tmp_path, {})
        xrefs = [e for e in result.edges if e["edge_type"] == "cross_references"]
        assert len(xrefs) == 1
        assert "#" not in xrefs[0]["target"]

    def test_mailto_skipped(self, tmp_path):
        doc = tmp_path / "readme.md"
        doc.write_text("# Readme\n\n[email](mailto:x@y.com)\n")
        result = extract(doc, tmp_path, {})
        xrefs = [e for e in result.edges if e["edge_type"] == "cross_references"]
        assert len(xrefs) == 0

    def test_multiple_links(self, tmp_path):
        doc = tmp_path / "readme.md"
        doc.write_text("# Readme\n\n[a](a.md) and [b](b.md) and [c](c.md)\n")
        result = extract(doc, tmp_path, {})
        xrefs = [e for e in result.edges if e["edge_type"] == "cross_references"]
        assert len(xrefs) == 3


class TestRefToNodeId:
    def test_python_file_bare_path(self, tmp_path):
        doc = tmp_path / "readme.md"
        doc.write_text("# Readme\n\n[code](src/app.py)\n")
        result = extract(doc, tmp_path, {})
        xrefs = [e for e in result.edges if e["edge_type"] == "cross_references"]
        assert len(xrefs) == 1
        # .py files get bare path (no prefix)
        assert xrefs[0]["target"] == "src/app.py"

    def test_json_file_config_prefix(self, tmp_path):
        doc = tmp_path / "readme.md"
        doc.write_text("# Readme\n\n[config](settings.json)\n")
        result = extract(doc, tmp_path, {})
        xrefs = [e for e in result.edges if e["edge_type"] == "cross_references"]
        assert len(xrefs) == 1
        assert xrefs[0]["target"] == "config::settings.json"

    def test_yaml_file_config_prefix(self, tmp_path):
        doc = tmp_path / "readme.md"
        doc.write_text("# Readme\n\n[config](deploy.yaml)\n")
        result = extract(doc, tmp_path, {})
        xrefs = [e for e in result.edges if e["edge_type"] == "cross_references"]
        assert len(xrefs) == 1
        assert xrefs[0]["target"] == "config::deploy.yaml"

    def test_toml_file_config_prefix(self, tmp_path):
        doc = tmp_path / "readme.md"
        doc.write_text("# Readme\n\n[config](pyproject.toml)\n")
        result = extract(doc, tmp_path, {})
        xrefs = [e for e in result.edges if e["edge_type"] == "cross_references"]
        assert len(xrefs) == 1
        assert xrefs[0]["target"] == "config::pyproject.toml"

    def test_md_file_doc_prefix(self, tmp_path):
        doc = tmp_path / "readme.md"
        doc.write_text("# Readme\n\n[guide](guide.md)\n")
        result = extract(doc, tmp_path, {})
        xrefs = [e for e in result.edges if e["edge_type"] == "cross_references"]
        assert len(xrefs) == 1
        assert xrefs[0]["target"] == "doc::guide.md"
