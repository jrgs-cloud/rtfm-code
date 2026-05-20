"""Domain extractor — produces typed nodes from domain-classified files.

This is the generic extractor that handles files classified by the
discovery module into specific domains (skills, agents, rules, etc.).
It parses YAML frontmatter, markdown structure, and file metadata to
produce appropriately-typed nodes without needing a separate extractor
per domain.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

import yaml

from rtfm.core.types import (
    ET_CROSS_REFERENCES,
    ExtractionResult,
    make_edge,
    make_node,
)

# Domain-specific node type constants
NT_SKILL = "SkillNode"
NT_AGENT = "AgentNode"
NT_RULE = "RuleNode"
NT_MEMORY = "MemoryNode"
NT_ETHOS = "EthosNode"
NT_PROJECT = "ProjectNode"
NT_INFRA = "InfraNode"
NT_GATEWAY = "GatewayTargetNode"
NT_DECISION = "DecisionNode"
NT_SYSTEM = "SystemNode"


def _parse_frontmatter(content: str) -> dict[str, Any] | None:
    """Extract YAML frontmatter from markdown content.

    Falls back to lenient line-by-line parsing when yaml.safe_load fails
    (common with unquoted colons in description fields).
    """
    if not content.startswith("---"):
        return None
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        result = yaml.safe_load(parts[1])
        if isinstance(result, dict):
            return result
        return None
    except yaml.YAMLError:
        # Lenient fallback: regex-based key:value parsing
        result = _parse_frontmatter_lenient(parts[1])
        return result if result else None


def _parse_frontmatter_lenient(raw: str) -> dict[str, Any]:
    """Lenient frontmatter parser for files with unquoted special chars."""
    fm: dict[str, Any] = {}
    current_key: str | None = None
    list_items: list[str] = []

    for line in raw.splitlines():
        # YAML list item under current key
        list_match = re.match(r"^\s+-\s+(.+)", line)
        if list_match and current_key:
            list_items.append(list_match.group(1).strip())
            fm[current_key] = list_items[:]
            continue

        # New key: value pair (only split on first colon)
        kv_match = re.match(r"^(\w[\w-]*)\s*:\s*(.*)", line)
        if kv_match:
            current_key = kv_match.group(1)
            value = kv_match.group(2).strip()
            list_items = []
            if value:
                # Comma-separated → list
                if "," in value and not value.startswith('"'):
                    fm[current_key] = [v.strip() for v in value.split(",") if v.strip()]
                else:
                    fm[current_key] = value.strip('"').strip("'")
            else:
                fm[current_key] = ""

    return fm


def _extract_title(content: str) -> str | None:
    """Extract first H1 heading from markdown."""
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def _extract_description(content: str) -> str:
    """Extract first paragraph after frontmatter/title as description."""
    lines = content.splitlines()
    in_frontmatter = False
    past_title = False
    desc_lines: list[str] = []

    for line in lines:
        if line.strip() == "---":
            in_frontmatter = not in_frontmatter
            continue
        if in_frontmatter:
            continue
        if line.startswith("# ") and not past_title:
            past_title = True
            continue
        if not past_title and line.strip():
            past_title = True

        if past_title:
            if line.strip() == "" and desc_lines:
                break
            if line.strip():
                desc_lines.append(line.strip())

    return " ".join(desc_lines)[:300] if desc_lines else ""


def _extract_references(content: str) -> list[str]:
    """Extract wikilinks and markdown links as references."""
    refs: list[str] = []
    # Wikilinks: [[target]]
    refs.extend(re.findall(r'\[\[([^\]]+)\]\]', content))
    # Markdown links: [text](target)
    refs.extend(re.findall(r'\[([^\]]*)\]\(([^)]+)\)', content))
    # @mentions
    refs.extend(re.findall(r'@([\w-]+)', content))
    return refs


def _extract_tags(content: str, frontmatter: dict | None) -> list[str]:
    """Extract tags from frontmatter or inline hashtags."""
    tags: list[str] = []
    if frontmatter:
        fm_tags = frontmatter.get("tags", [])
        if isinstance(fm_tags, list):
            tags.extend(fm_tags)
        elif isinstance(fm_tags, str):
            tags.extend(fm_tags.split(","))
    # Inline hashtags
    tags.extend(re.findall(r'(?:^|\s)#([\w-]+)', content))
    return list(set(tags))


def extract_domain_file(
    file_path: Path,
    root: Path,
    domain: str,
    node_type: str,
    metadata: dict[str, Any] | None = None,
) -> ExtractionResult:
    """Extract a single domain-classified file into typed nodes.

    Args:
        file_path: Absolute path to the file.
        root: Project root for relative path computation.
        domain: Domain name (e.g. "skills", "agents").
        node_type: Target node type string.
        metadata: Additional metadata from domain rule config.

    Returns:
        ExtractionResult with nodes and edges.
    """
    result = ExtractionResult()

    try:
        content = file_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result

    rel_path = str(file_path.relative_to(root))
    source_file = str(file_path)

    # Parse structure
    frontmatter = _parse_frontmatter(content)
    title = _extract_title(content) or file_path.stem
    description = _extract_description(content)
    references = _extract_references(content)
    tags = _extract_tags(content, frontmatter)

    # Build node ID from domain and path
    # e.g. "skill::deploy" or "agent::project-guru"
    _SINGULAR = {"skills": "skill", "agents": "agent", "rules": "rule",
                 "memories": "memory", "projects": "project", "ethos": "ethos"}
    id_prefix = _SINGULAR.get(domain, domain.removesuffix("s"))
    id_name = file_path.stem
    # For SKILL.md files, use parent dir name
    if file_path.name == "SKILL.md":
        id_name = file_path.parent.name
    node_id = f"{id_prefix}::{id_name}"

    # Build attrs
    attrs: dict[str, Any] = {
        "name": title,
        "description": description,
        "domain": domain,
        "rel_path": rel_path,
    }
    if tags:
        attrs["tags"] = tags
    if frontmatter:
        # Include all frontmatter fields — enables edge_rules to scan any custom attr
        for key, value in frontmatter.items():
            if key not in attrs and value is not None:
                attrs[key] = value
    if metadata:
        attrs["domain_metadata"] = metadata

    # Domain-specific attribute extraction
    if node_type == NT_SKILL:
        attrs.update(_extract_skill_attrs(content, frontmatter))
    elif node_type == NT_AGENT:
        attrs.update(_extract_agent_attrs(content, frontmatter))
    elif node_type == NT_RULE:
        attrs.update(_extract_rule_attrs(content, frontmatter))

    node = make_node(
        id=node_id,
        node_type=node_type,
        source_file=source_file,
        **attrs,
    )
    result.nodes.append(node)

    # Build edges from references
    for ref in references:
        if isinstance(ref, tuple):
            # Markdown link: (text, target)
            ref_name = ref[0] or ref[1]
        else:
            ref_name = ref

        # Try to resolve reference to a node ID
        target_id = _resolve_reference(ref_name, domain)
        if target_id and target_id != node_id:
            result.edges.append(make_edge(
                source=node_id,
                target=target_id,
                edge_type=ET_CROSS_REFERENCES,
            ))

    return result


def _resolve_reference(ref: str, source_domain: str) -> str | None:
    """Attempt to resolve a reference string to a node ID."""
    ref = ref.strip()
    if not ref:
        return None

    # Already looks like a node ID
    if "::" in ref:
        return ref

    # @mention → agent
    if ref.startswith("@"):
        return f"agent::{ref[1:]}"

    # Relative path references
    if "/" in ref or ref.endswith(".md"):
        # Extract meaningful name from path
        name = Path(ref).stem
        if name == "SKILL":
            name = Path(ref).parent.name
        # Guess domain from path
        if "skill" in ref.lower():
            return f"skill::{name}"
        if "agent" in ref.lower():
            return f"agent::{name}"
        if "rule" in ref.lower():
            return f"rule::{name}"
        return None

    # Simple name — assume same domain
    prefix = source_domain.rstrip("s")
    return f"{prefix}::{ref}"


def _extract_skill_attrs(content: str, _frontmatter: dict | None) -> dict[str, Any]:
    """Extract skill-specific attributes."""
    attrs: dict[str, Any] = {}

    # Extract XML tags common in SKILL.md files
    for tag in ("description", "constraints", "output-format"):
        match = re.search(rf'<{tag}>(.*?)</{tag}>', content, re.DOTALL)
        if match:
            attrs[f"skill_{tag.replace('-', '_')}"] = match.group(1).strip()[:500]

    # Extract commands/triggers
    commands = re.findall(r'/(\w[\w-]*)', content)
    if commands:
        attrs["commands"] = list(set(commands))[:10]

    return attrs


def _extract_agent_attrs(content: str, frontmatter: dict | None) -> dict[str, Any]:
    """Extract agent-specific attributes."""
    attrs: dict[str, Any] = {}

    if frontmatter:
        for key in ("model", "runtime", "tools", "skills"):
            if key in frontmatter:
                attrs[key] = frontmatter[key]

    # Detect delegations
    delegations = re.findall(r'@([\w-]+)', content)
    if delegations:
        attrs["delegates_to"] = list(set(delegations))

    return attrs


def _extract_rule_attrs(content: str, _frontmatter: dict | None) -> dict[str, Any]:
    """Extract rule-specific attributes."""
    attrs: dict[str, Any] = {}

    # Detect severity/enforcement level
    if "MUST" in content or "NEVER" in content:
        attrs["enforcement"] = "hard"
    elif "SHOULD" in content:
        attrs["enforcement"] = "soft"

    # Count constraints
    constraints = re.findall(r'(?:^|\n)\s*[-*]\s+', content)
    attrs["constraint_count"] = len(constraints)

    return attrs


def extract_domain_batch(
    classified_files: list[Any],  # list[ClassifiedFile] — avoid circular import
    root: Path,
    config: dict[str, Any] | None = None,
) -> ExtractionResult:
    """Extract a batch of domain-classified files.

    This is the entry point called by the build pipeline for all files
    routed to the "domain" extractor.

    After extracting all files, applies:
    - edge_rules: content-driven edges (scan @mentions, /commands, frontmatter attrs)
    - topology: declaration-driven system nodes and connections
    """
    result = ExtractionResult()
    processed = 0
    skipped = 0

    for cf in classified_files:
        file_result = extract_domain_file(
            file_path=cf.path,
            root=root,
            domain=cf.domain,
            node_type=cf.node_type,
        )
        if file_result.nodes:
            result.nodes.extend(file_result.nodes)
            result.edges.extend(file_result.edges)
            processed += 1
        else:
            skipped += 1

    # Smart defaults: emit edges from what we already parsed
    unconfigured = _emit_smart_edges(result, config or {})

    # Extendable: apply user-defined edge rules and topology from config
    if config:
        _apply_edge_rules(result, config.get("edge_rules", []))
        _apply_topology(result, config.get("topology", {}))

    # Detect unlinked tags — fields with repeated values that could be relationships
    detected_tags = _detect_tags(result, config or {})

    # Surface prompts to stderr for orchestrator
    import json as _json
    if unconfigured:
        print(
            f"[domain_extractor] unconfigured_domains: {_json.dumps(unconfigured)}",
            file=sys.stderr,
        )
    if detected_tags:
        print(
            f"[domain_extractor] detected_tags: {_json.dumps(detected_tags)}",
            file=sys.stderr,
        )

    print(
        f"[domain_extractor] processed {processed} domain files, skipped {skipped}",
        file=sys.stderr,
    )
    return result


# ---------------------------------------------------------------------------
# Tag detection — surfaces unlinked repeated fields as relationship prompts
# ---------------------------------------------------------------------------

# Fields that are structural/internal — never prompt about these
_IGNORE_FIELDS = frozenset({
    "id", "node_type", "source_file", "rel_path", "domain", "checksum",
    "cluster_id", "last_updated", "name", "description", "title",
    "content_preview", "constraint_count", "enforcement",
})


def _detect_tags(result: ExtractionResult, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Detect frontmatter fields with repeated values that could be relationships.

    Scans all domain nodes for fields that:
    - Appear on 3+ nodes
    - Have a small set of distinct values (2-20)
    - Don't already have an auto_edge or edge_rule covering them
    - Aren't structural/internal fields

    Returns prompts for the orchestrator.
    """
    # Collect fields already covered by auto_edges
    covered_fields: set[str] = set()
    defaults = _DEFAULT_AUTO_EDGES
    domains_config = config.get("domains", {})

    for node_type_rules in defaults.values():
        for pattern in node_type_rules:
            parts = pattern.split(":")
            if parts[0] == "attr" and len(parts) >= 2:
                covered_fields.add(parts[1])

    for domain_spec in domains_config.values():
        if isinstance(domain_spec, dict):
            for pattern in domain_spec.get("auto_edges", []):
                parts = pattern.split(":")
                if parts[0] == "attr" and len(parts) >= 2:
                    covered_fields.add(parts[1])

    # Also skip if topology already exists
    has_topology = bool(config.get("topology", {}).get("nodes"))

    # Scan all domain nodes for repeated field values
    field_stats: dict[str, dict[str, Any]] = {}

    for node in result.nodes:
        node_type = node.get("node_type", "")
        for key, value in node.items():
            if key in _IGNORE_FIELDS or key in covered_fields:
                continue

            # Normalize to list of string values
            values: list[str] = []
            if isinstance(value, list):
                values = [str(v).strip() for v in value if v]
            elif isinstance(value, str) and value.strip():
                # Skip long strings (descriptions, content)
                if len(value) > 100:
                    continue
                values = [value.strip()]
            else:
                continue

            if not values:
                continue

            if key not in field_stats:
                field_stats[key] = {
                    "field": key,
                    "values": set(),
                    "appears_on": 0,
                    "node_types": set(),
                }
            field_stats[key]["values"].update(values)
            field_stats[key]["appears_on"] += 1
            field_stats[key]["node_types"].add(node_type)

    # Filter to fields that look like relationship candidates
    detected: list[dict[str, Any]] = []
    for key, stats in field_stats.items():
        values = stats["values"]
        appears_on = stats["appears_on"]
        node_types = stats["node_types"]

        # Must appear on 3+ nodes with 2-20 distinct values
        # and values must repeat (ratio of values to appearances < 0.5)
        if appears_on < 3 or len(values) < 2 or len(values) > 20:
            continue
        if len(values) / appears_on > 0.5:
            continue  # Too many unique values — not a tag, just data

        # Skip if it's the runtime field and topology already exists
        if key == "runtime" and has_topology:
            continue

        detected.append({
            "field": key,
            "values": sorted(values),
            "appears_on": appears_on,
            "node_types": sorted(node_types),
            "prompt": f"Field '{key}' with values {sorted(values)} appears on {appears_on} nodes. What's the relationship?",
        })

    return detected


