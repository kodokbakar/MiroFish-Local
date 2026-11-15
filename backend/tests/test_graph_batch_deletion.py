import unittest
from unittest.mock import MagicMock

from app.storage.neo4j_storage import Neo4jStorage


class TestGraphBatchDeletionRegression(unittest.TestCase):
    """
    Regression test for Issue #97:
    Fix unbatched, unlabelled graph deletion causing JVM heap exhaustion
    and AllNodesScan.
    """

    def setUp(self):
        self.mock_driver = MagicMock()
        self.mock_session = MagicMock()
        self.mock_driver.session.return_value.__enter__.return_value = self.mock_session
        self.executed_queries = []

        def fake_run(query, **kwargs):
            self.executed_queries.append((query.strip(), kwargs))
            mock_result = MagicMock()
            mock_result.single.return_value = {"deleted": 0}
            return mock_result

        def fake_execute_write(fn, *args, **kwargs):
            fake_tx = MagicMock()
            fake_tx.run.side_effect = fake_run
            return fn(fake_tx, *args, **kwargs)

        self.mock_session.run.side_effect = fake_run
        self.mock_session.execute_write.side_effect = fake_execute_write

        self.storage = Neo4jStorage.__new__(Neo4jStorage)
        self.storage._driver = self.mock_driver
        self.storage._call_with_retry = lambda fn, *args, **kwargs: fn(*args, **kwargs)

    def test_delete_graph_uses_node_labels_and_prevents_unlabelled_scan(self):
        """
        delete_graph must not use unlabelled MATCH (n {graph_id: ...})
        because it forces an AllNodesScan across the entire Neo4j database.
        It must explicitly target labelled nodes (e.g. Entity, Episode).
        """
        self.storage.delete_graph("test_graph_123")

        queries = [q for q, _ in self.executed_queries]

        # Verify no query performs an unlabelled match on nodes
        for q in queries:
            self.assertNotIn(
                "MATCH (n {graph_id:",
                q,
                "delete_graph uses unlabelled 'MATCH (n {graph_id: ...})' triggering an AllNodesScan",
            )

        # Verify that explicit node labels are used (e.g., Entity and Episode)
        has_entity_label = any(":Entity" in q for q in queries)
        has_episode_label = any(":Episode" in q for q in queries)
        self.assertTrue(
            has_entity_label,
            "delete_graph does not explicitly target :Entity nodes with label index",
        )
        self.assertTrue(
            has_episode_label,
            "delete_graph does not explicitly target :Episode nodes with label index",
        )

    def test_delete_graph_is_batched_to_prevent_heap_exhaustion(self):
        """
        delete_graph must not execute deletion monolithically in a single unbatched transaction.
        Large graphs with hundreds of thousands of nodes/relationships will cause
        Neo4j JVM heap exhaustion and transaction log blowup.
        Deletion must be executed in batches (e.g. CALL { ... } IN TRANSACTIONS OF ... ROWS or iterative batching).
        """
        self.storage.delete_graph("test_graph_123")

        queries = [q for q, _ in self.executed_queries]

        # Check whether any query incorporates transactional batching
        is_batched = any(
            ("IN TRANSACTIONS" in q.upper()) or ("LIMIT" in q.upper())
            for q in queries
        )
        self.assertTrue(
            is_batched,
            "delete_graph executes deletion monolithically without batching (IN TRANSACTIONS or LIMIT batch loop)",
        )

    def test_delete_graph_iterates_until_all_batches_deleted(self):
        """
        Verify that delete_graph continues issuing batch deletion queries
        until count is 0, respecting custom batch_size.
        """
        self.executed_queries.clear()
        entity_counts = [500, 0]
        episode_counts = [200, 0]

        def fake_run_batches(query, **kwargs):
            self.executed_queries.append((query.strip(), kwargs))
            mock_result = MagicMock()
            if ":Entity" in query:
                mock_result.single.return_value = {
                    "deleted": entity_counts.pop(0) if entity_counts else 0
                }
            elif ":Episode" in query:
                mock_result.single.return_value = {
                    "deleted": episode_counts.pop(0) if episode_counts else 0
                }
            else:
                mock_result.single.return_value = {"deleted": 0}
            return mock_result

        def fake_execute_write(fn, *args, **kwargs):
            fake_tx = MagicMock()
            fake_tx.run.side_effect = fake_run_batches
            return fn(fake_tx, *args, **kwargs)

        self.mock_session.execute_write.side_effect = fake_execute_write

        self.storage.delete_graph("test_graph_123", batch_size=500)

        # 2 Entity queries + 2 Episode queries + 1 Graph node query = 5 queries
        self.assertEqual(len(self.executed_queries), 5)
        for _, kwargs in self.executed_queries[:4]:
            self.assertEqual(kwargs.get("batch_size"), 500)
            self.assertEqual(kwargs.get("gid"), "test_graph_123")


if __name__ == "__main__":
    unittest.main()
