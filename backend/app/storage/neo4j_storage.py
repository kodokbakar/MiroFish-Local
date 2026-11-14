"""
Neo4jStorage — Neo4j Community Edition implementation of GraphStorage.

Replaces all Zep Cloud API calls with local Neo4j Cypher queries.
Includes: CRUD, NER/RE-based text ingestion, hybrid search, retry logic.
"""

import json
import logging
import random
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional, Callable

from neo4j import GraphDatabase
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


def _normalize_embedding(emb: Optional[List[float]]) -> Optional[List[float]]:
    """Normalize embedding: return None if missing, empty, or all zeros (prevents Neo4j cosine index division by zero)."""
    if not emb or not any(v != 0.0 for v in emb):
        return None
    return emb


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
        self._write_locks: Dict[str, threading.Lock] = defaultdict(threading.Lock)
        self._write_locks_guard = threading.Lock()

        # Initialize schema (indexes, constraints)
        self._ensure_schema()

    def _get_write_lock(self, graph_id: str) -> threading.Lock:
        """Get or create a mutex for serialized writes on a specific graph."""
        if not hasattr(self, "_write_locks_guard"):
            self._write_locks_guard = threading.Lock()
        if not hasattr(self, "_write_locks"):
            self._write_locks = defaultdict(threading.Lock)
        with self._write_locks_guard:
            return self._write_locks[graph_id]

    def close(self):
        """Close the Neo4j driver connection."""
        self._driver.close()

    def check_health(self) -> Dict[str, Any]:
        """
        Check Neo4j connectivity and index status for telemetry.
        Returns a structured dictionary with status ('ok', 'degraded', 'unavailable').
        """
        start = time.perf_counter()
        try:
            with self._driver.session() as session:
                res = session.run("SHOW INDEXES YIELD name, state, populationPercent")
                records = list(res)
                online_count = sum(1 for r in records if r.get("state") == "ONLINE")
                populating_count = sum(1 for r in records if r.get("state") == "POPULATING")
                failed_count = sum(1 for r in records if r.get("state") == "FAILED")
                latency_ms = round((time.perf_counter() - start) * 1000, 2)

                status = "ok"
                if failed_count > 0 or populating_count > 0:
                    status = "degraded"

                return {
                    "status": status,
                    "latency_ms": latency_ms,
                    "indexes": {
                        "online": online_count,
                        "populating": populating_count,
                        "failed": failed_count,
                    },
                }
        except Exception as e:
            return {
                "status": "unavailable",
                "error": str(e),
                "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            }

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
                jitter = random.uniform(0.0, 0.5)
                wait = self.RETRY_DELAY_BASE * (2 ** attempt) + jitter
                logger.warning(
                    f"Neo4j transient error (attempt {attempt + 1}/{self.MAX_RETRIES}), "
                    f"retrying in {wait:.2f}s: {e}"
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
        simulation_id: Optional[str] = None,
        round_num: int = 0,
    ) -> str:
        """Process text: NER/RE → batch embed → create nodes/edges → return episode_id."""
        if ontology is None:
            ontology = self.get_ontology(graph_id)
        extraction = self.extract_text(text, ontology)
        if simulation_id is None and round_num == 0:
            return self.persist_extraction(graph_id, text, extraction)
        return self.persist_extraction(
            graph_id,
            text,
            extraction,
            simulation_id=simulation_id,
            round_num=round_num,
        )

    def extract_text(self, text: str, ontology: Dict[str, Any]) -> Dict[str, Any]:
        """Extract without touching Neo4j, so calls can overlap safely."""
        started = time.perf_counter()
        extraction = self._ner.extract(text, ontology)
        logger.info(
            "[add_text] NER done: %d entities, %d relations, %.3fs",
            len(extraction["entities"]), len(extraction["relations"]),
            time.perf_counter() - started,
        )
        return extraction

    def persist_extraction(
        self,
        graph_id: str,
        text: str,
        extraction: Dict[str, Any],
        simulation_id: Optional[str] = None,
        round_num: int = 0,
    ) -> str:
        """Embed and write one successful extraction in a single graph transaction."""
        if (
            not isinstance(extraction, dict)
            or not isinstance(extraction.get("entities"), list)
            or not isinstance(extraction.get("relations"), list)
        ):
            raise ValueError("Invalid NER extraction result")
        episode_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()
        entities = extraction.get("entities", [])
        relations = extraction.get("relations", [])

        # --- Batch embed all texts at once ---
        entity_summaries = [f"{e['name']} ({e['type']})" for e in entities]
        fact_texts = [r.get("fact", f"{r['source']} {r['type']} {r['target']}") for r in relations]
        all_texts_to_embed = entity_summaries + fact_texts

        all_embeddings: list = []
        embed_started = time.perf_counter()
        if all_texts_to_embed:
            logger.info(f"[add_text] Batch-embedding {len(all_texts_to_embed)} texts...")
            try:
                all_embeddings = self._embedding.embed_batch(all_texts_to_embed)
            except Exception as e:
                logger.warning(f"[add_text] Batch embedding failed, falling back to empty: {e}")
                all_embeddings = [None for _ in all_texts_to_embed]

        entity_embeddings = all_embeddings[:len(entities)]
        relation_embeddings = all_embeddings[len(entities):]
        embed_seconds = time.perf_counter() - embed_started
        write_started = time.perf_counter()

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
            raw_emb = entity_embeddings[idx] if idx < len(entity_embeddings) else None
            embedding = _normalize_embedding(raw_emb)

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

        with self._get_write_lock(graph_id), self._driver.session() as session:
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
                    # Sort deterministically by name_lower to prevent circular wait deadlocks
                    entities_data.sort(key=lambda x: x["name_lower"])

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
                            n.created_at = $now,
                            n.simulation_id = $simulation_id,
                            n.created_round = $round_num
                        ON MATCH SET
                            n.summary = CASE WHEN n.summary = '' OR n.summary IS NULL
                                THEN e.summary ELSE n.summary END,
                            n.attributes_json = e.attrs_json,
                            n.embedding = CASE WHEN e.embedding IS NOT NULL AND size(e.embedding) > 0 THEN e.embedding ELSE n.embedding END
                        RETURN e.name_lower AS name_lower, n.uuid AS uuid
                        """,
                        gid=graph_id,
                        entities=entities_data,
                        now=now,
                        simulation_id=simulation_id,
                        round_num=round_num,
                    )
                    if res:
                        for rec in res:
                            try:
                                if rec and rec["name_lower"] and rec["uuid"]:
                                    uuid_map[rec["name_lower"]] = rec["uuid"]
                            except Exception:
                                pass

                # 3. Batch set labels grouped by entity type in deterministic order
                for etype in sorted(type_to_names.keys()):
                    name_list = sorted(type_to_names[etype])
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

                        raw_fact_emb = relation_embeddings[idx] if idx < len(relation_embeddings) else None
                        fact_emb = _normalize_embedding(raw_fact_emb)
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
                            if not prev.get("fact_embedding") and fact_emb:
                                prev["fact_embedding"] = fact_emb

                    prepared_relations = list(deduped_relations.values())
                    if prepared_relations:
                        # Sort deterministically by (source_uuid, target_uuid, name) to prevent deadlocks
                        prepared_relations.sort(
                            key=lambda x: (x["source_uuid"], x["target_uuid"], x["name"])
                        )
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
                                r.expired_at = null,
                                r.simulation_id = $simulation_id,
                                r.created_round = $round_num
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
                            simulation_id=simulation_id,
                            round_num=round_num,
                        )

            self._call_with_retry(session.execute_write, _persist_chunk)

        logger.info(
            "[add_text] Chunk done: episode=%s embed=%.3fs write=%.3fs",
            episode_id, embed_seconds, time.perf_counter() - write_started,
        )
        return episode_id

    def persist_structured_activities(
        self,
        graph_id: str,
        activities: List[Any],
        simulation_id: Optional[str] = None,
        round_num: int = 0,
    ) -> str:
        """
        Fast-path graph ingestion for structured social actions.
        Directly creates actor/target Entity nodes and action RELATION edges via Cypher UNWIND,
        completely bypassing LLM NER.
        """
        if not activities:
            return ""

        episode_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc).isoformat()

        # If simulation_id or round_num are not provided as arguments, try extracting from activities
        if simulation_id is None:
            for act in activities:
                s = getattr(act, "simulation_id", None) if not isinstance(act, dict) else act.get("simulation_id")
                if s:
                    simulation_id = s
                    break
        if round_num == 0:
            for act in activities:
                r = getattr(act, "round_num", None) if not isinstance(act, dict) else act.get("round_num")
                if r:
                    round_num = r
                    break

        # Build structured entities and relations directly
        entities_dict: Dict[str, Dict[str, Any]] = {}
        relations_list: List[Dict[str, Any]] = []

        action_rel_map = {
            "LIKE_POST": "LIKED",
            "DISLIKE_POST": "DISLIKED",
            "CREATE_POST": "POSTED",
            "REPOST": "REPOSTED",
            "QUOTE_POST": "QUOTED",
            "FOLLOW": "FOLLOWS",
            "CREATE_COMMENT": "COMMENTED_ON",
            "LIKE_COMMENT": "LIKED_COMMENT",
            "DISLIKE_COMMENT": "DISLIKED_COMMENT",
            "MUTE": "MUTED",
        }

        for act in activities:
            # Handle both dataclass/object and dict
            agent_name = getattr(act, "agent_name", None) or (act.get("agent_name") if isinstance(act, dict) else "")
            action_type = getattr(act, "action_type", None) or (act.get("action_type") if isinstance(act, dict) else "")
            action_args = getattr(act, "action_args", None) or (act.get("action_args") if isinstance(act, dict) else {})
            if not isinstance(action_args, dict):
                action_args = {}

            if not agent_name:
                continue

            actor_lower = agent_name.lower()
            if actor_lower not in entities_dict:
                entities_dict[actor_lower] = {
                    "name": agent_name,
                    "name_lower": actor_lower,
                    "type": "Person",
                    "summary": f"Agent {agent_name}",
                    "uuid": str(uuid.uuid4()),
                }

            # Check target entity (e.g. target author, post, or followed user)
            target_name = (
                action_args.get("post_author_name")
                or action_args.get("original_author_name")
                or action_args.get("target_user")
            )
            rel_type = action_rel_map.get(action_type)

            if target_name and rel_type:
                target_lower = target_name.lower()
                if target_lower not in entities_dict:
                    entities_dict[target_lower] = {
                        "name": target_name,
                        "name_lower": target_lower,
                        "type": "Person",
                        "summary": f"User {target_name}",
                        "uuid": str(uuid.uuid4()),
                    }

                fact_text = f"{agent_name} {rel_type.lower()} {target_name}"
                relations_list.append({
                    "uuid": str(uuid.uuid4()),
                    "source_lower": actor_lower,
                    "target_lower": target_lower,
                    "name": rel_type,
                    "fact": fact_text,
                })

        entities_data = list(entities_dict.values())
        # Sort deterministically to prevent deadlocks
        entities_data.sort(key=lambda x: x["name_lower"])
        relations_list.sort(key=lambda x: (x["source_lower"], x["target_lower"], x["name"]))

        with self._get_write_lock(graph_id), self._driver.session() as session:
            def _write_activities(tx):
                # 1. Episode node
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
                    data=f"Structured batch: {len(activities)} activities",
                    created_at=now,
                )

                # 2. Merge entities
                if entities_data:
                    tx.run(
                        """
                        UNWIND $entities AS e
                        MERGE (n:Entity {graph_id: $gid, name_lower: e.name_lower})
                        ON CREATE SET
                            n.uuid = e.uuid,
                            n.name = e.name,
                            n.summary = e.summary,
                            n.attributes_json = '{}',
                            n.created_at = $now,
                            n.simulation_id = $simulation_id,
                            n.created_round = $round_num
                        SET n:Person
                        """,
                        gid=graph_id,
                        entities=entities_data,
                        now=now,
                        simulation_id=simulation_id,
                        round_num=round_num,
                    )

                # 3. Merge relations
                if relations_list:
                    tx.run(
                        """
                        UNWIND $relations AS r
                        MATCH (src:Entity {graph_id: $gid, name_lower: r.source_lower})
                        MATCH (tgt:Entity {graph_id: $gid, name_lower: r.target_lower})
                        MERGE (src)-[rel:RELATION {graph_id: $gid, name: r.name}]->(tgt)
                        ON CREATE SET
                            rel.uuid = r.uuid,
                            rel.fact = r.fact,
                            rel.episode_ids = [$episode_id],
                            rel.created_at = $now,
                            rel.simulation_id = $simulation_id,
                            rel.created_round = $round_num
                        ON MATCH SET
                            rel.episode_ids = CASE
                                WHEN rel.episode_ids IS NULL THEN [$episode_id]
                                WHEN $episode_id IN rel.episode_ids THEN rel.episode_ids
                                ELSE rel.episode_ids + [$episode_id]
                            END
                        """,
                        gid=graph_id,
                        relations=relations_list,
                        episode_id=episode_id,
                        now=now,
                        simulation_id=simulation_id,
                        round_num=round_num,
                    )

            self._call_with_retry(session.execute_write, _write_activities)

        logger.info(
            f"[fast_path] Ingested {len(activities)} activities into graph {graph_id} (episode={episode_id})"
        )
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

    def get_graph_data(
        self, graph_id: str, include_embeddings: bool = False
    ) -> Dict[str, Any]:
        """
        Full graph dump with enriched edge format (for frontend).
        Includes derived fields: fact_type, source_node_name, target_node_name.
        When include_embeddings is False, raw vector embeddings are pruned via
        map projection over Bolt to reduce network serialization overhead.
        """
        def _read(tx):
            # Get all nodes
            if include_embeddings:
                node_query = """
                MATCH (n:Entity {graph_id: $gid})
                RETURN n, labels(n) AS labels
                """
            else:
                node_query = """
                MATCH (n:Entity {graph_id: $gid})
                RETURN n {
                    .uuid,
                    .name,
                    .summary,
                    .attributes_json,
                    .created_at,
                    .graph_id,
                    .degree,
                    .community_id
                } AS n, labels(n) AS labels
                """

            node_result = tx.run(node_query, gid=graph_id)
            nodes = []
            node_map: Dict[str, str] = {}  # uuid -> name
            for record in node_result:
                nd = self._node_to_dict(record["n"], record["labels"])
                nodes.append(nd)
                node_map[nd["uuid"]] = nd["name"]

            # Get all edges with source/target node names (JOIN)
            if include_embeddings:
                edge_query = """
                MATCH (src:Entity)-[r:RELATION {graph_id: $gid}]->(tgt:Entity)
                RETURN r, src.uuid AS src_uuid, tgt.uuid AS tgt_uuid,
                       src.name AS src_name, tgt.name AS tgt_name
                """
            else:
                edge_query = """
                MATCH (src:Entity)-[r:RELATION {graph_id: $gid}]->(tgt:Entity)
                RETURN r {
                    .uuid,
                    .name,
                    .fact,
                    .attributes_json,
                    .created_at,
                    .valid_at,
                    .invalid_at,
                    .expired_at,
                    .episode_ids,
                    .graph_id
                } AS r, src.uuid AS src_uuid, tgt.uuid AS tgt_uuid,
                       src.name AS src_name, tgt.name AS tgt_name
                """

            edge_result = tx.run(edge_query, gid=graph_id)
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

    def get_graph_timeline(
        self, simulation_id: str, graph_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Get graph evolution timeline grouped by simulation round.
        Returns dict with:
            simulation_id: str
            rounds: list of dicts with:
                round: int
                nodes_added: list of node dicts
                edges_added: list of edge dicts
                cumulative_nodes: int
                cumulative_edges: int
        """
        def _read(tx):
            if graph_id:
                node_query = """
                MATCH (n:Entity {graph_id: $gid})
                WHERE n.simulation_id = $sim_id OR n.created_round = 0 OR n.simulation_id IS NULL
                RETURN n, labels(n) AS labels
                """
                edge_query = """
                MATCH (src:Entity)-[r:RELATION {graph_id: $gid}]->(tgt:Entity)
                WHERE r.simulation_id = $sim_id OR r.created_round = 0 OR r.simulation_id IS NULL
                RETURN r, src.uuid AS src_uuid, tgt.uuid AS tgt_uuid,
                       src.name AS src_name, tgt.name AS tgt_name
                """
            else:
                node_query = """
                MATCH (n:Entity)
                WHERE n.simulation_id = $sim_id
                RETURN n, labels(n) AS labels
                """
                edge_query = """
                MATCH (src:Entity)-[r:RELATION]->(tgt:Entity)
                WHERE r.simulation_id = $sim_id
                RETURN r, src.uuid AS src_uuid, tgt.uuid AS tgt_uuid,
                       src.name AS src_name, tgt.name AS tgt_name
                """

            node_result = tx.run(node_query, gid=graph_id, sim_id=simulation_id)
            nodes_by_round: Dict[int, List[Dict[str, Any]]] = {}
            seen_nodes: set = set()
            for record in node_result:
                nd = self._node_to_dict(record["n"], record["labels"])
                if nd["uuid"] in seen_nodes:
                    continue
                seen_nodes.add(nd["uuid"])
                r_num = nd.get("created_round") or 0
                nodes_by_round.setdefault(r_num, []).append(nd)

            edge_result = tx.run(edge_query, gid=graph_id, sim_id=simulation_id)
            edges_by_round: Dict[int, List[Dict[str, Any]]] = {}
            seen_edges: set = set()
            for record in edge_result:
                ed = self._edge_to_dict(record["r"], record["src_uuid"], record["tgt_uuid"])
                if ed["uuid"] in seen_edges:
                    continue
                seen_edges.add(ed["uuid"])
                ed["fact_type"] = ed["name"]
                ed["source_node_name"] = record["src_name"] or ""
                ed["target_node_name"] = record["tgt_name"] or ""
                ed["episodes"] = ed.get("episode_ids", [])
                r_num = ed.get("created_round") or 0
                edges_by_round.setdefault(r_num, []).append(ed)

            all_rounds = sorted(set(nodes_by_round.keys()) | set(edges_by_round.keys()) | {0})
            rounds_list = []
            cum_nodes = 0
            cum_edges = 0
            for r in all_rounds:
                n_added = nodes_by_round.get(r, [])
                e_added = edges_by_round.get(r, [])
                cum_nodes += len(n_added)
                cum_edges += len(e_added)
                rounds_list.append({
                    "round": r,
                    "nodes_added": n_added,
                    "edges_added": e_added,
                    "cumulative_nodes": cum_nodes,
                    "cumulative_edges": cum_edges,
                })

            return {
                "simulation_id": simulation_id,
                "rounds": rounds_list,
            }

        with self._driver.session() as session:
            return self._call_with_retry(session.execute_read, _read)

    def detect_communities(
        self, graph_id: str, algorithm: str = "louvain"
    ) -> Dict[str, Any]:
        """
        Detect entity communities in graph and persist community_id to nodes.
        Checks for Neo4j GDS; if absent or fails, falls back cleanly to NetworkX.
        """
        if self._check_gds_available():
            try:
                return self._detect_communities_gds(graph_id, algorithm)
            except Exception as e:
                logger.warning(f"Neo4j GDS community detection failed, falling back to NetworkX: {e}")

        return self._detect_communities_networkx(graph_id)

    def _check_gds_available(self) -> bool:
        def _read(tx):
            res = tx.run("SHOW PROCEDURES YIELD name WHERE name = 'gds.louvain.stream'")
            return res.single() is not None

        try:
            with self._driver.session() as session:
                return self._call_with_retry(session.execute_read, _read)
        except Exception:
            return False

    def _detect_communities_gds(self, graph_id: str, algorithm: str) -> Dict[str, Any]:
        raise NotImplementedError("GDS louvain stream not configured in Community Edition")

    def _detect_communities_networkx(self, graph_id: str) -> Dict[str, Any]:
        import networkx as nx
        import networkx.algorithms.community as nx_comm

        def _fetch_graph_data(tx):
            node_res = tx.run(
                "MATCH (n:Entity {graph_id: $gid}) RETURN n.uuid AS uuid, n.name AS name, labels(n) AS labels",
                gid=graph_id,
            )
            nodes = [
                {
                    "uuid": r["uuid"],
                    "name": r["name"] or "",
                    "labels": [lbl for lbl in (r["labels"] or []) if lbl != "Entity"],
                }
                for r in node_res
                if r["uuid"]
            ]

            edge_res = tx.run(
                """
                MATCH (src:Entity)-[r:RELATION {graph_id: $gid}]->(tgt:Entity)
                RETURN src.uuid AS source, tgt.uuid AS target, r.weight AS weight
                """,
                gid=graph_id,
            )
            edges = [
                {
                    "source": r["source"],
                    "target": r["target"],
                    "weight": float(r["weight"]) if r["weight"] is not None else 1.0,
                }
                for r in edge_res
                if r["source"] and r["target"]
            ]
            return nodes, edges

        with self._driver.session() as session:
            nodes, edges = self._call_with_retry(session.execute_read, _fetch_graph_data)

        if not nodes:
            return {"communities": [], "total_communities": 0, "total_nodes": 0}

        G = nx.Graph()
        node_lookup = {}
        for n in nodes:
            G.add_node(n["uuid"], name=n["name"], labels=n["labels"])
            node_lookup[n["uuid"]] = n

        for e in edges:
            if G.has_edge(e["source"], e["target"]):
                G[e["source"]][e["target"]]["weight"] += e["weight"]
            else:
                G.add_edge(e["source"], e["target"], weight=e["weight"])

        community_sets = nx_comm.louvain_communities(G, seed=42)
        # Sort communities deterministically by size descending, then first node uuid
        sorted_communities = sorted(
            community_sets,
            key=lambda c: (-len(c), min(c) if c else ""),
        )

        updates = []
        communities_summary = []
        for cid, members in enumerate(sorted_communities):
            for node_uuid in members:
                updates.append({"uuid": node_uuid, "community_id": cid})

            # Calculate degrees within community to pick top entities
            subG = G.subgraph(members)
            degrees = dict(subG.degree(weight="weight"))
            top_member_uuids = sorted(members, key=lambda u: (-degrees.get(u, 0), node_lookup[u]["name"]))[:5]
            top_entities = [node_lookup[u]["name"] for u in top_member_uuids if node_lookup.get(u)]

            # Dominant labels
            label_counts = defaultdict(int)
            for u in members:
                for lbl in node_lookup.get(u, {}).get("labels", []):
                    label_counts[lbl] += 1
            sorted_labels = sorted(label_counts.items(), key=lambda x: (-x[1], x[0]))
            dominant_labels = [lbl for lbl, _ in sorted_labels[:3]]

            communities_summary.append({
                "id": cid,
                "size": len(members),
                "top_entities": top_entities,
                "dominant_labels": dominant_labels,
            })

        if updates:
            def _persist(tx):
                tx.run(
                    """
                    UNWIND $updates AS u
                    MATCH (n:Entity {uuid: u.uuid, graph_id: $gid})
                    SET n.community_id = u.community_id
                    """,
                    updates=updates,
                    gid=graph_id,
                )

            with self._driver.session() as session:
                self._call_with_retry(session.execute_write, _persist)

        return {
            "communities": communities_summary,
            "total_communities": len(communities_summary),
            "total_nodes": len(nodes),
        }

    # ----------------------------------------------------------------
    # Dict conversion helpers
    # ----------------------------------------------------------------

    _node_to_dict = staticmethod(node_to_dict)
    _edge_to_dict = staticmethod(edge_to_dict)
