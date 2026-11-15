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

    def test_delete_graph_non_existent_graph_id_completes_cleanly(self):
        """
        Non-existent graph_id: deletion completes cleanly returning 0 deleted count
        without error and does not raise an exception.
        """
        self.executed_queries.clear()
        # Default mock returns deleted: 0
        self.storage.delete_graph("non_existent_graph_id")

        # Must execute: Entity batch check (0), Episode batch check (0), Graph node delete
        self.assertEqual(len(self.executed_queries), 3)
        entity_query, entity_kwargs = self.executed_queries[0]
        episode_query, episode_kwargs = self.executed_queries[1]
        graph_query, graph_kwargs = self.executed_queries[2]

        self.assertIn(":Entity", entity_query)
        self.assertIn(":Episode", episode_query)
        self.assertIn(":Graph", graph_query)
        self.assertEqual(entity_kwargs.get("gid"), "non_existent_graph_id")
        self.assertEqual(episode_kwargs.get("gid"), "non_existent_graph_id")
        self.assertEqual(graph_kwargs.get("gid"), "non_existent_graph_id")

    def test_delete_graph_exact_batch_boundary_condition(self):
        """
        Graph with exact multiple of batch size (e.g. exactly 2000 nodes with batch_size=1000)
        terminates cleanly after the final 0-count batch.
        """
        self.executed_queries.clear()
        # Exactly 2000 entities in batches of 1000 -> 1000, 1000, 0
        entity_counts = [1000, 1000, 0]
        episode_counts = [0]

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

        self.storage.delete_graph("boundary_graph", batch_size=1000)

        # 3 Entity queries (1000, 1000, 0) + 1 Episode query (0) + 1 Graph node query = 5 queries
        self.assertEqual(len(self.executed_queries), 5)
        entity_calls = [q for q, _ in self.executed_queries if ":Entity" in q]
        self.assertEqual(len(entity_calls), 3)

    def test_delete_graph_mixed_nodes_and_relationships(self):
        """
        Graph with entities, episodes, and residual relationships,
        verifying DETACH DELETE is used for all node types to clean up relationships.
        """
        self.executed_queries.clear()
        entity_counts = [150, 0]
        episode_counts = [80, 0]

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

        self.storage.delete_graph("mixed_graph", batch_size=200)

        # Check all queries use DETACH DELETE to ensure relationship cleanup
        for query, _ in self.executed_queries:
            self.assertIn("DETACH DELETE", query)

        entity_queries = [q for q, _ in self.executed_queries if ":Entity" in q]
        episode_queries = [q for q, _ in self.executed_queries if ":Episode" in q]
        graph_queries = [q for q, _ in self.executed_queries if ":Graph" in q]

        self.assertEqual(len(entity_queries), 2)
        self.assertEqual(len(episode_queries), 2)
        self.assertEqual(len(graph_queries), 1)

    def test_delete_graph_custom_batch_size_override(self):
        """
        Custom batch_size (e.g. 50 or 500) is properly propagated to query parameters.
        """
        for custom_size in (50, 500):
            self.executed_queries.clear()
            self.storage.delete_graph("test_graph_batch_param", batch_size=custom_size)

            batch_queries = [
                kwargs for q, kwargs in self.executed_queries
                if ":Entity" in q or ":Episode" in q
            ]
            self.assertGreaterEqual(len(batch_queries), 2)
            for kwargs in batch_queries:
                self.assertEqual(kwargs.get("batch_size"), custom_size)
                self.assertEqual(kwargs.get("gid"), "test_graph_batch_param")


if __name__ == "__main__":
    unittest.main()
