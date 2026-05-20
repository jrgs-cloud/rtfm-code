"""Auto-detection of .rtfm.json scope configuration."""

import json
from pathlib import Path


def detect_scope(start_dir: Path) -> dict | None:
    """Walk up from start_dir looking for .rtfm.json. Returns config or None."""
    current = start_dir.resolve()
    while current != current.parent:
        config_file = current / ".rtfm.json"
        if config_file.exists():
            try:
                return json.loads(config_file.read_text())
            except (json.JSONDecodeError, OSError):
                return None
        current = current.parent
    return None


def get_effective_scope(explicit_scope: str | None, cwd: Path) -> str | None:
    """Return explicit scope, or auto-detected scope from .rtfm.json, or None."""
    if explicit_scope:
        return explicit_scope
    config = detect_scope(cwd)
    if config and "scope" in config:
        return config["scope"]
    return None
