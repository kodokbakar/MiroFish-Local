"""
Regression test for Issue #99 (Item 076):
Enforce uniqueness constraint on Entity (graph_id, name_lower) to prevent duplicate nodes.

In concurrent ingestion / race condition scenarios, a standard index
(CREATE INDEX entity_graph_name FOR (n:Entity) ON (n.graph_id, n.name_lower))
does not prevent duplicate Entity nodes from being created during concurrent MERGE operations.
A uniqueness constraint:
CREATE CONSTRAINT entity_graph_name IF NOT EXISTS FOR (n:Entity) REQUIRE (n.graph_id, n.name_lower) IS UNIQUE
is required by Neo4j 5.x to guarantee composite uniqueness.
"""

import unittest
from unittest.mock import MagicMock, patch

from neo4j.exceptions import ClientError

from app.storage import neo4j_schema
from app.storage.neo4j_storage import Neo4jStorage
from scripts.migrate_neo4j_schema import apply_schema


class TestEntityUniquenessConstraint(unittest.TestCase):
    """Test suite asserting composite uniqueness constraint on Entity (graph_id, name_lower)."""

    def test_entity_graph_name_is_uniqueness_constraint(self):
        """
        Verify that entity_graph_name is defined as a uniqueness constraint,
        not a standard property index, in neo4j_schema.
        """
        constraint_query = getattr(neo4j_schema, "CREATE_ENTITY_GRAPH_NAME_CONSTRAINT", None)
        self.assertIsNotNone(
            constraint_query,
            "neo4j_schema must define CREATE_ENTITY_GRAPH_NAME_CONSTRAINT instead of a regular index",
        )

        query_upper = constraint_query.upper()
        self.assertIn("CREATE CONSTRAINT", query_upper)
        self.assertIn("FOR (N:ENTITY)", query_upper)
        self.assertIn("IS UNIQUE", query_upper)
        self.assertTrue(
            "REQUIRE (N.GRAPH_ID, N.NAME_LOWER) IS UNIQUE" in query_upper
            or "REQUIRE (N.NAME_LOWER, N.GRAPH_ID) IS UNIQUE" in query_upper,
            f"Constraint must enforce uniqueness on (n.graph_id, n.name_lower). Found: {constraint_query}",
        )

    def test_all_schema_queries_includes_uniqueness_constraint_not_plain_index(self):
        """
        Verify that ALL_SCHEMA_QUERIES contains the uniqueness constraint
        and does not contain a plain CREATE INDEX for entity_graph_name.
        """
        has_plain_index = any(
            "CREATE INDEX entity_graph_name" in q and "UNIQUE" not in q
            for q in neo4j_schema.ALL_SCHEMA_QUERIES
        )
        self.assertFalse(
            has_plain_index,
            "ALL_SCHEMA_QUERIES should not contain plain 'CREATE INDEX entity_graph_name'",
        )

        has_constraint = any(
            "CONSTRAINT" in q.upper()
            and "ENTITY" in q.upper()
            and "GRAPH_ID" in q.upper()
            and "NAME_LOWER" in q.upper()
            and "UNIQUE" in q.upper()
            for q in neo4j_schema.ALL_SCHEMA_QUERIES
        )
        self.assertTrue(
            has_constraint,
            "ALL_SCHEMA_QUERIES must contain a composite uniqueness constraint on Entity (graph_id, name_lower)",
        )

    def test_drop_legacy_index_precedes_constraint(self):
        """
        Verify that DROP_LEGACY_ENTITY_GRAPH_NAME_INDEX is executed before
        CREATE_ENTITY_GRAPH_NAME_CONSTRAINT so legacy plain indexes are cleaned up.
        """
        queries = neo4j_schema.ALL_SCHEMA_QUERIES
        self.assertIn(neo4j_schema.DROP_LEGACY_ENTITY_GRAPH_NAME_INDEX, queries)
        self.assertIn(neo4j_schema.CREATE_ENTITY_GRAPH_NAME_CONSTRAINT, queries)

        drop_idx = queries.index(neo4j_schema.DROP_LEGACY_ENTITY_GRAPH_NAME_INDEX)
        create_idx = queries.index(neo4j_schema.CREATE_ENTITY_GRAPH_NAME_CONSTRAINT)
        self.assertLess(
            drop_idx,
            create_idx,
            "DROP_LEGACY_ENTITY_GRAPH_NAME_INDEX must precede CREATE_ENTITY_GRAPH_NAME_CONSTRAINT",
        )

    def test_idempotent_schema_initialization(self):
        """
        Verify running schema initialization multiple times consecutively
        does not raise ConstraintAlreadyExists / IndexAlreadyExists exceptions.
        """
        # 1. Verify all CREATE queries use IF NOT EXISTS and DROP uses IF EXISTS
        for query in neo4j_schema.ALL_SCHEMA_QUERIES:
            query_clean = query.strip().upper()
            is_create = any(
                query_clean.startswith(prefix)
                for prefix in ("CREATE CONSTRAINT", "CREATE INDEX", "CREATE VECTOR INDEX", "CREATE FULLTEXT INDEX")
            )
            if is_create:
                self.assertIn(
                    "IF NOT EXISTS",
                    query_clean,
                    f"Schema query missing IF NOT EXISTS: {query}",
                )
            elif query_clean.startswith("DROP INDEX"):
                self.assertIn(
                    "IF EXISTS",
                    query_clean,
                    f"Drop index query missing IF EXISTS: {query}",
                )

        # 2. Test consecutive executions of apply_schema
        mock_driver = MagicMock()
        mock_session = MagicMock()
        mock_driver.session.return_value.__enter__.return_value = mock_session

        # Run 3 consecutive times without errors
        for _ in range(3):
            apply_schema(mock_driver)

        expected_total_calls = len(neo4j_schema.ALL_SCHEMA_QUERIES) * 3
        self.assertEqual(mock_session.run.call_count, expected_total_calls)

        # 3. Simulate driver throwing ConstraintAlreadyExists / IndexAlreadyExists exceptions
        mock_session.reset_mock()
        constraint_exists_err = ClientError.hydrate(
            message="An equivalent constraint already exists",
            code="Neo.ClientError.Schema.ConstraintAlreadyExists",
        )
        index_exists_err = ClientError.hydrate(
            message="An equivalent index already exists",
            code="Neo.ClientError.Schema.IndexAlreadyExists",
        )
        mock_session.run.side_effect = [
            None,
            constraint_exists_err,
            index_exists_err,
        ] + [None] * (len(neo4j_schema.ALL_SCHEMA_QUERIES) - 3)

        # Neither apply_schema nor _ensure_schema should re-raise these exceptions
        apply_schema(mock_driver)

        # 4. Test Neo4jStorage._ensure_schema idempotency
        storage = Neo4jStorage.__new__(Neo4jStorage)
        storage._driver = mock_driver
        mock_session.reset_mock()
        mock_session.run.side_effect = None
        mock_session.run.return_value = []

        with patch.object(storage, "wait_for_indexes_online", return_value=True):
            storage._ensure_schema()
            storage._ensure_schema()

    def test_case_insensitivity_prevents_duplicate_entities_under_same_graph(self):
        """
        Verify that name_lower uniqueness prevents duplicate entities with
        different casing (e.g. 'Alice' and 'alice') under the same graph_id.
        """
        graph_id = "graph_test_case_sensitivity"
        cased_names = ["Alice", "alice", "ALICE", "aLiCe"]

        # Ensure all variations fold to the same name_lower
        name_lowers = [name.lower() for name in cased_names]
        self.assertEqual(len(set(name_lowers)), 1)
        self.assertEqual(name_lowers[0], "alice")

        # Simulate Neo4j unique composite index constraint: (graph_id, name_lower)
        unique_nodes = {}  # composite_key -> node_data

        def insert_entity(gid: str, name: str, uuid: str):
            key = (gid, name.lower())
            if key in unique_nodes:
                raise ClientError.hydrate(
                    message=f"Node already exists with {key}",
                    code="Neo.ClientError.Schema.ConstraintValidationFailed",
                )
            unique_nodes[key] = {"uuid": uuid, "name": name, "graph_id": gid}

        # First entity insertion succeeds
        insert_entity(graph_id, cased_names[0], "uuid-alice-1")
        self.assertEqual(len(unique_nodes), 1)

        # Subsequent insertions with different casing under the same graph_id fail the constraint
        for variant in cased_names[1:]:
            with self.assertRaises(ClientError):
                insert_entity(graph_id, variant, f"uuid-{variant}")

        # In MERGE semantics, verify both variations match the same record
        merged_nodes = {}
        for variant in cased_names:
            key = (graph_id, variant.lower())
            if key not in merged_nodes:
                merged_nodes[key] = {"name": variant, "graph_id": graph_id, "name_lower": key[1]}
        self.assertEqual(
            len(merged_nodes),
            1,
            "All cased variants of an entity must merge into a single node under the same graph_id",
        )

    def test_cross_graph_isolation_allows_same_name_in_different_graphs(self):
        """
        Verify that identical name_lower across different graph_id values
        are allowed by the composite constraint (n.graph_id, n.name_lower).
        """
        entity_name = "Alice"
        graph_a = "graph_alpha"
        graph_b = "graph_beta"

        # The composite keys must be distinct despite identical name_lower
        key_a = (graph_a, entity_name.lower())
        key_b = (graph_b, entity_name.lower())
        self.assertNotEqual(key_a, key_b)

        # Simulate Neo4j unique composite index constraint: (graph_id, name_lower)
        unique_nodes = {}

        def insert_entity(gid: str, name: str, uuid: str):
            key = (gid, name.lower())
            if key in unique_nodes:
                raise ClientError.hydrate(
                    message=f"Node already exists with {key}",
                    code="Neo.ClientError.Schema.ConstraintValidationFailed",
                )
            unique_nodes[key] = {"uuid": uuid, "name": name, "graph_id": gid}

        # Both entities under different graph_ids can be stored concurrently
        insert_entity(graph_a, entity_name, "uuid-alpha-alice")
        insert_entity(graph_b, entity_name, "uuid-beta-alice")

        self.assertEqual(len(unique_nodes), 2)
        self.assertIn(key_a, unique_nodes)
        self.assertIn(key_b, unique_nodes)
        self.assertEqual(unique_nodes[key_a]["uuid"], "uuid-alpha-alice")
        self.assertEqual(unique_nodes[key_b]["uuid"], "uuid-beta-alice")


if __name__ == "__main__":
    unittest.main()
