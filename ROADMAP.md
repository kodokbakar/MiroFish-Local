# MiroFish-Local Roadmap

## Current State (v0.7.0)

**MiroFish-Local** is a local-first, vendor-independent multi-agent simulation and GraphRAG engine.
Forked from [nikmcfly/MiroFish-Offline](https://github.com/nikmcfly/MiroFish-Offline) (which originated from [666ghj/MiroFish](https://github.com/666ghj/MiroFish)), this repository completely removes external cloud dependencies (Zep Cloud, DashScope) in favor of a local Neo4j 5.15+ Community Edition database behind an abstract `GraphStorage` layer and flexible OpenAI-compatible LLM endpoints (Ollama, vLLM, llama.cpp, Claude, OpenAI).

### v0.7.0 Key Capabilities
- **Engine**: 100% offline-capable, runs on Python 3.12+ with Flask and Neo4j CE.
- **Graph Visualization & Scale**:
  - D3 Level of Detail (LOD) and connection-degree node rendering cap (top 100/300/500/All) with off-screen entity search and injection to maintain 60 FPS viewport rendering (#81).
  - Bolt payload map projection prunes raw 1536-dim vector embeddings during visual queries, slashing network and memory footprint (#82).
- **Graph Intelligence & Clustering**:
  - Automated Louvain community detection (with GDS procedure and pure in-memory NetworkX fallback) to partition entities into thematic clusters with D3 palette mapping (#83).
  - Temporal graph evolution tracking across simulation rounds with interactive frontend scrubber playback (#84).
- **Simulation Execution & Streaming**:
  - Real-time Server-Sent Events (SSE) streaming endpoint (`/api/simulation/<id>/stream`) replacing frontend polling timers (#75).
  - Subprocess group isolation (`start_new_session=True` / `os.killpg`) to guarantee zero orphan OASIS processes (#76).
  - Customizable `AgentArchetype` definitions and ratio sampling for diverse agent behaviors (#77).
- **Staged Multi-Model Routing**:
  - Task-based model router assigning dedicated models per pipeline stage (`LLM_MODEL_NER`, `LLM_MODEL_REPORT`, `LLM_MODEL_SIMULATION`) with automatic fallback to `LLM_MODEL_NAME`.
- **Infrastructure Telemetry**:
  - `GET /api/status` endpoint diagnosing Neo4j connectivity, index population states, LLM reachability, and disk capacity without credential exposure.
- **Fast-Path Graph Ingestion**:
  - Direct Cypher batch ingestion for structured OASIS agent activities (`LIKE_POST`, `POSTED`, `FOLLOWS`, etc.) via `UNWIND`, bypassing LLM NER and dropping memory update latency to milliseconds.
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
- **Quality & Verification**: 879+ backend tests and 20 frontend tests passing offline across storage, clustering, simulation, and report generation.

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

### v0.5.0 — Multi-Model & Task Routing
- [x] **Task-based Model Router**: Assign dedicated models per pipeline stage (fast/cheap 7B-8B model for NER/RE, high-reasoning 32B+ model for ReAct Report Agent) (#69)
- [x] Support vLLM and llama.cpp as alternative backends alongside Ollama (via unified OpenAI-compatible `/v1` endpoint).
- [x] Model benchmarking and concurrency measurement script.
- [x] Telemetry & health endpoint `/api/status` (Neo4j connection, LLM endpoint availability, disk storage) (#70)
- [x] Bypass LLM NER for structured social actions (Fast-Path Ingestion via Cypher UNWIND MERGE) (#71)

### v0.6.0 — Enhanced Simulation & Real-time Streaming
- [x] Real-time simulation stream via SSE (`/api/simulation/<id>/stream`) replacing polling timers (#75)
- [x] Agent memory persistence across simulation rounds directly to Neo4j.
- [x] Process Group isolation (`start_new_session=True` / `os.killpg`) to guarantee full termination of child OASIS subprocesses (#76)
- [x] Export simulation transcripts as structured JSON.
- [x] Custom agent archetypes: define personality templates beyond OASIS defaults (#77)

### v0.7.0 — Graph Intelligence & Visual Scale
- [x] Visual level-of-detail and node rendering cap (top 100/300/500/All) in D3 to prevent browser main-thread freeze (#81).
- [x] Graph visualization improvements: force-directed layout, filtering by entity type, dynamic color palette.
- [x] Bolt payload projection: prune raw 1536-dim vector transfer on visual graph queries (#82).
- [x] Community detection (Louvain) to auto-identify entity clusters with NetworkX fallback (#83).
- [x] Temporal graph evolution tracking across simulation rounds with interactive playback scrubber (#84).

---

## Upcoming Milestones

### v0.8.0 — Multi-Scenario A/B Testing & Graph Branching
- [ ] Graph snapshot & branching: instant clone of graph state at round N to test counterfactual hypotheses.
- [ ] Multi-scenario comparison view: side-by-side visualization of divergence between baseline and counterfactual runs.
- [ ] Graph export/import (GraphML, JSON-LD, Gephi format) for external graph analytics.
- [ ] Streaming file reader for large PDF/EPUB/TXT corpus without memory spikes.

### v0.9.0 — Simulation Realism Metrics & Agent Evaluation
- [ ] Simulation realism & consensus metrics (clustering coefficient, echo chamber index, polarizations over rounds).
- [ ] LLM-as-a-judge debiasing metrics for simulation outputs.
- [ ] Cross-platform information diffusion simulation (multi-channel information flow between platforms).

---

## Long Term

### v1.0.0 — Production Ready
- [ ] Basic API key / Bearer token authentication guard for remote/LAN deployments.
- [ ] Standalone Docker Compose bundle with pre-configured Ollama/Neo4j healthchecks.
- [x] Comprehensive test suite (879+ backend and 20 frontend tests passing offline).
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
