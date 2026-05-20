"""Auto-recursive domain discovery from folder structure.

Walks a root directory, matches files to domains using:
1. Explicit config rules from .rtfm.json `domains` section
2. Nested .rtfm.json overrides at any depth
3. Heuristic detection for common patterns when no config exists

Returns a classified file map that the build pipeline uses to route
files to the correct extractor with the correct node type.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SKIP_DIRS_DEFAULT = frozenset({
    "__pycache__", "node_modules", ".venv", "dist", ".git",
    ".hg", ".svn", "cdk.out", ".mypy_cache", ".pytest_cache",
    ".tox", "build", "egg-info",
})


def _parse_gitignore(root: Path) -> frozenset[str]:
    """Parse .gitignore at root and extract directory skip patterns.

    Returns directory names that should be skipped during walk.
    Falls back to SKIP_DIRS_DEFAULT if no .gitignore exists.
    """
    gitignore = root / ".gitignore"
    if not gitignore.is_file():
        return SKIP_DIRS_DEFAULT

    skip_dirs: set[str] = set()
    try:
        for line in gitignore.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            # Negation patterns — skip
            if line.startswith("!"):
                continue
            # Strip trailing slashes
            clean = line.rstrip("/")
            # Extract the directory name from patterns like:
            # "node_modules", "__pycache__/", ".venv", "build/", "dist/"
            # Skip complex patterns with wildcards or paths
            if "/" in clean and not clean.endswith("*"):
                # Path-based pattern like "infrastructure/aws/cdk.out/"
                # Extract the leaf directory
                parts = clean.split("/")
                leaf = parts[-1] if parts[-1] else parts[-2] if len(parts) > 1 else ""
                if leaf and "*" not in leaf:
                    skip_dirs.add(leaf)
            elif "*" not in clean and clean:
                # Simple directory name like "node_modules", ".venv"
                skip_dirs.add(clean)
    except OSError:
        return SKIP_DIRS_DEFAULT

    # Always skip .git
    skip_dirs.add(".git")
    return frozenset(skip_dirs) if skip_dirs else SKIP_DIRS_DEFAULT

# File extension → base extractor mapping
EXT_TO_EXTRACTOR: dict[str, str] = {
    ".py": "code",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "code",
    ".jsx": "code",
    ".json": "config",
    ".yaml": "config",
    ".yml": "config",
    ".toml": "config",
    ".md": "doc",
    ".rst": "doc",
}


@dataclass
class DomainRule:
    """A rule that maps files to a domain with a specific node type."""

    name: str
    node_type: str
    match: str  # glob pattern relative to the domain root
    extractor: str = "domain"  # which extractor handles these files
    root: str = ""  # relative path from project root where this domain lives
    metadata: dict[str, Any] = field(default_factory=dict)

    def matches(self, rel_path: str) -> bool:
        """Check if a relative path matches this rule's glob pattern.

        Supports ** for recursive directory matching (fnmatch doesn't natively).
        """
        from fnmatch import fnmatch

        if "**/" in self.match:
            # Split on **/ — prefix must match start, suffix matches any file below
            prefix, suffix = self.match.split("**/", 1)
            if prefix and not rel_path.startswith(prefix):
                return False
            remainder = rel_path[len(prefix):] if prefix else rel_path
            # Match suffix against filename (handles nested dirs)
            return fnmatch(Path(remainder).name, suffix) or fnmatch(remainder, suffix)

        return fnmatch(rel_path, self.match) or fnmatch(Path(rel_path).name, self.match)


@dataclass
class ClassifiedFile:
    """A file classified into a domain with routing info."""

    path: Path
    rel_path: str  # relative to project root
    domain: str  # domain name (e.g. "skills", "agents", "code")
    node_type: str  # target node type (e.g. "SkillNode", "FunctionNode")
    extractor: str  # which extractor to use


@dataclass
class DiscoveryResult:
    """Result of recursive discovery — classified files grouped by extractor."""

    files: list[ClassifiedFile] = field(default_factory=list)
    domains_found: list[str] = field(default_factory=list)
    overrides_applied: list[str] = field(default_factory=list)

    def by_extractor(self) -> dict[str, list[ClassifiedFile]]:
        """Group classified files by their target extractor."""
        groups: dict[str, list[ClassifiedFile]] = {}
        for f in self.files:
            groups.setdefault(f.extractor, []).append(f)
        return groups

    def by_domain(self) -> dict[str, list[ClassifiedFile]]:
        """Group classified files by domain name."""
        groups: dict[str, list[ClassifiedFile]] = {}
        for f in self.files:
            groups.setdefault(f.domain, []).append(f)
        return groups


# ---------------------------------------------------------------------------
# Heuristic detection rules — applied when no explicit domain config exists
# ---------------------------------------------------------------------------

HEURISTICS: list[dict[str, Any]] = [
    {
        "detect": lambda d, _: (d / "SKILL.md").exists(),
        "domain": "skills",
        "node_type": "SkillNode",
        "match": "SKILL.md",
        "extractor": "domain",
    },
    {
        "detect": lambda d, _: d.name == "skills" and any(
            (d / child / "SKILL.md").exists() for child in d.iterdir() if child.is_dir()
        ),
        "domain": "skills",
        "node_type": "SkillNode",
        "match": "*/SKILL.md",
        "extractor": "domain",
    },
    {
        "detect": lambda d, _files: d.name == "agents" and any(f.suffix == ".md" for f in _files),
        "domain": "agents",
        "node_type": "AgentNode",
        "match": "*.md",
        "extractor": "domain",
    },
    {
        "detect": lambda d, _files: d.name == "rules" and any(f.suffix == ".md" for f in _files),
        "domain": "rules",
        "node_type": "RuleNode",
        "match": "*.md",
        "extractor": "domain",
    },
    {
        "detect": lambda d, _files: d.name == "memory" and any(f.suffix == ".md" for f in _files),
        "domain": "memories",
        "node_type": "MemoryNode",
        "match": "*.md",
        "extractor": "domain",
    },
    {
        "detect": lambda d, _files: d.name == "ethos" and any(f.suffix == ".md" for f in _files),
        "domain": "ethos",
        "node_type": "EthosNode",
        "match": "*.md",
        "extractor": "domain",
    },
    {
        "detect": lambda d, _files: d.name in ("infra", "infrastructure") and any(f.suffix == ".py" for f in _files),
        "domain": "infra",
        "node_type": "InfraNode",
        "match": "**/*.py",
        "extractor": "code",
    },
    {
        "detect": lambda d, _files: d.name == "projects" and any(
            f.name in ("workflow.json", "SPEC.md") for f in _files
        ),
        "domain": "projects",
        "node_type": "ProjectNode",
        "match": "*/SPEC.md",
        "extractor": "domain",
    },
]


def _load_config(path: Path) -> dict | None:
    """Load .rtfm.json from a directory, or None if not present."""
    config_file = path / ".rtfm.json"
    if config_file.exists():
        try:
            return json.loads(config_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
    return None


def _parse_domain_rules(config: dict, config_root: str = "") -> list[DomainRule]:
    """Parse domain rules from a config dict's `domains` section."""
    rules: list[DomainRule] = []
    domains = config.get("domains", {})
    for name, spec in domains.items():
        if isinstance(spec, str):
            # Short form: "skills": "*/SKILL.md"
            rules.append(DomainRule(
                name=name,
                node_type=f"{name.rstrip('s').capitalize()}Node",
                match=spec,
                root=config_root,
            ))
        elif isinstance(spec, dict):
            rules.append(DomainRule(
                name=name,
                node_type=spec.get("node_type", f"{name.rstrip('s').capitalize()}Node"),
                match=spec.get("match", "*"),
                extractor=spec.get("extractor", "domain"),
                root=spec.get("root", config_root),
                metadata=spec.get("metadata", {}),
            ))
    return rules


def _classify_by_extension(file: Path, rel_path: str) -> ClassifiedFile:
    """Fallback classification based on file extension."""
    ext = file.suffix.lower()
    extractor = EXT_TO_EXTRACTOR.get(ext, "doc")

    # Map extractor to a generic node type
    type_map = {
        "code": "ModuleNode",
        "typescript": "ModuleNode",
        "config": "ConfigNode",
        "doc": "DocNode",
    }
    node_type = type_map.get(extractor, "DocNode")

    return ClassifiedFile(
        path=file,
        rel_path=rel_path,
        domain="code" if extractor in ("code", "typescript") else extractor,
        node_type=node_type,
        extractor=extractor,
    )


def discover(
    root: Path,
    config: dict | None = None,
    skip_dirs: frozenset[str] | None = None,
    use_heuristics: bool = True,
) -> DiscoveryResult:
    """Recursively discover and classify all files from root.

    Args:
        root: Project root directory to scan.
        config: Top-level .rtfm.json config. If None, attempts to load from root.
        skip_dirs: Directory names to skip. Defaults to SKIP_DIRS_DEFAULT.
        use_heuristics: Whether to apply heuristic domain detection.

    Returns:
        DiscoveryResult with all classified files.
    """
    root = root.resolve()
    if skip_dirs is None:
        skip_dirs = _parse_gitignore(root)

    if config is None:
        config = _load_config(root) or {}

    # Merge skip_dirs from config (if any still specified)
    config_skips = config.get("skip_dirs", [])
    effective_skips = skip_dirs | frozenset(config_skips)

    # Parse top-level domain rules
    domain_rules = _parse_domain_rules(config)

    # Track which domains have explicit targets in config
    explicit_targets = config.get("targets", None)

    result = DiscoveryResult()
    result.domains_found = [r.name for r in domain_rules]

    # Walk the tree
    _walk_recursive(
        directory=root,
        root=root,
        domain_rules=domain_rules,
        skip_dirs=effective_skips,
        use_heuristics=use_heuristics,
        result=result,
        depth=0,
        explicit_targets=explicit_targets,
    )

    return result


def _walk_recursive(
    directory: Path,
    root: Path,
    domain_rules: list[DomainRule],
    skip_dirs: frozenset[str],
    use_heuristics: bool,
    result: DiscoveryResult,
    depth: int,
    explicit_targets: list[str] | None = None,

    max_depth: int = 50,
) -> None:
    """Recursively walk directory tree, classifying files."""
    if depth > max_depth:
        return

    local_rules = list(domain_rules)

    # Check for nested .rtfm.json override in this directory
    if depth > 0:
        nested_config = _load_config(directory)
        if nested_config:
            config_root = str(directory.relative_to(root))
            nested_rules = _parse_domain_rules(nested_config, config_root=config_root)
            if nested_rules:
                local_rules = nested_rules + local_rules
                result.overrides_applied.append(str(directory.relative_to(root)))
                for r in nested_rules:
                    if r.name not in result.domains_found:
                        result.domains_found.append(r.name)

    # Collect direct children
    try:
        children = sorted(directory.iterdir())
    except PermissionError:
        return

    files = [c for c in children if c.is_file()]
    dirs = [c for c in children if c.is_dir()]

    # Apply heuristics at this directory level
    heuristic_rules: list[DomainRule] = []
    if use_heuristics:
        for h in HEURISTICS:
            try:
                if h["detect"](directory, files):
                    rule = DomainRule(
                        name=h["domain"],
                        node_type=h["node_type"],
                        match=h["match"],
                        extractor=h["extractor"],
                        root=str(directory.relative_to(root)),
                    )
                    heuristic_rules.append(rule)
                    if h["domain"] not in result.domains_found:
                        result.domains_found.append(h["domain"])
            except (OSError, StopIteration):
                continue

    # Classify files in this directory
    # Domain rules take precedence over heuristics
    active_rules = local_rules + heuristic_rules
    for file in files:
        if file.name.startswith(".") and file.suffix not in (".md", ".json", ".yaml", ".yml"):
            continue

        rel_path = str(file.relative_to(root))

        # Check if within explicit targets (if set)
        if explicit_targets is not None:
            in_target = any(
                rel_path.startswith(t.rstrip("/")) or t == "."
                for t in explicit_targets
            )
            if not in_target:
                continue

        # Try domain rules first
        classified = False
        for rule in active_rules:
            # Build the path relative to the rule's root for matching
            if rule.root:
                rule_root = root / rule.root
                if not str(file).startswith(str(rule_root)):
                    continue
                match_path = str(file.relative_to(rule_root))
            else:
                match_path = rel_path

            if rule.matches(match_path):
                result.files.append(ClassifiedFile(
                    path=file,
                    rel_path=rel_path,
                    domain=rule.name,
                    node_type=rule.node_type,
                    extractor=rule.extractor,
                ))
                classified = True
                break

        # Fallback to extension-based classification
        if not classified:
            ext = file.suffix.lower()
            if ext in EXT_TO_EXTRACTOR:
                result.files.append(_classify_by_extension(file, rel_path))

    # Determine dot-directories referenced by domain rules (don't skip those)
    domain_dot_dirs: set[str] = set()
    for rule in local_rules:
        if rule.match.startswith("."):
            domain_dot_dirs.add(rule.match.split("/")[0])
        if rule.root.startswith("."):
            domain_dot_dirs.add(rule.root.split("/")[0])

    # Recurse into subdirectories
    for d in dirs:
        if d.name in skip_dirs:
            continue
        if d.name.startswith(".") and d.name not in domain_dot_dirs:
            continue
        _walk_recursive(
            directory=d,
            root=root,
            domain_rules=local_rules,
            skip_dirs=skip_dirs,
            use_heuristics=use_heuristics,
            result=result,
            depth=depth + 1,
            explicit_targets=explicit_targets,

        )