# ---------------------------------------------------------------------------
# Smart defaults — config-driven with sensible fallbacks
# ---------------------------------------------------------------------------


def _load_defaults() -> dict[str, list[str]]:
    """Load default auto_edges from shipped defaults.json."""
    import json
    defaults_path = Path(__file__).parent.parent / "defaults.json"
    try:
        data = json.loads(defaults_path.read_text(encoding="utf-8"))
        return data.get("auto_edges", {})
    except (OSError, json.JSONDecodeError):
        return {}


_DEFAULT_AUTO_EDGES: dict[str, list[str]] = _load_defaults()


def _emit_smart_edges(result: ExtractionResult, config: dict[str, Any]) -> list[dict[str, Any]]:
    """Emit edges based on auto_edges config per domain.

    Each domain can declare auto_edges in .rtfm.json:
      "agents": {
        "auto_edges": ["@mentions:delegates_to:agents", "attr:delegates_to:delegates_to:agents"]
      }

    Pattern format: "<scan>:<edge_type>:<target_domain>[:<direction>]"
      - scan: "@mentions", "/commands", "attr:<field>"
      - edge_type: the edge type string to emit
      - target_domain: which domain's nodes are valid targets
      - direction: "outbound" (default) or "inbound"

    If auto_edges is not specified, falls back to _DEFAULT_AUTO_EDGES by node_type.
    Set "auto_edges": [] to disable smart edges for a domain.

    Returns list of unconfigured domain hints for the orchestrator.
    """
    domains_config = config.get("domains", {})

    # Build target lookups: domain_name → set of node short names
    domain_nodes: dict[str, set[str]] = {}
    for n in result.nodes:
        domain = n.get("domain", "") or n.get("attrs", {}).get("domain", "")
        if domain:
            short_name = n["id"].split("::")[-1]
            domain_nodes.setdefault(domain, set()).add(short_name)

    content_cache: dict[str, str] = {}
    unconfigured: dict[str, dict[str, Any]] = {}

    for node in result.nodes:
        node_id = node["id"]
        node_type = node.get("node_type", "")
        domain = node.get("domain", "") or node.get("attrs", {}).get("domain", "")
        attrs = node.get("attrs", node)  # nodes are stored flat
        self_name = node_id.split("::")[-1]

        # Resolve auto_edges: config > defaults > empty
        domain_spec = domains_config.get(domain, {})
        if isinstance(domain_spec, dict) and "auto_edges" in domain_spec:
            auto_edges = domain_spec["auto_edges"]
        else:
            auto_edges = _DEFAULT_AUTO_EDGES.get(node_type, [])

        # Track domains with no auto_edges at all
        if not auto_edges and domain and domain not in unconfigured:
            source_file = node.get("source_file", "")
            unconfigured[domain] = {
                "domain": domain,
                "node_type": node_type,
                "count": len(domain_nodes.get(domain, set())),
                "sample_file": source_file,
                "prompt": f"Found {len(domain_nodes.get(domain, set()))} {node_type} files in '{domain}' with no auto_edges configured. What relationships should be extracted?",
            }

        for pattern in auto_edges:
            _apply_auto_edge(
                node, node_id, self_name, attrs, pattern,
                domain_nodes, content_cache, result,
            )

    return list(unconfigured.values())


