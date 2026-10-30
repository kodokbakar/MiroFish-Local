"""
Tests for Issue #11:
1. Schema indexes: composite property index on (n:Entity {graph_id, name_lower})
   and relationship index on ()-[r:RELATION]-() ON (r.graph_id).
2. Cypher UNWIND batch ingestion: single write transaction per chunk, UNWIND entity merges,
   batch label setting, UNWIND relation merges, cross-chunk entity resolution.
3. Batch node retrieval API: get_nodes_by_uuids in GraphStorage and Neo4jStorage,
   get_nodes_detail in GraphToolsService, and batch node resolution in insight_forge.
4. Batch node retrieval in EntityReaderService.get_entity_detail.
5. Ontology caching during batch ingestion (add_text_batch and GraphBuilderService).
"""

import json
import unittest
from unittest.mock import MagicMock, patch

from app.storage import neo4j_schema
from app.storage.graph_storage import GraphStorage
from app.storage.neo4j_storage import Neo4jStorage
from app.services.graph_tools import GraphToolsService, NodeInfo, EdgeInfo
from app.services.entity_reader import EntityReader
from app.services.graph_builder import GraphBuilderService
from tests.test_neo4j_storage_ingestion import InMemoryNeo4jDriver, FakeResult


class TestStorageBatchUnwindAndIndexing(unittest.TestCase):
    """Test suite covering Issue #11 performance and schema optimizations."""

    def setUp(self):
        self.fake_driver = InMemoryNeo4jDriver()
        self.mock_embedding = MagicMock()
        self.mock_embedding.embed_batch.side_effect = lambda texts: [[0.1, 0.2] for _ in texts]
        self.mock_ner = MagicMock()

        with patch("app.storage.neo4j_storage.GraphDatabase.driver", return_value=self.fake_driver):
            self.storage = Neo4jStorage(
                uri="bolt://localhost:7687",
                user="neo4j",
                password="password",
                embedding_service=self.mock_embedding,
                ner_extractor=self.mock_ner,
            )

    # ----------------------------------------------------------------
    # 1. Schema Indexes
    # ----------------------------------------------------------------

    def test_schema_indexes_defined_and_included(self):
        """Verify schema index queries exist and are included in ALL_SCHEMA_QUERIES."""
        self.assertIn("CREATE INDEX entity_graph_name IF NOT EXISTS", neo4j_schema.CREATE_ENTITY_GRAPH_NAME_INDEX)
        self.assertIn("FOR (n:Entity) ON (n.graph_id, n.name_lower)", neo4j_schema.CREATE_ENTITY_GRAPH_NAME_INDEX)

        self.assertIn("CREATE INDEX relation_graph_id IF NOT EXISTS", neo4j_schema.CREATE_RELATION_GRAPH_ID_INDEX)
        self.assertIn("FOR ()-[r:RELATION]-() ON (r.graph_id)", neo4j_schema.CREATE_RELATION_GRAPH_ID_INDEX)

        self.assertIn(neo4j_schema.CREATE_ENTITY_GRAPH_NAME_INDEX, neo4j_schema.ALL_SCHEMA_QUERIES)
        self.assertIn(neo4j_schema.CREATE_RELATION_GRAPH_ID_INDEX, neo4j_schema.ALL_SCHEMA_QUERIES)

    # ----------------------------------------------------------------
    # 2. Single Write Transaction per Chunk & UNWIND batching
    # ----------------------------------------------------------------

    def test_add_text_executes_single_write_transaction(self):
        """Verify add_text consolidates chunk ingestion into exactly 1 execute_write call."""
        self.mock_ner.extract.return_value = {
            "entities": [
                {"name": "Alice", "type": "Person", "attributes": {"role": "Lead"}},
                {"name": "TechCorp", "type": "Company", "attributes": {"industry": "AI"}},
                {"name": "Bob", "type": "Person", "attributes": {"role": "Dev"}},
            ],
            "relations": [
                {"source": "Alice", "target": "TechCorp", "type": "WORKS_AT", "fact": "Alice works at TechCorp"},
                {"source": "Bob", "target": "TechCorp", "type": "WORKS_AT", "fact": "Bob works at TechCorp"},
            ],
        }

        call_with_retry_calls = []
        original_call_with_retry = self.storage._call_with_retry

        def counting_call_with_retry(fn, *args, **kwargs):
            call_with_retry_calls.append(fn)
            return original_call_with_retry(fn, *args, **kwargs)

        with patch.object(self.storage, "_call_with_retry", side_effect=counting_call_with_retry):
            ep_id = self.storage.add_text("test-graph", "Alice and Bob work at TechCorp.")

        self.assertIsNotNone(ep_id)
        # Ingestion MUST execute in a single write transaction (Issue #11 core requirement)
        self.assertEqual(len(call_with_retry_calls), 1)

        # Verify UNWIND queries occurred in the transaction
        queries = [q for q, _ in self.fake_driver.queries]
        self.assertTrue(any("UNWIND $entities AS e" in q for q in queries))
        self.assertTrue(any("UNWIND $relations AS rel_data" in q for q in queries))

    def test_add_text_deduplicates_and_merges_entities_within_chunk(self):
        """Verify entities appearing multiple times in one chunk are merged before UNWIND."""
        self.mock_ner.extract.return_value = {
            "entities": [
                {"name": "Alice", "type": "Person", "attributes": {"role": "Engineer"}},
                {"name": "Alice", "type": "Person", "attributes": {"team": "Core", "role": "Senior Engineer"}},
            ],
            "relations": [],
        }

        self.storage.add_text("test-graph", "Alice is an Engineer and Senior Engineer on Core team.")

        entity_unwind_queries = [
            (q, params) for q, params in self.fake_driver.queries
            if "UNWIND $entities AS e" in q
        ]
        self.assertEqual(len(entity_unwind_queries), 1)
        _, params = entity_unwind_queries[0]

        # UNWIND entities list should have only 1 entry for 'alice'
        self.assertEqual(len(params["entities"]), 1)
        e = params["entities"][0]
        self.assertEqual(e["name_lower"], "alice")
        attrs = json.loads(e["attrs_json"])
        self.assertEqual(attrs, {"role": "Senior Engineer", "team": "Core"})

    def test_add_text_deduplicates_relations_within_chunk(self):
        """Verify duplicate relations in the same chunk are deduplicated and merged before UNWIND."""
        self.mock_ner.extract.return_value = {
            "entities": [
                {"name": "Alice", "type": "Person", "attributes": {}},
                {"name": "Bob", "type": "Person", "attributes": {}},
            ],
            "relations": [
                {"source": "Alice", "target": "Bob", "type": "COLLABORATES", "fact": ""},
                {"source": "Alice", "target": "Bob", "type": "COLLABORATES", "fact": "Alice collaborates with Bob on AI"},
            ],
        }

        self.storage.add_text("test-graph", "Alice collaborates with Bob.")

        relation_unwind_queries = [
            (q, params) for q, params in self.fake_driver.queries
            if "UNWIND $relations AS rel_data" in q
        ]
        self.assertEqual(len(relation_unwind_queries), 1)
        _, params = relation_unwind_queries[0]

        # Relations list should have only 1 entry
        self.assertEqual(len(params["relations"]), 1)
        self.assertEqual(params["relations"][0]["name"], "COLLABORATES")
        self.assertEqual(params["relations"][0]["fact"], "Alice collaborates with Bob on AI")

    def test_add_text_resolves_relations_for_prior_chunk_entities(self):
        """Verify relations referencing entities from earlier chunks resolve UUIDs from DB."""
        # Chunk 1: Create Alice
        self.mock_ner.extract.return_value = {
            "entities": [{"name": "Alice", "type": "Person", "attributes": {}}],
            "relations": [],
        }
        self.storage.add_text("test-graph", "Alice arrives.")
        alice_uuid = self.fake_driver.entities[("test-graph", "alice")]["uuid"]

        # Chunk 2: Create Bob and relation Alice -> Bob (Alice not in chunk 2 NER entities)
        self.mock_ner.extract.return_value = {
            "entities": [{"name": "Bob", "type": "Person", "attributes": {}}],
            "relations": [
                {"source": "Alice", "target": "Bob", "type": "HIRES", "fact": "Alice hires Bob"},
            ],
        }
        self.storage.add_text("test-graph", "Bob is hired by Alice.")

        bob_uuid = self.fake_driver.entities[("test-graph", "bob")]["uuid"]
        rel_key = (alice_uuid, bob_uuid, "test-graph", "HIRES")
        self.assertIn(rel_key, self.fake_driver.relations)
        self.assertEqual(self.fake_driver.relations[rel_key]["fact"], "Alice hires Bob")

    # ----------------------------------------------------------------
    # 3. Batch Node Retrieval API (get_nodes_by_uuids)
    # ----------------------------------------------------------------

    def test_get_nodes_by_uuids_empty_input(self):
        """Verify get_nodes_by_uuids returns [] without querying when input is empty."""
        res = self.storage.get_nodes_by_uuids([])
        self.assertEqual(res, [])
        res2 = self.storage.get_nodes_by_uuids(["", None])
        self.assertEqual(res2, [])

    def test_get_nodes_by_uuids_batch_cypher_retrieval(self):
        """Verify get_nodes_by_uuids retrieves multiple nodes in 1 Cypher query."""
        self.fake_driver.entities[("test-graph", "alice")] = {
            "uuid": "u-alice",
            "name": "Alice",
            "name_lower": "alice",
            "summary": "Alice summary",
            "attributes_json": '{"role": "Lead"}',
            "created_at": "2026-01-01",
            "labels": ["Entity", "Person"],
        }
        self.fake_driver.entities[("test-graph", "bob")] = {
            "uuid": "u-bob",
            "name": "Bob",
            "name_lower": "bob",
            "summary": "Bob summary",
            "attributes_json": '{"role": "Dev"}',
            "created_at": "2026-01-01",
            "labels": ["Entity", "Person"],
        }

        nodes = self.storage.get_nodes_by_uuids(["u-alice", "u-bob", "u-alice"])
        self.assertEqual(len(nodes), 2)
        node_map = {n["uuid"]: n for n in nodes}

        self.assertIn("u-alice", node_map)
        self.assertEqual(node_map["u-alice"]["name"], "Alice")
        self.assertEqual(node_map["u-alice"]["attributes"], {"role": "Lead"})
        self.assertIn("Person", node_map["u-alice"]["labels"])

        self.assertIn("u-bob", node_map)
        self.assertEqual(node_map["u-bob"]["name"], "Bob")
        self.assertEqual(node_map["u-bob"]["attributes"], {"role": "Dev"})

    # ----------------------------------------------------------------
    # 4. GraphToolsService.get_nodes_detail & insight_forge Batching
    # ----------------------------------------------------------------

    def test_graph_tools_get_nodes_detail(self):
        """Verify GraphToolsService.get_nodes_detail maps batch dicts to NodeInfo instances."""
        mock_storage = MagicMock(spec=GraphStorage)
        mock_storage.get_nodes_by_uuids.return_value = [
            {
                "uuid": "u-1",
                "name": "Node One",
                "labels": ["Person"],
                "summary": "Summary 1",
                "attributes": {"k": "v"},
            },
            {
                "uuid": "u-2",
                "name": "Node Two",
                "labels": ["Company"],
                "summary": "Summary 2",
                "attributes": {},
            },
        ]

        tools = GraphToolsService(storage=mock_storage)
        details = tools.get_nodes_detail(["u-1", "u-2"])

        self.assertEqual(len(details), 2)
        self.assertIsInstance(details[0], NodeInfo)
        self.assertEqual(details[0].uuid, "u-1")
        self.assertEqual(details[0].name, "Node One")
        self.assertEqual(details[0].labels, ["Person"])
        self.assertEqual(details[1].uuid, "u-2")

        mock_storage.get_nodes_by_uuids.assert_called_once_with(["u-1", "u-2"])

    def test_insight_forge_uses_batch_node_retrieval(self):
        """Verify insight_forge resolves entities in 1 batch query instead of an N+1 loop."""
        mock_storage = MagicMock(spec=GraphStorage)

        # Mock search returning edges
        mock_storage.search.return_value = {
            "edges": [
                {
                    "source_node_uuid": "u-alice",
                    "target_node_uuid": "u-techcorp",
                    "name": "WORKS_AT",
                    "fact": "Alice works at TechCorp",
                    "created_at": "2026-01-01",
                },
                {
                    "source_node_uuid": "u-bob",
                    "target_node_uuid": "u-techcorp",
                    "name": "WORKS_AT",
                    "fact": "Bob works at TechCorp",
                    "created_at": "2026-01-01",
                },
            ],
            "nodes": [],
        }

        mock_storage.get_nodes_by_uuids.return_value = [
            {"uuid": "u-alice", "name": "Alice", "labels": ["Entity", "Person"], "summary": "Alice", "attributes": {}},
            {"uuid": "u-bob", "name": "Bob", "labels": ["Entity", "Person"], "summary": "Bob", "attributes": {}},
            {"uuid": "u-techcorp", "name": "TechCorp", "labels": ["Entity", "Company"], "summary": "TechCorp", "attributes": {}},
        ]

        tools = GraphToolsService(storage=mock_storage)
        res = tools.insight_forge("test-graph", query="TechCorp team", simulation_requirement="Find members")

        # Ensure get_nodes_by_uuids was called (batch)
        mock_storage.get_nodes_by_uuids.assert_called_once()
        requested_uuids = set(mock_storage.get_nodes_by_uuids.call_args[0][0])
        self.assertEqual(requested_uuids, {"u-alice", "u-bob", "u-techcorp"})

        # storage.get_node must NOT have been called in an N+1 loop
        mock_storage.get_node.assert_not_called()

        # Check results populated correctly
        self.assertEqual(res.total_entities, 3)
        self.assertEqual(res.total_relationships, 2)
        chain_strs = res.relationship_chains
        self.assertTrue(any("Alice --[WORKS_AT]--> TechCorp" in c for c in chain_strs))
        self.assertTrue(any("Bob --[WORKS_AT]--> TechCorp" in c for c in chain_strs))

    # ----------------------------------------------------------------
    # 5. EntityReader Batching
    # ----------------------------------------------------------------

    def test_entity_reader_uses_batch_node_retrieval(self):
        """Verify EntityReader.get_entity_detail retrieves related nodes in batch."""
        mock_storage = MagicMock(spec=GraphStorage)
        mock_storage.get_node.return_value = {
            "uuid": "u-main",
            "name": "Main Entity",
            "labels": ["Person"],
            "summary": "Main person",
            "attributes": {},
        }
        mock_storage.get_node_edges.return_value = [
            {"name": "FRIEND_OF", "fact": "Friends", "target_node_uuid": "u-rel1", "source_node_uuid": "u-main"},
            {"name": "COLLEAGUE_OF", "fact": "Colleagues", "target_node_uuid": "u-rel2", "source_node_uuid": "u-main"},
        ]
        mock_storage.get_nodes_by_uuids.return_value = [
            {"uuid": "u-rel1", "name": "Friend 1", "labels": ["Person"], "summary": "Friend"},
            {"uuid": "u-rel2", "name": "Colleague 2", "labels": ["Person"], "summary": "Colleague"},
        ]

        reader = EntityReader(storage=mock_storage)
        node = reader.get_entity_with_context("test-graph", "u-main")

        self.assertIsNotNone(node)
        self.assertEqual(node.name, "Main Entity")
        self.assertEqual(len(node.related_nodes), 2)

        # Batch retrieval must be called with both UUIDs
        mock_storage.get_nodes_by_uuids.assert_called_once()
        called_uuids = set(mock_storage.get_nodes_by_uuids.call_args[0][0])
        self.assertEqual(called_uuids, {"u-rel1", "u-rel2"})

    # ----------------------------------------------------------------
    # 6. Ontology Caching During Batch Ingestion
    # ----------------------------------------------------------------

    def test_add_text_batch_caches_ontology(self):
        """Verify Neo4jStorage.add_text_batch fetches ontology only once for all chunks."""
        storage = Neo4jStorage.__new__(Neo4jStorage)
        storage.get_ontology = MagicMock(return_value={"entity_types": ["Person"]})
        storage.add_text = MagicMock(return_value="mock-ep-id")

        chunks = ["Chunk 1 text", "Chunk 2 text", "Chunk 3 text"]
        ep_ids = storage.add_text_batch("test-graph", chunks, batch_size=2)

        self.assertEqual(len(ep_ids), 3)
        # get_ontology should be called once, NOT per chunk
        storage.get_ontology.assert_called_once_with("test-graph")
        # add_text should receive the cached ontology parameter
        self.assertEqual(storage.add_text.call_count, 3)
        for call in storage.add_text.call_args_list:
            self.assertEqual(call[1]["ontology"], {"entity_types": ["Person"]})

    def test_graph_builder_passes_cached_ontology(self):
        """Verify GraphBuilderService.add_text_batches forwards ontology to storage.add_text."""
        mock_storage = MagicMock(spec=GraphStorage)
        mock_storage.add_text.return_value = "ep-uuid"

        builder = GraphBuilderService(storage=mock_storage)
        ontology = {"entity_types": ["Event"]}
        chunks = ["Batch chunk 1", "Batch chunk 2"]

        builder.add_text_batches("test-graph", chunks, batch_size=2, ontology=ontology)

        self.assertEqual(mock_storage.add_text.call_count, 2)
        for call in mock_storage.add_text.call_args_list:
            self.assertEqual(call[1]["ontology"], ontology)


if __name__ == "__main__":
    unittest.main()
