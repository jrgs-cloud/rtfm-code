"""LanceDB vector store for semantic code search."""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .model_manager import ModelManager, ModelUnavailableError

try:
    import lancedb
    _LANCEDB_AVAILABLE = True
except ImportError:
    _LANCEDB_AVAILABLE = False

DEFAULT_EMBEDDING_MODEL = "BAAI/bge-small-en-v1.5"
DEFAULT_EMBEDDING_DIM = 384
DEFAULT_TABLE_NAME = "chunks"
BATCH_SIZE = 64
MAX_RETRIES = 5
INITIAL_BACKOFF_S = 1.0

_model_manager: ModelManager | None = None
_model_lock = threading.Lock()


def _get_model_manager() -> ModelManager:
    global _model_manager
    if _model_manager is None:
        with _model_lock:
            if _model_manager is None:
                _model_manager = ModelManager()
    return _model_manager


def _semantic_unavailable_error() -> dict:
    return {
        "error": "model_unavailable",
        "message": "Install rtfm[semantic] and ensure model is available",
    }


@dataclass
class SearchResult:
    node_id: str
    source_file: str
    node_type: str
    score: float
    chunk_preview: str


def _embed_batch_with_retry(texts: list[str]) -> list[list[float]]:
    mgr = _get_model_manager()
    backoff = INITIAL_BACKOFF_S

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return mgr.embed(texts)
        except ModelUnavailableError:
            raise
        except Exception as e:
            if attempt == MAX_RETRIES:
                raise RuntimeError(
                    f"Embedding failed after {MAX_RETRIES} retries: {e}"
                ) from e
            print(
                f"[rtfm] embed attempt {attempt}/{MAX_RETRIES} failed: {e}, "
                f"retrying in {backoff:.0f}s",
                file=sys.stderr,
            )
            time.sleep(backoff)
            backoff *= 2

    return []  # unreachable


def _embed_texts(texts: list[str], verbose: bool = False) -> list[list[float]]:
    all_vectors: list[list[float]] = []
    total = len(texts)

    for i in range(0, total, BATCH_SIZE):
        batch = texts[i:i + BATCH_SIZE]
        vectors = _embed_batch_with_retry(batch)
        all_vectors.extend(vectors)
        if verbose and total > BATCH_SIZE:
            done = min(i + BATCH_SIZE, total)
            print(f"[rtfm] embedded {done}/{total} chunks", file=sys.stderr)

    return all_vectors


def is_semantic_available() -> bool:
    """Check if semantic search dependencies (lancedb + fastembed) are available."""
    return _LANCEDB_AVAILABLE and _get_model_manager().available


def create_index(
    chunks: list[dict],
    index_path: Path,
    table_name: str = DEFAULT_TABLE_NAME,
) -> int | dict:
    """Create or overwrite a LanceDB vector index from text chunks.

    Args:
        chunks: List of dicts with node_id, source_file, node_type, content, start_line, end_line.
        index_path: Directory path for the LanceDB database.
        table_name: Table name within the database.

    Returns:
        Number of records indexed, or error dict if semantic is unavailable.

    Raises:
        ValueError: If chunks list is empty.
    """
    if not _LANCEDB_AVAILABLE:
        return _semantic_unavailable_error()

    mgr = _get_model_manager()
    if not mgr.available:
        return _semantic_unavailable_error()

    if not chunks:
        raise ValueError("No chunks provided for indexing")

    contents = [c["content"] for c in chunks]
    vectors = _embed_texts(contents)

    records = []
    for chunk, vector in zip(chunks, vectors):
        records.append({
            "node_id": chunk["node_id"],
            "source_file": chunk["source_file"],
            "node_type": chunk["node_type"],
            "content": chunk["content"],
            "start_line": chunk["start_line"],
            "end_line": chunk["end_line"],
            "vector": vector,
        })

    index_path.mkdir(parents=True, exist_ok=True)
    db = lancedb.connect(str(index_path))
    db.create_table(table_name, records, mode="overwrite")
    return len(records)


