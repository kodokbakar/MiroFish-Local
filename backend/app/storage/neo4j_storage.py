"""
Neo4jStorage — Neo4j Community Edition implementation of GraphStorage.

Replaces all Zep Cloud API calls with local Neo4j Cypher queries.
Includes: CRUD, NER/RE-based text ingestion, hybrid search, retry logic.
"""

import json
import time
import uuid
import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Callable

from neo4j import GraphDatabase, Session as Neo4jSession
from neo4j.exceptions import (
    TransientError,
    ServiceUnavailable,
    SessionExpired,
)

from ..config import Config
from .graph_storage import GraphStorage
from .embedding_service import EmbeddingService
from .ner_extractor import NERExtractor
from .search_service import SearchService
from .neo4j_records import edge_to_dict, node_to_dict
from . import neo4j_schema
from .label_utils import LABEL_PATTERN, validate_label, is_valid_label

__all__ = [
    "Neo4jStorage",
    "LABEL_PATTERN",
    "validate_label",
    "is_valid_label",
]

logger = logging.getLogger('mirofish.neo4j_storage')


class Neo4jStorage(GraphStorage):
    """Neo4j CE implementation of the GraphStorage interface."""

    MAX_RETRIES = 3
    RETRY_DELAY_BASE = 1  # seconds

    def __init__(
        self,
        uri: Optional[str] = None,
        user: Optional[str] = None,
        password: Optional[str] = None,
        embedding_service: Optional[EmbeddingService] = None,
        ner_extractor: Optional[NERExtractor] = None,
    ):
        self._uri = uri or Config.NEO4J_URI
        self._user = user or Config.NEO4J_USER
        self._password = password or Config.NEO4J_PASSWORD

        self._driver = GraphDatabase.driver(
            self._uri, auth=(self._user, self._password)
        )
        self._embedding = embedding_service or EmbeddingService()
        self._ner = ner_extractor or NERExtractor()
        self._search = SearchService(self._embedding)

        # Initialize schema (indexes, constraints)
        self._ensure_schema()

    def close(self):
        """Close the Neo4j driver connection."""
        self._driver.close()

    def _ensure_schema(self):
        """Create indexes and constraints if they don't exist and wait for online status."""
        with self._driver.session() as session:
            for query in neo4j_schema.ALL_SCHEMA_QUERIES:
                try:
                    session.run(query)
                except Exception as e:
                    logger.warning(f"Schema query warning (may already exist): {e}")

        try:
            self.wait_for_indexes_online(timeout_sec=5.0)
        except Exception as e:
            logger.debug(f"Index wait check failed or skipped: {e}")

    def wait_for_indexes_online(self, timeout_sec: float = 10.0, poll_interval: float = 0.5) -> bool:
        """
        Wait for all schema indexes to transition from POPULATING to ONLINE.
        Safely handles large databases where index population occurs asynchronously.
        Returns True if all indexes are ONLINE, False if timeout reached.
        """
        start_time = time.time()
        while time.time() - start_time < timeout_sec:
            try:
                with self._driver.session() as session:
                    res = session.run("SHOW INDEXES YIELD name, state, populationPercent")
                    records = list(res)
                    if not records:
                        return True
                    populating = [
                        r["name"] for r in records
                        if r.get("state") == "POPULATING"
                    ]
                    failed = [
                        r["name"] for r in records
                        if r.get("state") == "FAILED"
                    ]
                    if failed:
                        logger.error(f"Neo4j indexes in FAILED state: {failed}")
                    if not populating:
                        logger.info("All Neo4j indexes are ONLINE.")
                        return True
                    logger.info(f"Waiting for indexes to populate: {populating}...")
            except Exception as e:
                logger.debug(f"SHOW INDEXES query not supported or failed: {e}")
                return True
            time.sleep(poll_interval)

        logger.warning(
            f"Timeout ({timeout_sec}s) waiting for indexes to become ONLINE. "
            "They will continue populating in the background."
        )
        return False

    # ----------------------------------------------------------------
    # Retry wrapper
    # ----------------------------------------------------------------

    def _call_with_retry(self, func, *args, **kwargs):
        """
        Execute a function with retry on Neo4j transient errors.
        Replaces 3 different retry patterns from the Zep codebase.
        """
        last_error = None
        for attempt in range(self.MAX_RETRIES):
            try:
                return func(*args, **kwargs)
            except (TransientError, ServiceUnavailable, SessionExpired) as e:
                last_error = e
                wait = self.RETRY_DELAY_BASE * (2 ** attempt)
                logger.warning(
                    f"Neo4j transient error (attempt {attempt + 1}/{self.MAX_RETRIES}), "
                    f"retrying in {wait}s: {e}"
                )
                time.sleep(wait)
            except Exception:
                raise

        raise last_error  # type: ignore

    # ----------------------------------------------------------------
    # Graph lifecycle
    # ----------------------------------------------------------------

    def create_graph(self, name: str, description: str = "") -> str:
        graph_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        def _create(tx):
            tx.run(
                """
                CREATE (g:Graph {
                    graph_id: $graph_id,
                    name: $name,
                    description: $description,
                    ontology_json: '{}',
                    created_at: $created_at
                })
                """,
                graph_id=graph_id,
                name=name,
                description=description,
                created_at=now,
            )

        with self._driver.session() as session:
            self._call_with_retry(session.execute_write, _create)

        logger.info(f"Created graph '{name}' with id {graph_id}")
        return graph_id

    def delete_graph(self, graph_id: str) -> None:
        def _delete(tx):
            # Delete all entities and their relationships
            tx.run(
                "MATCH (n {graph_id: $gid}) DETACH DELETE n",
                gid=graph_id,
            )
            # Delete graph node
            tx.run(
                "MATCH (g:Graph {graph_id: $gid}) DELETE g",
                gid=graph_id,
            )

        with self._driver.session() as session:
            self._call_with_retry(session.execute_write, _delete)
        logger.info(f"Deleted graph {graph_id}")

    def set_ontology(self, graph_id: str, ontology: Dict[str, Any]) -> None:
        def _set(tx):
            tx.run(
                """
                MATCH (g:Graph {graph_id: $gid})
                SET g.ontology_json = $ontology_json
                """,
                gid=graph_id,
                ontology_json=json.dumps(ontology, ensure_ascii=False),
            )

        with self._driver.session() as session:
            self._call_with_retry(session.execute_write, _set)

    def get_ontology(self, graph_id: str) -> Dict[str, Any]:
        with self._driver.session() as session:
            result = session.run(
                "MATCH (g:Graph {graph_id: $gid}) RETURN g.ontology_json AS oj",
                gid=graph_id,
            )
            record = result.single()
            if record and record["oj"]:
                return json.loads(record["oj"])
            return {}

    # ----------------------------------------------------------------
    # Add data (NER → nodes/edges)
    # ----------------------------------------------------------------

    def add_text(
        self,
        graph_id: str,
        text: str,
        ontology: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Process text: NER/RE → batch embed → create nodes/edges → return episode_id."""
        episode_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # Get ontology for NER guidance if not provided
        if ontology is None:
            ontology = self.get_ontology(graph_id)

        # Extract entities and relations
        logger.info(f"[add_text] Starting NER extraction for chunk ({len(text)} chars)...")
        extraction = self._ner.extract(text, ontology)
        entities = extraction.get("entities", [])
        relations = extraction.get("relations", [])

        logger.info(
            f"[add_text] NER done: {len(entities)} entities, {len(relations)} relations"
        )

        # --- Batch embed all texts at once ---
        entity_summaries = [f"{e['name']} ({e['type']})" for e in entities]
        fact_texts = [r.get("fact", f"{r['source']} {r['type']} {r['target']}") for r in relations]
        all_texts_to_embed = entity_summaries + fact_texts

        all_embeddings: list = []
        if all_texts_to_embed:
            logger.info(f"[add_text] Batch-embedding {len(all_texts_to_embed)} texts...")
            try:
                all_embeddings = self._embedding.embed_batch(all_texts_to_embed)
            except Exception as e:
                logger.warning(f"[add_text] Batch embedding failed, falling back to empty: {e}")
                all_embeddings = [[] for _ in all_texts_to_embed]

        entity_embeddings = all_embeddings[:len(entities)]
        relation_embeddings = all_embeddings[len(entities):]
        logger.info(f"[add_text] Embedding done, writing to Neo4j...")

        # Pre-process entities: deduplicate and merge within chunk
        merged_entities_by_name: Dict[str, Dict[str, Any]] = {}
        for idx, entity in enumerate(entities):
            ename = entity.get("name", "")
            if not ename:
                continue
            name_lower = ename.lower()
            etype = entity.get("type", "Entity")
            attrs = entity.get("attributes", {})
            summary_text = entity_summaries[idx]
            embedding = entity_embeddings[idx] if idx < len(entity_embeddings) else []

            if name_lower not in merged_entities_by_name:
                merged_entities_by_name[name_lower] = {
                    "name": ename,
                    "name_lower": name_lower,
                    "type": etype,
                    "attributes": dict(attrs) if isinstance(attrs, dict) else {},
                    "summary": summary_text,
                    "embedding": embedding,
                    "uuid": str(uuid.uuid4()),
                }
            else:
                prev = merged_entities_by_name[name_lower]
                if isinstance(attrs, dict):
                    prev["attributes"].update(attrs)
                if not prev["summary"] and summary_text:
                    prev["summary"] = summary_text
                if not prev["embedding"] and embedding:
                    prev["embedding"] = embedding
                if etype and etype != "Entity":
                    prev["type"] = etype

        # Group valid labels for batch label assignment
        type_to_names: Dict[str, List[str]] = defaultdict(list)
        for item in merged_entities_by_name.values():
            etype = item.get("type")
            if etype and etype != "Entity":
                try:
                    validate_label(etype)
                    type_to_names[etype].append(item["name_lower"])
                except ValueError as e:
                    logger.warning(f"Skipping invalid entity label '{etype}' for '{item['name']}': {e}")
                except Exception as e:
                    logger.warning(f"Failed to add label '{etype}' to '{item['name']}': {e}")

        with self._driver.session() as session:
            def _persist_chunk(tx):
                # 1. Create episode node
                tx.run(
                    """
                    CREATE (ep:Episode {
                        uuid: $uuid,
                        graph_id: $graph_id,
                        data: $data,
                        processed: true,
                        created_at: $created_at
                    })
                    """,
                    uuid=episode_id,
                    graph_id=graph_id,
                    data=text,
                    created_at=now,
                )

                uuid_map: Dict[str, str] = {
                    item["name_lower"]: item["uuid"] for item in merged_entities_by_name.values()
                }

                # 2. If entities exist, check existing attributes in batch and MERGE via UNWIND
                if merged_entities_by_name:
                    name_lowers = list(merged_entities_by_name.keys())
                    # Batch fetch existing attributes
                    existing_records = tx.run(
                        """
                        MATCH (n:Entity {graph_id: $gid})
                        WHERE n.name_lower IN $name_lowers
                        RETURN n.name_lower AS name_lower, n.attributes_json AS attributes_json
                        """,
                        gid=graph_id,
                        name_lowers=name_lowers,
                    )
                    if existing_records:
                        for rec in existing_records:
                            try:
                                nl = rec["name_lower"]
                                if nl in merged_entities_by_name and rec["attributes_json"]:
                                    existing_attrs = json.loads(rec["attributes_json"])
                                    if isinstance(existing_attrs, dict):
                                        merged_entities_by_name[nl]["attributes"] = {
                                            **existing_attrs,
                                            **merged_entities_by_name[nl]["attributes"],
                                        }
                            except (json.JSONDecodeError, TypeError, KeyError):
                                pass

                    entities_data = [
                        {
                            "uuid": item["uuid"],
                            "name": item["name"],
                            "name_lower": item["name_lower"],
                            "summary": item["summary"],
                            "attrs_json": json.dumps(item["attributes"], ensure_ascii=False),
                            "embedding": item["embedding"],
                        }
                        for item in merged_entities_by_name.values()
                    ]

                    res = tx.run(
                        """
                        UNWIND $entities AS e
                        MERGE (n:Entity {graph_id: $gid, name_lower: e.name_lower})
                        ON CREATE SET
                            n.uuid = e.uuid,
                            n.name = e.name,
                            n.summary = e.summary,
                            n.attributes_json = e.attrs_json,
                            n.embedding = e.embedding,
                            n.created_at = $now
                        ON MATCH SET
                            n.summary = CASE WHEN n.summary = '' OR n.summary IS NULL
                                THEN e.summary ELSE n.summary END,
                            n.attributes_json = e.attrs_json,
                            n.embedding = e.embedding
                        RETURN e.name_lower AS name_lower, n.uuid AS uuid
                        """,
                        gid=graph_id,
                        entities=entities_data,
                        now=now,
                    )
                    if res:
                        for rec in res:
                            try:
                                if rec and rec["name_lower"] and rec["uuid"]:
                                    uuid_map[rec["name_lower"]] = rec["uuid"]
                            except Exception:
                                pass

                # 3. Batch set labels grouped by entity type
                for etype, name_list in type_to_names.items():
                    tx.run(
                        f"MATCH (n:Entity {{graph_id: $gid}}) WHERE n.name_lower IN $names SET n:`{etype}`",
                        gid=graph_id,
                        names=name_list,
                    )

                # 4. Resolve UUIDs for relations and batch MERGE via UNWIND
                if relations:
                    # Check if any relation source/target is missing from uuid_map
                    missing_names = set()
                    for r in relations:
                        s_name = r.get("source", "").lower()
                        t_name = r.get("target", "").lower()
                        if s_name and s_name not in uuid_map:
                            missing_names.add(s_name)
                        if t_name and t_name not in uuid_map:
                            missing_names.add(t_name)

                    if missing_names:
                        missing_records = tx.run(
                            """
                            MATCH (n:Entity {graph_id: $gid})
                            WHERE n.name_lower IN $missing_names
                            RETURN n.name_lower AS name_lower, n.uuid AS uuid
                            """,
                            gid=graph_id,
                            missing_names=list(missing_names),
                        )
                        if missing_records:
                            for rec in missing_records:
                                try:
                                    if rec and rec["name_lower"] and rec["uuid"]:
                                        uuid_map[rec["name_lower"]] = rec["uuid"]
                                except Exception:
                                    pass

                    # Prepare and deduplicate relations for UNWIND
                    deduped_relations: Dict[tuple, Dict[str, Any]] = {}
                    for idx, relation in enumerate(relations):
                        source_name = relation.get("source", "")
                        target_name = relation.get("target", "")
                        rtype = relation.get("type", "")
                        fact = relation.get("fact", "")

                        source_uuid = uuid_map.get(source_name.lower())
                        target_uuid = uuid_map.get(target_name.lower())

                        if not source_uuid or not target_uuid:
                            logger.warning(
                                f"Skipping relation {source_name}->{target_name}: "
                                f"entity not found in extraction results"
                            )
                            continue

                        fact_emb = relation_embeddings[idx] if idx < len(relation_embeddings) else []
                        key = (source_uuid, target_uuid, rtype)
                        if key not in deduped_relations:
                            deduped_relations[key] = {
                                "uuid": str(uuid.uuid4()),
                                "source_uuid": source_uuid,
                                "target_uuid": target_uuid,
                                "name": rtype,
                                "fact": fact,
                                "fact_embedding": fact_emb,
                            }
                        else:
                            prev = deduped_relations[key]
                            if not prev.get("fact") and fact:
                                prev["fact"] = fact
                                prev["fact_embedding"] = fact_emb

                    prepared_relations = list(deduped_relations.values())
                    if prepared_relations:
                        tx.run(
                            """
                            UNWIND $relations AS rel_data
                            MATCH (src:Entity {uuid: rel_data.source_uuid})
                            MATCH (tgt:Entity {uuid: rel_data.target_uuid})
                            MERGE (src)-[r:RELATION {graph_id: $gid, name: rel_data.name}]->(tgt)
                            ON CREATE SET
                                r.uuid = rel_data.uuid,
                                r.graph_id = $gid,
                                r.name = rel_data.name,
                                r.fact = rel_data.fact,
                                r.fact_embedding = rel_data.fact_embedding,
                                r.attributes_json = '{}',
                                r.episode_ids = [$episode_id],
                                r.created_at = $now,
                                r.valid_at = null,
                                r.invalid_at = null,
                                r.expired_at = null
                            ON MATCH SET
                                r.episode_ids = CASE
                                    WHEN r.episode_ids IS NULL THEN [$episode_id]
                                    WHEN $episode_id IN r.episode_ids THEN r.episode_ids
                                    ELSE r.episode_ids + [$episode_id]
                                END,
                                r.fact_embedding = CASE
                                    WHEN r.fact = '' OR r.fact IS NULL THEN rel_data.fact_embedding
                                    ELSE r.fact_embedding
                                END,
                                r.fact = CASE
                                    WHEN r.fact = '' OR r.fact IS NULL THEN rel_data.fact
                                    ELSE r.fact
                                END
                            """,
                            gid=graph_id,
                            relations=prepared_relations,
                            episode_id=episode_id,
                            now=now,
                        )

            self._call_with_retry(session.execute_write, _persist_chunk)

        logger.info(f"[add_text] Chunk done: episode={episode_id}")
        return episode_id

    def add_text_batch(
        self,
        graph_id: str,
        chunks: List[str],
        batch_size: int = 3,
        progress_callback: Optional[Callable] = None,
        ontology: Optional[Dict[str, Any]] = None,
    ) -> List[str]:
        """Batch-add text chunks with progress reporting and cached ontology."""
        episode_ids = []
        valid_chunks = [c for c in chunks if c and c.strip()]
        total = len(valid_chunks)
        if total == 0:
            return []

        # Cache ontology once for the entire batch to avoid redundant Cypher queries
        if ontology is None:
            ontology = self.get_ontology(graph_id)

        batch_size = max(1, batch_size)
        total_batches = (total + batch_size - 1) // batch_size
        logger.info(f"[add_text_batch] Ingesting {total} chunks in {total_batches} batches (batch_size={batch_size})")

        for i in range(0, total, batch_size):
            batch = valid_chunks[i:i + batch_size]
            for j, chunk in enumerate(batch):
                idx = i + j
                episode_id = self.add_text(graph_id, chunk, ontology=ontology)
                episode_ids.append(episode_id)

                if progress_callback:
                    progress = (idx + 1) / total
                    progress_callback(progress)

                logger.info(f"Processed chunk {idx + 1}/{total}")

        return episode_ids

    def wait_for_processing(
        self,
        episode_ids: List[str],
        progress_callback: Optional[Callable] = None,
        timeout: int = 600,
    ) -> None:
        """No-op — processing is synchronous in Neo4j."""
        if progress_callback:
            progress_callback(1.0)

    # ----------------------------------------------------------------
    # Read nodes
    # ----------------------------------------------------------------

    def get_all_nodes(self, graph_id: str, limit: int = 2000) -> List[Dict[str, Any]]:
        def _read(tx):
            result = tx.run(
                """
                MATCH (n:Entity {graph_id: $gid})
                RETURN n, labels(n) AS labels
                ORDER BY n.created_at DESC
                LIMIT $limit
                """,
                gid=graph_id,
                limit=limit,
            )
            return [self._node_to_dict(record["n"], record["labels"]) for record in result]

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    def get_node(self, uuid: str) -> Optional[Dict[str, Any]]:
        def _read(tx):
            result = tx.run(
                "MATCH (n:Entity {uuid: $uuid}) RETURN n, labels(n) AS labels",
                uuid=uuid,
            )
            record = result.single()
            if record:
                return self._node_to_dict(record["n"], record["labels"])
            return None

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    def get_nodes_by_uuids(self, uuids: List[str]) -> List[Dict[str, Any]]:
        """Batch retrieval of nodes by UUIDs in a single Cypher query."""
        if not uuids:
            return []

        cleaned_uuids = [u for u in set(uuids) if u]
        if not cleaned_uuids:
            return []

        def _read(tx):
            result = tx.run(
                """
                MATCH (n:Entity)
                WHERE n.uuid IN $uuids
                RETURN n, labels(n) AS labels
                """,
                uuids=cleaned_uuids,
            )
            return [self._node_to_dict(record["n"], record["labels"]) for record in result]

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    def get_node_edges(self, node_uuid: str) -> List[Dict[str, Any]]:
        """O(1) Cypher — NOT full scan + filter like the old Zep code."""
        def _read(tx):
            result = tx.run(
                """
                MATCH (n:Entity {uuid: $uuid})-[r:RELATION]-(m:Entity)
                RETURN r, startNode(r).uuid AS src_uuid, endNode(r).uuid AS tgt_uuid
                """,
                uuid=node_uuid,
            )
            return [
                self._edge_to_dict(record["r"], record["src_uuid"], record["tgt_uuid"])
                for record in result
            ]

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    def get_nodes_by_label(self, graph_id: str, label: str) -> List[Dict[str, Any]]:
        validate_label(label)

        def _read(tx):
            # Dynamic label in query (safe — validated against LABEL_PATTERN)
            query = f"""
                MATCH (n:Entity:`{label}` {{graph_id: $gid}})
                RETURN n, labels(n) AS labels
            """
            result = tx.run(query, gid=graph_id)
            return [self._node_to_dict(record["n"], record["labels"]) for record in result]

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    # ----------------------------------------------------------------
    # Read edges
    # ----------------------------------------------------------------

    def get_all_edges(self, graph_id: str) -> List[Dict[str, Any]]:
        def _read(tx):
            result = tx.run(
                """
                MATCH (src:Entity)-[r:RELATION {graph_id: $gid}]->(tgt:Entity)
                RETURN r, src.uuid AS src_uuid, tgt.uuid AS tgt_uuid
                ORDER BY r.created_at DESC
                """,
                gid=graph_id,
            )
            return [
                self._edge_to_dict(record["r"], record["src_uuid"], record["tgt_uuid"])
                for record in result
            ]

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    # ----------------------------------------------------------------
    # Search
    # ----------------------------------------------------------------

    def search(
        self,
        graph_id: str,
        query: str,
        limit: int = 10,
        scope: str = "edges",
    ):
        """
        Hybrid search — returns results matching the scope.

        Returns a dict with 'edges' and/or 'nodes' lists
        (callers like zep_tools will wrap into SearchResult).
        """
        result = {"edges": [], "nodes": [], "query": query}

        with self._driver.session() as session:
            if scope in ("edges", "both"):
                result["edges"] = self._search.search_edges(
                    session, graph_id, query, limit
                )

            if scope in ("nodes", "both"):
                result["nodes"] = self._search.search_nodes(
                    session, graph_id, query, limit
                )

        return result

    # ----------------------------------------------------------------
    # Graph info
    # ----------------------------------------------------------------

    def get_graph_info(self, graph_id: str) -> Dict[str, Any]:
        def _read(tx):
            # Count nodes
            node_result = tx.run(
                "MATCH (n:Entity {graph_id: $gid}) RETURN count(n) AS cnt",
                gid=graph_id,
            )
            node_count = node_result.single()["cnt"]

            # Count edges
            edge_result = tx.run(
                "MATCH ()-[r:RELATION {graph_id: $gid}]->() RETURN count(r) AS cnt",
                gid=graph_id,
            )
            edge_count = edge_result.single()["cnt"]

            # Distinct entity types
            label_result = tx.run(
                """
                MATCH (n:Entity {graph_id: $gid})
                UNWIND labels(n) AS lbl
                WITH lbl WHERE lbl <> 'Entity'
                RETURN DISTINCT lbl
                """,
                gid=graph_id,
            )
            entity_types = [record["lbl"] for record in label_result]

            return {
                "graph_id": graph_id,
                "node_count": node_count,
                "edge_count": edge_count,
                "entity_types": entity_types,
            }

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    def get_graph_data(self, graph_id: str) -> Dict[str, Any]:
        """
        Full graph dump with enriched edge format (for frontend).
        Includes derived fields: fact_type, source_node_name, target_node_name.
        """
        def _read(tx):
            # Get all nodes
            node_result = tx.run(
                """
                MATCH (n:Entity {graph_id: $gid})
                RETURN n, labels(n) AS labels
                """,
                gid=graph_id,
            )
            nodes = []
            node_map: Dict[str, str] = {}  # uuid -> name
            for record in node_result:
                nd = self._node_to_dict(record["n"], record["labels"])
                nodes.append(nd)
                node_map[nd["uuid"]] = nd["name"]

            # Get all edges with source/target node names (JOIN)
            edge_result = tx.run(
                """
                MATCH (src:Entity)-[r:RELATION {graph_id: $gid}]->(tgt:Entity)
                RETURN r, src.uuid AS src_uuid, tgt.uuid AS tgt_uuid,
                       src.name AS src_name, tgt.name AS tgt_name
                """,
                gid=graph_id,
            )
            edges = []
            for record in edge_result:
                ed = self._edge_to_dict(record["r"], record["src_uuid"], record["tgt_uuid"])
                # Enriched fields for frontend
                ed["fact_type"] = ed["name"]
                ed["source_node_name"] = record["src_name"] or ""
                ed["target_node_name"] = record["tgt_name"] or ""
                # Legacy alias
                ed["episodes"] = ed.get("episode_ids", [])
                edges.append(ed)

            return {
                "graph_id": graph_id,
                "nodes": nodes,
                "edges": edges,
                "node_count": len(nodes),
                "edge_count": len(edges),
            }

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    # ----------------------------------------------------------------
    # Dict conversion helpers
    # ----------------------------------------------------------------

    _node_to_dict = staticmethod(node_to_dict)
    _edge_to_dict = staticmethod(edge_to_dict)
