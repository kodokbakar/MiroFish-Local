"""Synthetic ingestion benchmark; does not represent a live LLM or Neo4j run.

Run from backend with ``PYTHONPATH=. python scripts/benchmark_ner_concurrency.py``.
The 12 fixed-size chunks and stage delays make the serial and parallel paths
comparable without credentials. Use a live provider and database for deployment
capacity decisions; the production default remains one extraction worker.
"""

import json
import threading
import time

from app.services.graph_builder import GraphBuilderService


class TimedStorage:
    """Simulate 60 ms NER, 12 ms embedding, and 12 ms graph writes per chunk."""

    def __init__(self):
        self.caller = threading.get_ident()
        self.lock = threading.Lock()
        self.extraction_seconds = 0.0
        self.embedding_seconds = 0.0
        self.write_seconds = 0.0

    def extract_text(self, text, ontology):
        started = time.perf_counter()
        time.sleep(0.060)
        with self.lock:
            self.extraction_seconds += time.perf_counter() - started
        return {"entities": [], "relations": []}

    def persist_extraction(self, graph_id, text, extraction):
        assert threading.get_ident() == self.caller, "write ran on a worker"
        started = time.perf_counter()
        time.sleep(0.012)
        self.embedding_seconds += time.perf_counter() - started
        started = time.perf_counter()
        time.sleep(0.012)
        self.write_seconds += time.perf_counter() - started
        return f"episode-{text[:2]}"

    def add_text(self, graph_id, text, ontology=None):
        return self.persist_extraction(graph_id, text, self.extract_text(text, ontology))


def main():
    chunks = [f"{index:02d} " + "A representative document sentence. " * 14 for index in range(12)]
    results = []
    for workers in (1, 2, 4):
        storage = TimedStorage()
        started = time.perf_counter()
        episodes = GraphBuilderService(storage).add_text_batches(
            "benchmark", chunks, ontology={"entity_types": [], "edge_types": []},
            max_workers=workers,
        )
        results.append({
            "workers": workers,
            "chunks": len(episodes),
            "elapsed_seconds": round(time.perf_counter() - started, 3),
            "extraction_stage_seconds_sum": round(storage.extraction_seconds, 3),
            "embedding_stage_seconds_sum": round(storage.embedding_seconds, 3),
            "write_stage_seconds_sum": round(storage.write_seconds, 3),
        })
    print(json.dumps({"kind": "synthetic", "results": results}, indent=2))


if __name__ == "__main__":
    main()