def _apply_auto_edge(
    node: dict[str, Any],
    node_id: str,
    self_name: str,
    attrs: dict[str, Any],
    pattern: str,
    domain_nodes: dict[str, set[str]],
    content_cache: dict[str, str],
    result: ExtractionResult,
) -> None:
    """Apply a single auto_edge pattern to a node."""
    parts = pattern.split(":")
    if len(parts) < 3:
        return

    # Handle compound scan types: "attr:fieldname" uses first two parts
    if parts[0] == "attr" and len(parts) >= 4:
        scan = f"attr:{parts[1]}"
        edge_type = parts[2]
        target_domain = parts[3]
        direction = parts[4] if len(parts) > 4 else "outbound"
    else:
        scan = parts[0]
        edge_type = parts[1]
        target_domain = parts[2]
        direction = parts[3] if len(parts) > 3 else "outbound"

    # Resolve target prefix from domain name
    _SINGULAR = {"skills": "skill", "agents": "agent", "rules": "rule",
                 "memories": "memory", "projects": "project", "ethos": "ethos"}
    target_prefix = _SINGULAR.get(target_domain, target_domain.rstrip("s")) + "::"

    valid_targets = domain_nodes.get(target_domain, set())
    targets: list[str] = []

    if scan == "@mentions":
        content = _get_node_content(node, content_cache)
        if content:
            mentions = re.findall(r"@([\w][\w-]*)", content)
            targets = [m for m in dict.fromkeys(mentions) if m != self_name and m in valid_targets]

    elif scan == "/commands":
        content = _get_node_content(node, content_cache)
        if content:
            commands = re.findall(r"(?<!/)/([a-zA-Z][\w-]*)", content)
            targets = [c for c in dict.fromkeys(commands) if c != self_name and c in valid_targets]

    elif scan == "paths":
        # Extract backtick-quoted file paths from content and match against node IDs
        from fnmatch import fnmatch
        content = _get_node_content(node, content_cache)
        if content:
            paths = re.findall(r'`([^`]+\.\w+)`', content)
            all_node_ids = {n["id"] for n in result.nodes if n.get("node_type") == "ModuleNode"}
            for p in dict.fromkeys(paths):
                # Direct match or glob match
                for nid in all_node_ids:
                    if nid == p or fnmatch(nid, p):
                        if direction == "inbound":
                            result.edges.append(make_edge(source=nid, target=node_id, edge_type=edge_type))
                        else:
                            result.edges.append(make_edge(source=node_id, target=nid, edge_type=edge_type))
            return  # paths handles its own edge emission

    elif scan.startswith("attr:"):
        attr_name = scan[5:]
        value = attrs.get(attr_name, "")
        if isinstance(value, list):
            targets = [str(v).strip().strip("@`") for v in value if str(v).strip().strip("@`") in valid_targets]
        elif isinstance(value, str) and value:
            items = re.split(r"[,;]\s*", value)
            targets = [i.strip().strip("@`") for i in items if i.strip().strip("@`") in valid_targets]

    # Emit edges
    for target_name in targets:
        if target_name == self_name:
            continue
        target_id = f"{target_prefix}{target_name}"
        if direction == "inbound":
            result.edges.append(make_edge(source=target_id, target=node_id, edge_type=edge_type))
        else:
            result.edges.append(make_edge(source=node_id, target=target_id, edge_type=edge_type))


