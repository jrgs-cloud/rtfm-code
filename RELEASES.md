# rtfm — Release History

## v1.0.0 (2026-05-20)

**Published:** https://github.com/jrgs-cloud/rtfm-code/releases/tag/v1.0.0

**What shipped:**
- 17 CLI commands (gate, impact, query, neighbors, cluster, node, dark-spots, search, hybrid, build-all, enrich, index-semantic, reindex, init, watch, validate, export-vault)
- Python + TypeScript structural extraction
- Jedi type-resolved enrichment (parallel, adaptive concurrency)
- Semantic search (fastembed bge-small-en + LanceDB)
- Hybrid RRF search (structural + semantic merged)
- Incremental updates with always-enrich (no silent edge loss)
- Adaptive concurrency (physical cores + burst when idle)
- Watcher with auto-enrich + auto-index
- Dark-spots with actionable suggestions
- Obsidian vault export
- 496 tests passing

**Benchmarked (6-core/12-thread Linux, 1362-file monorepo):**
- Nodes: 8,238 | Edges: 18,881 | Chunks: 6,973
- Full build: 28.9min | Jedi enrichment: 119s | Search: 50ms warm
- Gate: 2ms | Impact: 45ms

**PyPI:** Not yet published (pending `rtfm-code` registration)

---

## Unreleased

- Guru incremental wiring (/wake + /sync triggers)
- Crabstik end-to-end validation
- PyPI publish
