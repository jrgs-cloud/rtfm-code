"""Adaptive concurrency — shared logic for embed threads and Jedi workers.

Detects physical cores, checks system load, and picks optimal concurrency.
Both ONNX embedding and Jedi multiprocessing use this module.

Override with env vars:
    RTFM_EMBED_THREADS=N  — pin embed threads
    RTFM_WORKERS=N        — pin Jedi workers
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def _detect_physical_cores() -> int:
    """Detect physical core count from /proc/cpuinfo.

    Falls back to logical // 2 on non-Linux or parse failure.
    """
    logical = os.cpu_count() or 1
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.strip().startswith("cpu cores"):
                    cores = int(line.split(":")[1].strip())
                    if cores > 0:
                        return cores
    except (OSError, ValueError, IndexError):
        pass
    # Fallback: assume hyperthreading (2 threads per core)
    return max(logical // 2, 1)


def _is_system_idle(physical_cores: int) -> bool:
    """Check if system load is below 30% of physical cores."""
    try:
        load_1min = os.getloadavg()[0]
        return load_1min < (physical_cores * 0.3)
    except (OSError, AttributeError):
        # Windows: no getloadavg — assume not idle (conservative)
        return False


def adaptive_threads() -> int:
    """Optimal ONNX thread count for embedding.

    Benchmarked on bge-small-en (384-dim, 33MB):
        threads=1: 494ms  2: 348ms  4: 268ms  6: 259ms
        8: 234ms  9: 318ms  10: 243ms  12: 1253ms (cliff)

    Sweet spot: physical_cores + 2 (maps to 8 on 6-core/12-thread).
    Beyond that, ONNX thread coordination overhead dominates.

    Returns:
        Thread count. Logs the decision at DEBUG level.
    """
    env = os.environ.get("RTFM_EMBED_THREADS")
    if env:
        pinned = max(1, int(env))
        logger.debug("[concurrency] embed threads pinned: %d (RTFM_EMBED_THREADS)", pinned)
        return pinned

    logical = os.cpu_count() or 1
    physical = _detect_physical_cores()
    idle = _is_system_idle(physical)

    if idle:
        # Burst: physical + 2 (benchmarked sweet spot)
        threads = min(physical + 2, logical - 2)
    else:
        # Contended: physical cores only
        threads = physical

    threads = max(threads, 1)
    logger.debug(
        "[concurrency] embed threads adaptive: %d (physical=%d, logical=%d, idle=%s)",
        threads, physical, logical, idle,
    )
    return threads


def adaptive_workers() -> int:
    """Optimal multiprocessing worker count for Jedi enrichment.

    Benchmarked on 6-core/12-thread machine (402 files):
        workers=6: 134s, 3842 edges
        workers=8: 119s, 3844 edges (fastest, most edges)
        workers=9: 176s, 2804 edges (thrashed, 27% edges lost)
        workers=12: 159s, 3824 edges

    Sweet spot: physical_cores + 2. Beyond that, processes fight for
    physical execution units and hit per-file timeouts.

    Returns:
        Worker count. Logs the decision at DEBUG level.
    """
    env = os.environ.get("RTFM_WORKERS")
    if env:
        pinned = max(1, int(env))
        logger.debug("[concurrency] jedi workers pinned: %d (RTFM_WORKERS)", pinned)
        return pinned

    logical = os.cpu_count() or 1
    physical = _detect_physical_cores()
    idle = _is_system_idle(physical)

    if idle:
        # Burst: physical + 2 (benchmarked sweet spot)
        workers = min(physical + 2, logical - 2)
    else:
        # Contended: physical cores only
        workers = physical

    workers = max(workers, 1)
    logger.debug(
        "[concurrency] jedi workers adaptive: %d (physical=%d, logical=%d, idle=%s)",
        workers, physical, logical, idle,
    )
    return workers
