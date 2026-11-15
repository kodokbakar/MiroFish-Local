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

from app.storage import neo4j_schema


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


if __name__ == "__main__":
    unittest.main()
