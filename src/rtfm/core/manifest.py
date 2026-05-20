"""Build cache manifest — skip rebuild when nothing changed.

Stores file checksums in status/.rtfm-manifest.json.
On next build-all, compares current files against manifest:
- All match → "fresh" (no work needed)
- Some changed → returns list of changed files for partial rebuild
- No manifest → full rebuild (first run)
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


MANIFEST_FILENAME = ".rtfm-manifest.json"


def _hash_file(path: Path) -> str:
    """Fast file hash (first 8KB + size for speed on large files)."""
    try:
        stat = path.stat()
        size = stat.st_size
        with open(path, "rb") as f:
            head = f.read(8192)
        return hashlib.sha256(head + str(size).encode()).hexdigest()[:16]
    except OSError:
        return ""


def compute_manifest(files: list[Path]) -> dict[str, str]:
    """Compute checksums for a list of files."""
    return {str(f): _hash_file(f) for f in files if f.exists()}


def load_manifest(state_dir: Path) -> dict[str, str] | None:
    """Load existing manifest from state dir. Returns None if not found."""
    manifest_path = state_dir / MANIFEST_FILENAME
    if not manifest_path.exists():
        return None
    try:
        return json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def save_manifest(state_dir: Path, manifest: dict[str, str]) -> None:
    """Save manifest to state dir."""
    state_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = state_dir / MANIFEST_FILENAME
    manifest_path.write_text(json.dumps(manifest, indent=2))


def check_freshness(
    state_dir: Path,
    current_files: list[Path],
) -> tuple[str, list[Path]]:
    """Check if the graph is fresh, stale, or missing.

    Returns:
        ("fresh", []) — nothing changed, skip rebuild
        ("stale", [changed_files]) — some files changed, partial rebuild possible
        ("missing", [all_files]) — no manifest, full rebuild needed
    """
    old_manifest = load_manifest(state_dir)
    if old_manifest is None:
        return ("missing", current_files)

    current_manifest = compute_manifest(current_files)

    # Check for changes
    changed: list[Path] = []

    # Files that changed or are new
    for file_str, checksum in current_manifest.items():
        if file_str not in old_manifest or old_manifest[file_str] != checksum:
            changed.append(Path(file_str))

    # Files that were deleted (in old but not current)
    deleted = set(old_manifest.keys()) - set(current_manifest.keys())

    if not changed and not deleted:
        return ("fresh", [])

    return ("stale", changed)
