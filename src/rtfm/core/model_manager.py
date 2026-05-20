"""Model management for semantic search embeddings."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

from .concurrency import adaptive_threads

logger = logging.getLogger(__name__)

DEFAULT_MODEL_NAME = "BAAI/bge-small-en-v1.5"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "rtfm" / "models"
DEFAULT_EMBED_THREADS = adaptive_threads()


class ModelUnavailableError(RuntimeError):
    """Raised when semantic operations are attempted without an available model."""


class ModelManager:
    """Manages the fastembed text embedding model lifecycle.

    Handles model loading, caching, and availability detection.
    Supports CODE_GRAPH_MODEL_PATH and CODE_GRAPH_CACHE_DIR env vars
    for custom model locations. Falls back to BAAI/bge-small-en-v1.5.
    """

    def __init__(self) -> None:
        self._model = None
        self._model_name: str | None = None
        self._available: bool = False
        self._error_message: str | None = None
        self._load()

    @property
    def available(self) -> bool:
        return self._available

    @property
    def error_message(self) -> str | None:
        return self._error_message

    @property
    def model_name(self) -> str | None:
        return self._model_name

    def _resolve_model_path(self) -> str:
        env_path = os.environ.get("CODE_GRAPH_MODEL_PATH")
        if env_path:
            if not Path(env_path).exists():
                raise FileNotFoundError(
                    f"CODE_GRAPH_MODEL_PATH={env_path} does not exist"
                )
            return env_path
        return DEFAULT_MODEL_NAME

    def _resolve_cache_dir(self) -> str:
        env_cache = os.environ.get("CODE_GRAPH_CACHE_DIR")
        if env_cache:
            return env_cache
        return str(DEFAULT_CACHE_DIR)

    def _resolve_threads(self) -> int:
        """Resolve ONNX thread count from env or default to cpu_count."""
        env_threads = os.environ.get("RTFM_EMBED_THREADS")
        if env_threads:
            return max(1, int(env_threads))
        return DEFAULT_EMBED_THREADS

    def _load(self) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError:
            self._available = False
            self._error_message = (
                "fastembed not installed — run: pip install rtfm[semantic]"
            )
            logger.warning("[rtfm] %s", self._error_message)
            return

        model_path = None
        try:
            model_path = self._resolve_model_path()
            cache_dir = self._resolve_cache_dir()
            Path(cache_dir).mkdir(parents=True, exist_ok=True)
            self._model = TextEmbedding(
                model_name=model_path, cache_dir=cache_dir,
                threads=self._resolve_threads(),
            )
            self._model_name = model_path
            self._available = True
            self._error_message = None
        except FileNotFoundError as e:
            self._available = False
            self._error_message = str(e)
            logger.warning("[rtfm] model unavailable: %s", e)
        except Exception as e:
            self._available = False
            self._error_message = (
                f"Failed to load model '{model_path or DEFAULT_MODEL_NAME}': {e}"
            )
            logger.warning("[rtfm] %s", self._error_message)

    def reload_model(self) -> bool:
        self._model = None
        self._model_name = None
        self._available = False
        self._error_message = None
        self._load()
        return self._available

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not self._available or self._model is None:
            raise ModelUnavailableError(
                "Semantic model is not available. "
                "Install rtfm[semantic] and ensure model is accessible."
            )
        return [vec.tolist() for vec in self._model.embed(texts)]