def _get_node_content(node: dict[str, Any], cache: dict[str, str]) -> str:
    """Load file content for a node, with caching."""
    source_file = node.get("source_file", "")
    if not source_file:
        return ""
    if source_file not in cache:
        try:
            cache[source_file] = Path(source_file).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            cache[source_file] = ""
    return cache[source_file]


# ---------------------------------------------------------------------------
# Post-merge edges — runs after all extractors merge, sees full node set
# ---------------------------------------------------------------------------


def apply_post_merge_edges(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply cross-domain edge rules that need the full merged node set.

    Called after Phase 5 merge — can see ModuleNodes, FunctionNodes, etc.
    Currently handles the 'paths' scan type which matches backtick-quoted
    file paths in domain files against code module node IDs.
    """
    from fnmatch import fnmatch

    new_edges: list[dict[str, Any]] = []
    defaults = _DEFAULT_AUTO_EDGES
    domains_config = config.get("domains", {})

    # Build module ID set for path matching
    module_ids = {n["id"] for n in nodes if n.get("node_type") == "ModuleNode"}

    # Only process domain nodes that have 'paths' in their auto_edges
    content_cache: dict[str, str] = {}

    for node in nodes:
        node_type = node.get("node_type", "")
        domain = node.get("domain", "")
        node_id = node.get("id", "")

        # Resolve auto_edges for this node
        domain_spec = domains_config.get(domain, {})
        if isinstance(domain_spec, dict) and "auto_edges" in domain_spec:
            auto_edges = domain_spec["auto_edges"]
        else:
            auto_edges = defaults.get(node_type, [])

        for pattern in auto_edges:
            parts = pattern.split(":")
            if parts[0] != "paths":
                continue

            # Parse: paths:<edge_type>:<ignored_domain>[:<direction>]
            edge_type = parts[1] if len(parts) > 1 else "governs"
            direction = parts[3] if len(parts) > 3 else "outbound"

            # Read content and extract backtick paths
            source_file = node.get("source_file", "")
            if not source_file:
                continue
            if source_file not in content_cache:
                try:
                    content_cache[source_file] = Path(source_file).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    content_cache[source_file] = ""
            content = content_cache[source_file]
            if not content:
                continue

            paths = re.findall(r'`([^`]+\.\w+)`', content)
            for p in dict.fromkeys(paths):
                for mid in module_ids:
                    if mid == p or fnmatch(mid, p):
                        if direction == "inbound":
                            new_edges.append(make_edge(source=mid, target=node_id, edge_type=edge_type))
                        else:
                            new_edges.append(make_edge(source=node_id, target=mid, edge_type=edge_type))

    # --- File reference detection (reads/writes) ---
    # Scan code files for string literals that match known node IDs
    all_target_ids = module_ids | {n["id"] for n in nodes if n.get("node_type") == "ConfigNode"}
    # Build filename → node_id lookup for bare name matching
    filename_to_id: dict[str, str] = {}
    for tid in all_target_ids:
        # Strip prefix (config::) and get basename
        clean = tid.replace("config::", "")
        basename = clean.rsplit("/", 1)[-1] if "/" in clean else clean
        if basename and basename not in filename_to_id:
            filename_to_id[basename] = tid
        # Also map the full relative path without prefix
        if clean != basename:
            filename_to_id[clean] = tid

    code_nodes = [n for n in nodes if n.get("node_type") == "ModuleNode"]

    for node in code_nodes:
        source_file = node.get("source_file", "")
        node_id = node.get("id", "")
        if not source_file:
            continue
        if source_file not in content_cache:
            try:
                content_cache[source_file] = Path(source_file).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                content_cache[source_file] = ""
        content = content_cache[source_file]
        if not content:
            continue

        # Find string literals that match project file paths
        seen: set[str] = set()
        for match in re.finditer(r'["\']([^"\']{3,80})["\']', content):
            ref = match.group(1)
            if ref in seen:
                continue
            # Check if it matches a known node (exact, prefixed, or by filename)
            target_id = None
            if ref in all_target_ids:
                target_id = ref
            elif f"config::{ref}" in all_target_ids:
                target_id = f"config::{ref}"
            elif ref in filename_to_id:
                target_id = filename_to_id[ref]

            if not target_id or target_id == node_id:
                continue
            seen.add(ref)

            # Classify as read or write based on context
            start = max(0, match.start() - 100)
            end = min(len(content), match.end() + 100)
            context = content[start:end]
            is_write = bool(re.search(r'write_text|write_bytes|"w"|\'w\'|json\.dump|yaml\.dump|\.write\(', context))

            new_edges.append(make_edge(
                source=node_id,
                target=target_id,
                edge_type="writes" if is_write else "reads",
            ))

    return new_edges


# ---------------------------------------------------------------------------
# Config-driven edge rules — user-defined relationship extraction
# ---------------------------------------------------------------------------


def _apply_edge_rules(result: ExtractionResult, rules: list[dict[str, Any]]) -> None:
    """Apply edge_rules from config to all extracted nodes.

    Each rule scans node content or attributes and emits edges.
    Supported scan types:
      - "@mentions": regex @name in node content → edge to target_prefix::name
      - "/commands": regex /name in node content → edge to target_prefix::name
      - "attr:<field>": read frontmatter field → edge per value

    Rule format:
      {
        "source_domain": "agents",       # optional — filter to nodes from this domain
        "scan": "@mentions",             # what to look for
        "edge_type": "delegates_to",     # edge type to emit
        "target_prefix": "agent::",      # how to form target node ID
        "direction": "outbound"          # outbound (default) or inbound
      }
    """
    if not rules:
        return

    # Build lookup of existing node IDs for target validation
    node_ids = {n["id"] for n in result.nodes}
    # Build domain lookup: node_id → domain
    node_domains = {n["id"]: n.get("domain", "") for n in result.nodes}
    # Build content cache: node_id → source content (lazy loaded)
    node_content: dict[str, str] = {}

    for rule in rules:
        source_domain = rule.get("source_domain")
        scan = rule.get("scan", "")
        edge_type = rule.get("edge_type", "relates_to")
        target_prefix = rule.get("target_prefix", "")
        direction = rule.get("direction", "outbound")

        for node in result.nodes:
            # Filter by source domain if specified
            if source_domain and node_domains.get(node["id"]) != source_domain:
                continue

            targets = _resolve_scan_targets(node, scan, node_content)

            for target_name in targets:
                target_id = f"{target_prefix}{target_name}"

                # Only emit edge if target exists in graph (or skip validation for topology targets)
                if target_id not in node_ids:
                    continue

                if direction == "inbound":
                    result.edges.append(make_edge(
                        source=target_id,
                        target=node["id"],
                        edge_type=edge_type,
                    ))
                else:
                    result.edges.append(make_edge(
                        source=node["id"],
                        target=target_id,
                        edge_type=edge_type,
                    ))


def _resolve_scan_targets(
    node: dict[str, Any], scan: str, content_cache: dict[str, str]
) -> list[str]:
    """Resolve scan targets from a node based on scan type."""
    if scan == "@mentions":
        content = _get_node_content(node, content_cache)
        self_name = node.get("id", "").split("::")[-1]
        mentions = re.findall(r"@([\w][\w-]*)", content)
        return [m for m in dict.fromkeys(mentions) if m != self_name]

    elif scan == "/commands":
        content = _get_node_content(node, content_cache)
        self_name = node.get("id", "").split("::")[-1]
        commands = re.findall(r"(?<!/)/([a-zA-Z][\w-]*)", content)
        return [c for c in dict.fromkeys(commands) if c != self_name]

    elif scan.startswith("attr:"):
        attr_name = scan[5:]
        attrs = node.get("attrs", node)
        value = attrs.get(attr_name, "")
        if isinstance(value, list):
            return [str(v).strip().strip("@`") for v in value if v]
        elif isinstance(value, str) and value:
            items = re.split(r"[,;]\s*", value)
            return [i.strip().strip("@`") for i in items if i.strip()]

    return []


# ---------------------------------------------------------------------------
# Config-driven topology — declaration-driven system connections
# ---------------------------------------------------------------------------


def _apply_topology(result: ExtractionResult, topology: dict[str, Any]) -> None:
    """Apply topology declarations from config.

    Creates synthetic nodes and edges that represent architectural facts
    not derivable from file content alone.

    Topology format:
      {
        "nodes": [
          {"id": "system::ide-agent", "type": "SystemNode", "name": "Dice"}
        ],
        "edges": [
          {"source": "system::ide-agent", "target": "system::sdk-agent", "type": "delegates_remote"}
        ],
        "fan_out": [
          {"source": "system::ide-agent", "target_domain": "skills", "edge_type": "executes_skill"}
        ]
      }
    """
    if not topology:
        return

    # Create declared nodes
    for node_decl in topology.get("nodes", []):
        node_id = node_decl.get("id", "")
        if not node_id:
            continue
        result.nodes.append(make_node(
            id=node_id,
            node_type=node_decl.get("type", "SystemNode"),
            source_file="",
            name=node_decl.get("name", node_id.split("::")[-1]),
            **{k: v for k, v in node_decl.items() if k not in ("id", "type", "name")},
        ))

    # Create declared edges
    for edge_decl in topology.get("edges", []):
        source = edge_decl.get("source", "")
        target = edge_decl.get("target", "")
        edge_type = edge_decl.get("type", "connects_to")
        if source and target:
            result.edges.append(make_edge(
                source=source,
                target=target,
                edge_type=edge_type,
            ))

    # Fan-out: for every node in target_domain, create edge from source
    node_domains = {n.get("domain", ""): [] for n in result.nodes}
    for n in result.nodes:
        domain = n.get("domain", "") or n.get("attrs", {}).get("domain", "")
        if domain:
            node_domains.setdefault(domain, []).append(n["id"])

    for fan in topology.get("fan_out", []):
        source = fan.get("source", "")
        target_domain = fan.get("target_domain", "")
        edge_type = fan.get("edge_type", "connects_to")
        if not source or not target_domain:
            continue
        for target_id in node_domains.get(target_domain, []):
            result.edges.append(make_edge(
                source=source,
                target=target_id,
                edge_type=edge_type,
            ))