def update_index(
    chunks: list[dict],
    index_path: Path,
    source_files: list[str],
    table_name: str = DEFAULT_TABLE_NAME,
) -> int | dict:
    """Update index: delete rows matching source_files, insert new chunks.

    This enables incremental re-indexing — only changed files are re-embedded
    and their old rows replaced.

    Args:
        chunks: New chunks to insert (already chunked from changed files).
        index_path: Path to the LanceDB database directory.
        source_files: List of source_file values whose old rows should be deleted.
        table_name: Table name within the database.

    Returns:
        Number of new records inserted, or error dict if semantic is unavailable.
    """
    if not _LANCEDB_AVAILABLE:
        return _semantic_unavailable_error()

    mgr = _get_model_manager()
    if not mgr.available:
        return _semantic_unavailable_error()

    if not index_exists(index_path, table_name):
        # No existing index — fall through to create if we have chunks
        if chunks:
            return create_index(chunks, index_path, table_name)
        return 0

    db = lancedb.connect(str(index_path))
    table = db.open_table(table_name)

    # Delete old rows for the changed source files
    for sf in source_files:
        try:
            table.delete(f'source_file = "{sf}"')
        except Exception:
            pass  # Row may not exist — that's fine

    if not chunks:
        return 0

    # Embed and insert new chunks
    contents = [c["content"] for c in chunks]
    vectors = _embed_texts(contents)

    records = []
    for chunk, vector in zip(chunks, vectors):
        records.append({
            "node_id": chunk["node_id"],
            "source_file": chunk["source_file"],
            "node_type": chunk["node_type"],
            "content": chunk["content"],
            "start_line": chunk["start_line"],
            "end_line": chunk["end_line"],
            "vector": vector,
        })

    table.add(records)
    return len(records)


def search(
    query: str,
    index_path: Path,
    top_k: int = 10,
    threshold: float | None = None,
    table_name: str = DEFAULT_TABLE_NAME,
) -> list[SearchResult] | dict:
    """Search the vector index for semantically similar code chunks.

    Args:
        query: Natural language search query.
        index_path: Path to the LanceDB database directory.
        top_k: Maximum number of results to return.
        threshold: Optional minimum cosine similarity score (0-1).
        table_name: Table name within the database.

    Returns:
        List of SearchResult dicts sorted by similarity, or error dict if unavailable.
    """
    if not _LANCEDB_AVAILABLE or not _get_model_manager().available:
        return _semantic_unavailable_error()

    if not index_exists(index_path, table_name):
        return []

    query_vector = _embed_texts([query])[0]
    db = lancedb.connect(str(index_path))
    table = db.open_table(table_name)

    arrow_table = table.search(query_vector).limit(top_k).to_arrow()

    results: list[SearchResult] = []
    for i in range(arrow_table.num_rows):
        score = float(1 - arrow_table.column("_distance")[i].as_py())
        if threshold is not None and score < threshold:
            continue
        results.append(SearchResult(
            node_id=arrow_table.column("node_id")[i].as_py(),
            source_file=arrow_table.column("source_file")[i].as_py(),
            node_type=arrow_table.column("node_type")[i].as_py(),
            score=score,
            chunk_preview=arrow_table.column("content")[i].as_py()[:200],
        ))

    results.sort(key=lambda r: r.score, reverse=True)
    return results


def index_exists(index_path: Path, table_name: str = DEFAULT_TABLE_NAME) -> bool:
    """Check if a LanceDB vector index exists at the given path.

    Args:
        index_path: Path to the LanceDB database directory.
        table_name: Table name to check for.

    Returns:
        True if the index and table exist, False otherwise.
    """
    if not _LANCEDB_AVAILABLE:
        return False
    try:
        db = lancedb.connect(str(index_path))
        tables = db.list_tables()
        # lancedb >= 0.4 returns ListTablesResponse object, not a plain list
        table_list = tables.tables if hasattr(tables, 'tables') else list(tables)
        return table_name in table_list
    except Exception:
        return False


def get_index_stats(
    index_path: Path,
    table_name: str = DEFAULT_TABLE_NAME,
) -> dict:
    """Get statistics about a vector index (row count, schema).

    Args:
        index_path: Path to the LanceDB database directory.
        table_name: Table name to query stats for.

    Returns:
        Dict with count, schema info, or error dict if index doesn't exist.
    """
    if not _LANCEDB_AVAILABLE:
        return _semantic_unavailable_error()

    if not index_exists(index_path, table_name):
        raise FileNotFoundError(f"No index found at {index_path}")

    db = lancedb.connect(str(index_path))
    table = db.open_table(table_name)
    row_count = table.count_rows()

    index_dir = Path(index_path)
    last_modified = max(
        (f.stat().st_mtime for f in index_dir.rglob("*") if f.is_file()),
        default=0.0,
    )

    mgr = _get_model_manager()
    return {
        "chunk_count": row_count,
        "last_built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(last_modified)),
        "index_path": str(index_path),
        "embedding_model": mgr.model_name or DEFAULT_EMBEDDING_MODEL,
        "semantic_available": mgr.available,
    }
