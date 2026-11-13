# MiroFish-Local Roadmap

## Current State (v0.4.0)

**MiroFish-Local** is a local-first, vendor-independent multi-agent simulation and GraphRAG engine.
Forked from [nikmcfly/MiroFish-Offline](https://github.com/nikmcfly/MiroFish-Offline) (which originated from [666ghj/MiroFish](https://github.com/666ghj/MiroFish)), this repository completely removes external cloud dependencies (Zep Cloud, DashScope) in favor of a local Neo4j 5.15+ Community Edition database behind an abstract `GraphStorage` layer and flexible OpenAI-compatible LLM endpoints (Ollama, vLLM, llama.cpp, Claude, OpenAI).

### v0.4.0 Key Capabilities
- **Engine**: 100% offline-capable, runs on Python 3.12+ with Flask and Neo4j CE.
- **Resilience & Concurrency**:
  - Auto-reconnect & retry logic with exponential backoff and randomized jitter on Neo4j transient errors (`TransientError`, `ServiceUnavailable`, `SessionExpired`).
  - Deterministic entity (`name_lower`) and relation lock ordering plus per-graph write mutex to prevent Neo4j transaction deadlocks.
  - Exponential backoff with full jitter and `Retry-After` header support in `LLMClient` for transient HTTP 429 rate limits and connection drops.
- **Search & Retrieval**:
  - Hybrid search combining Cosine Vector Similarity (0.7) and BM25 Fulltext Keyword Matching (0.3).
  - Multi-project candidate oversampling (`candidate_limit`) to prevent vector search zero-recall caused by global index starvation.
  - Zero-vector guard preventing division by zero on Neo4j cosine similarity calculations.
- **Ingestion & Caching**:
  - Table-aware parsing and chunking for CSV and JSON datasets.
  - Local atomic caching for successful NER extraction to avoid redundant LLM calls across builds.
  - Parallel bounded NER extraction (1–4 worker threads) with ordered serial persistence.
- **Quality & Verification**: 853 automated tests passing offline across backend storage, ingestion, simulation, and report generation.

---

## Completed Milestones

### v0.1.0 — Initial Local Migration
- [x] Full English UI translation across Vue components.
- [x] Complete removal of Zep Cloud SDK and DashScope API lock-in.
- [x] Abstract `GraphStorage` interface implementation with Neo4j 5.15+ CE.
- [x] Local Ollama LLM and embedding integration.

### v0.2.0 — Subprocess & Pipeline Stability
- [x] Zombie subprocess termination and IPC cleanup on simulation stop.
- [x] ReACT loop tool call parsing guards and fake tool result sanitization.
- [x] Structured LLM output normalization for OASIS agent profiles.
- [x] Multi-platform simulation runner state management and partial action log recovery.

### v0.3.0 — Resilience, Security & Concurrency
- [x] Neo4j transient connection retry with exponential backoff and randomized jitter.
- [x] Python 3.12+ execution support.
- [x] Frontend XSS prevention on Markdown rendering and interview responses.
- [x] HTTP 429 rate limit retry with exponential backoff, full jitter, and `Retry-After` support.
- [x] Deterministic lock acquisition order and per-graph write mutex to prevent transaction deadlocks on hot entities.

### v0.4.0 — Ingestion & Search Isolation
- [x] CSV and JSON table-aware upload parsing and chunking.
- [x] Local atomic disk caching for NER extraction results across builds.
- [x] Parallel NER extraction pipeline with bounded concurrency (1–4 workers).
- [x] Multi-project vector search candidate oversampling (`candidate_limit = max(limit * 10, 100)`) to resolve zero-recall cross-graph starvation.
- [x] Concurrency and throughput benchmark suite (`backend/scripts/benchmark_ner_concurrency.py`).
- [x] Comprehensive test suite exceeding 850+ offline passing tests.

---

## Upcoming Milestones

### v0.5.0 — Multi-Model & Task Routing
- [ ] **Task-based Model Router**: Assign dedicated models per pipeline stage (fast/cheap 7B-8B model for NER/RE, high-reasoning 32B+ model for ReAct Report Agent) (#69)
- [x] Support vLLM and llama.cpp as alternative backends alongside Ollama (via unified OpenAI-compatible `/v1` endpoint).
- [x] Model benchmarking and concurrency measurement script.
- [ ] Telemetry & health endpoint `/api/status` (Neo4j connection, LLM endpoint availability, disk storage) (#70)
- [ ] Bypass LLM NER for structured social actions (Fast-Path Ingestion via Cypher UNWIND MERGE) (#71)

### v0.6.0 — Enhanced Simulation & Real-time Streaming
- [ ] Real-time simulation stream via SSE/WebSocket to eliminate polling `setInterval` timers in frontend.
- [x] Agent memory persistence across simulation rounds directly to Neo4j.
- [ ] Process Group isolation (`start_new_session`) to guarantee full termination of child OASIS subprocesses.
- [x] Export simulation transcripts as structured JSON.
- [ ] Custom agent archetypes: define personality templates beyond OASIS defaults.

### v0.7.0 — Graph Intelligence & Visual Scale
- [ ] Visual level-of-detail and node rendering cap (top 300 degree nodes) in D3 to prevent browser main-thread freeze.
- [x] Graph visualization improvements: force-directed layout, filtering by entity type, dynamic color palette.
- [ ] Bolt payload projection: prune raw 1536-dim vector transfer on visual graph queries.
- [ ] Community detection (Louvain/Leiden) to auto-identify entity clusters.
- [ ] Temporal graph evolution tracking across simulation rounds.

---

## Long Term

### v1.0.0 — Production Ready
- [ ] Graph versioning & cloning: instant snapshot/clone for multi-scenario A/B simulations.
- [ ] Basic API key / Bearer token authentication guard for remote/LAN deployments.
- [ ] Streaming file reader to prevent memory spikes on large file ingestion.
- [x] Comprehensive test suite (unit + integration + contract tests passing offline).
- [x] Concurrency and throughput benchmark suite.
- [ ] Deployment guides for standalone Docker Compose and bare-metal environments.

### Beyond v1.0
- [ ] Federation: connect multiple MiroFish instances to share entity knowledge.
- [ ] Fine-tuned local models specifically trained for social simulation NER/RE.
- [ ] Voice-driven interaction with simulation agents via local Whisper + TTS.

---

## Hardware Tiers

| Tier | RAM | GPU VRAM | Recommended Model | Expected Performance |
|------|-----|----------|-------------------|---------------------|
| Minimal | 8 GB | — (CPU only) | qwen2.5:3b | Slow, basic NER quality |
| Light | 16 GB | 6-8 GB | qwen2.5:7b | Usable for small graphs |
| Standard | 32 GB | 12-16 GB | qwen2.5:14b | Good for most use cases |
| Power | 64 GB | 24+ GB | qwen2.5:32b | Full quality, fast |

---

## Contributing & License

This project is licensed under **AGPL-3.0**. Credits and attribution to the original [MiroFish](https://github.com/666ghj/MiroFish) and [MiroFish-Offline](https://github.com/nikmcfly/MiroFish-Offline) are preserved.
Contributions are welcome. See [GitHub Issues](https://github.com/kodokbakar/MiroFish-Local/issues) for active tasks.
