"""Regression tests for Issue #65: Neo4j transaction deadlock prevention on hot entity nodes."""

from unittest.mock import MagicMock, patch

from app.storage.neo4j_storage import Neo4jStorage
from tests.test_neo4j_storage_ingestion import InMemoryNeo4jDriver


def _make_storage(driver):
    mock_embedding = MagicMock()
    mock_embedding.embed_batch.side_effect = lambda texts: [[0.1, 0.2] for _ in texts]
    mock_ner = MagicMock()

    with patch("app.storage.neo4j_storage.GraphDatabase.driver", return_value=driver):
        storage = Neo4jStorage(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="password",
            embedding_service=mock_embedding,
            ner_extractor=mock_ner,
        )
    return storage


def test_persist_extraction_sorts_entities_deterministically():
    """
    Entities passed to UNWIND $entities MUST be sorted by name_lower to ensure
    consistent, deterministic lock acquisition ordering across transactions.
    """
    driver = InMemoryNeo4jDriver()
    storage = _make_storage(driver)

    # Input in non-alphabetical order: Zebra, Belgium, Apple, Turkey
    entities = [
        {"name": "Zebra", "type": "Animal"},
        {"name": "Belgium", "type": "Location"},
        {"name": "Apple", "type": "Organization"},
        {"name": "Turkey", "type": "Location"},
    ]

    storage.persist_extraction("g_test", "Chunk text", {"entities": entities, "relations": []})

    unwind_calls = [
        kwargs for query, kwargs in driver.queries
        if "UNWIND $entities AS e" in query
    ]
    assert len(unwind_calls) == 1
    unwind_entities = unwind_calls[0]["entities"]
    name_lowers = [e["name_lower"] for e in unwind_entities]

    # Must be sorted alphabetically to prevent circular lock dependency (deadlocks)
    assert name_lowers == ["apple", "belgium", "turkey", "zebra"]


def test_persist_extraction_sorts_relations_deterministically():
    """
    Relations passed to UNWIND $relations MUST be sorted deterministically
    by (source_uuid, target_uuid, name) to ensure consistent lock acquisition ordering.
    """
    driver = InMemoryNeo4jDriver()
    storage = _make_storage(driver)

    entities = [
        {"name": "NodeB", "type": "Concept"},
        {"name": "NodeA", "type": "Concept"},
        {"name": "NodeC", "type": "Concept"},
    ]
    relations = [
        {"source": "NodeC", "target": "NodeA", "type": "REL_Z", "fact": "C to A"},
        {"source": "NodeA", "target": "NodeB", "type": "REL_A", "fact": "A to B"},
        {"source": "NodeB", "target": "NodeC", "type": "REL_M", "fact": "B to C"},
    ]

    storage.persist_extraction("g_test", "Chunk text", {"entities": entities, "relations": relations})

    unwind_rel_calls = [
        kwargs for query, kwargs in driver.queries
        if "UNWIND $relations AS rel_data" in query
    ]
    assert len(unwind_rel_calls) == 1
    unwind_rels = unwind_rel_calls[0]["relations"]
    keys = [(r["source_uuid"], r["target_uuid"], r["name"]) for r in unwind_rels]
    assert keys == sorted(keys)
