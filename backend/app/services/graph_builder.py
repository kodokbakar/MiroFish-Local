"""
Graph building service.
Uses GraphStorage (Neo4j) to replace Zep Cloud API.
"""

import time
import logging
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, Any, List, Optional, Callable
from dataclasses import dataclass

from ..config import Config
from ..models.task import TaskManager, TaskStatus
from ..storage import GraphStorage
from .text_processor import TextProcessor
from ..utils.ontology import normalize_ontology_attribute

logger = logging.getLogger('mirofish.graph_builder')


@dataclass
class GraphInfo:
    """Graph information"""
    graph_id: str
    node_count: int
    edge_count: int
    entity_types: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "node_count": self.node_count,
            "edge_count": self.edge_count,
            "entity_types": self.entity_types,
        }


class GraphBuilderService:
    """
    Graph building service
    Build knowledge graph through GraphStorage interface
    """

    def __init__(self, storage: GraphStorage):
        self.storage = storage
        self.task_manager = TaskManager()

    def build_graph_async(
        self,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str = "MiroFish Graph",
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        batch_size: int = 3
    ) -> str:
        """
        Build graph asynchronously

        Args:
            text: Input text to process
            ontology: Ontology definition (from ontology generator output)
            graph_name: Name for the graph
            chunk_size: Text chunk size
            chunk_overlap: Chunk overlap size
            batch_size: Number of chunks to send per batch

        Returns:
            Task ID
        """
        # Create task
        task_id = self.task_manager.create_task(
            task_type="graph_build",
            metadata={
                "graph_name": graph_name,
                "chunk_size": chunk_size,
                "text_length": len(text),
            }
        )

        # Execute build in background thread
        thread = threading.Thread(
            target=self._build_graph_worker,
            args=(task_id, text, ontology, graph_name, chunk_size, chunk_overlap, batch_size)
        )
        thread.daemon = True
        thread.start()

        return task_id

    def _build_graph_worker(
        self,
        task_id: str,
        text: str,
        ontology: Dict[str, Any],
        graph_name: str,
        chunk_size: int,
        chunk_overlap: int,
        batch_size: int
    ):
        """Graph build worker thread"""
        try:
            self.task_manager.update_task(
                task_id,
                status=TaskStatus.PROCESSING,
                progress=5,
                message="Starting graph building..."
            )

            # 1. Create graph
            graph_id = self.create_graph(graph_name)
            self.task_manager.update_task(
                task_id,
                progress=10,
                message=f"Graph created: {graph_id}"
            )

            # 2. Set ontology
            self.set_ontology(graph_id, ontology)
            self.task_manager.update_task(
                task_id,
                progress=15,
                message="Ontology set"
            )

            # 3. Text chunking
            chunks = TextProcessor.split_text(text, chunk_size, chunk_overlap)
            total_chunks = len(chunks)
            self.task_manager.update_task(
                task_id,
                progress=20,
                message=f"Text split into {total_chunks} chunks"
            )

            # 4. Send data in batches (NER + embedding + Neo4j insert — synchronous)
            episode_uuids = self.add_text_batches(
                graph_id, chunks, batch_size,
                lambda msg, prog: self.task_manager.update_task(
                    task_id,
                    progress=20 + int(prog * 0.6),  # 20-80%
                    message=msg
                ),
                ontology=ontology
            )

            # 5. Wait for processing (no-op for Neo4j — already synchronous)
            self.storage.wait_for_processing(episode_uuids)

            self.task_manager.update_task(
                task_id,
                progress=85,
                message="Data processing completed, getting graph information..."
            )

            # 6. Get graph information
            graph_info = self._get_graph_info(graph_id)

            # Completed
            self.task_manager.complete_task(task_id, {
                "graph_id": graph_id,
                "graph_info": graph_info.to_dict(),
                "chunks_processed": total_chunks,
            })

        except Exception as e:
            import traceback
            error_msg = f"{str(e)}\n{traceback.format_exc()}"
            self.task_manager.fail_task(task_id, error_msg)

    def create_graph(self, name: str) -> str:
        """Create graph"""
        return self.storage.create_graph(
            name=name,
            description="MiroFish Social Simulation Graph"
        )

    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]):
        """
        SetGraphOntology

        Simply stores ontology as JSON in the Graph node.
        No more dynamic Pydantic class creation (was Zep-specific).
        The NER extractor reads this ontology to guide extraction.
        """
        if isinstance(ontology, dict):
            for key in ("entity_types", "edge_types", "relation_types"):
                type_list = ontology.get(key)
                if isinstance(type_list, list):
                    for item in type_list:
                        if isinstance(item, dict) and "attributes" in item:
                            attrs = item["attributes"]
                            if isinstance(attrs, str):
                                attrs = [attrs]
                            if isinstance(attrs, list):
                                item["attributes"] = [
                                    normalized
                                    for attr in attrs
                                    if (normalized := normalize_ontology_attribute(attr)) is not None
                                ]
        self.storage.set_ontology(graph_id, ontology)

    def add_text_batches(
        self,
        graph_id: str,
        chunks: List[str],
        batch_size: int = 3,
        progress_callback: Optional[Callable] = None,
        ontology: Optional[Dict[str, Any]] = None,
        max_workers: Optional[int] = None,
    ) -> List[str]:
        """Extract with bounded concurrency, then persist episodes in source order."""
        episode_uuids = []
        total_chunks = len(chunks)
        if not total_chunks:
            return episode_uuids
        batch_size = max(1, batch_size)
        workers = Config.NER_EXTRACTION_WORKERS if max_workers is None else max_workers
        if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4:
            raise ValueError("NER_EXTRACTION_WORKERS must be between 1 and 4")
        total_batches = (total_chunks + batch_size - 1) // batch_size

        logger.info(
            "[graph_build] Starting: %d chunks, %d batches, workers=%d, max_inflight=%d",
            total_chunks, total_batches, workers, workers * 2,
        )

        def record_persisted(index: int, episode_id: str, elapsed: float) -> None:
            episode_uuids.append(episode_id)
            logger.info(
                "[graph_build] Chunk %d/%d persisted in %.3fs",
                index + 1, total_chunks, elapsed,
            )
            if progress_callback:
                progress_callback(
                    f"Processed chunk {index + 1}/{total_chunks}",
                    (index + 1) / total_chunks,
                )

        if workers == 1:
            for index, chunk in enumerate(chunks):
                started = time.perf_counter()
                try:
                    episode_id = self.storage.add_text(graph_id, chunk, ontology=ontology)
                except Exception as error:
                    logger.error(
                        "[graph_build] Chunk %d/%d failed: %s",
                        index + 1, total_chunks, type(error).__name__,
                    )
                    if progress_callback:
                        progress_callback(f"Chunk {index + 1} processing failed", 0)
                    raise
                record_persisted(index, episode_id, time.perf_counter() - started)
        else:
            if ontology is None:
                ontology = self.storage.get_ontology(graph_id)

            def extract_one(chunk: str):
                started = time.perf_counter()
                result = self.storage.extract_text(chunk, ontology)
                return result, time.perf_counter() - started

            pending = deque()
            next_index = 0
            extraction_seconds = 0.0
            persistence_seconds = 0.0
            with ThreadPoolExecutor(max_workers=workers) as pool:
                while next_index < min(total_chunks, workers * 2):
                    pending.append((next_index, pool.submit(extract_one, chunks[next_index])))
                    next_index += 1

                while pending:
                    index, future = pending.popleft()
                    try:
                        extraction, elapsed = future.result()
                        extraction_seconds += elapsed
                        started = time.perf_counter()
                        episode_id = self.storage.persist_extraction(graph_id, chunks[index], extraction)
                        persisted_in = time.perf_counter() - started
                        persistence_seconds += persisted_in
                    except Exception as error:
                        for _, remaining in pending:
                            remaining.cancel()
                        logger.error(
                            "[graph_build] Chunk %d/%d failed: %s",
                            index + 1, total_chunks, type(error).__name__,
                        )
                        if progress_callback:
                            progress_callback(f"Chunk {index + 1} processing failed", 0)
                        raise
                    record_persisted(index, episode_id, persisted_in)
                    if next_index < total_chunks:
                        pending.append((next_index, pool.submit(extract_one, chunks[next_index])))
                        next_index += 1
            logger.info(
                "[graph_build] Stage timings: extraction_sum=%.3fs persistence_sum=%.3fs",
                extraction_seconds, persistence_seconds,
            )

        logger.info("[graph_build] All %d chunks processed successfully", total_chunks)
        return episode_uuids

    def _get_graph_info(self, graph_id: str) -> GraphInfo:
        """Get graph information"""
        info = self.storage.get_graph_info(graph_id)
        return GraphInfo(
            graph_id=info["graph_id"],
            node_count=info["node_count"],
            edge_count=info["edge_count"],
            entity_types=info.get("entity_types", []),
        )

    def get_graph_data(self, graph_id: str) -> Dict[str, Any]:
        """Get complete graph data (including details)"""
        return self.storage.get_graph_data(graph_id)

    def delete_graph(self, graph_id: str):
        """Delete graph"""
        self.storage.delete_graph(graph_id)
