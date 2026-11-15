"""
Regression tests for Issue #96:
1. EntityReader.filter_defined_entities() O(|V|*|E|) CPU starvation due to nested loops.
2. Neo4jStorage.get_all_edges() unbounded Cypher queries and missing limit parameter.
"""

import inspect
import time
from unittest.mock import MagicMock

from app.services.entity_reader import EntityReader
from app.storage.graph_storage import GraphStorage
from app.storage.neo4j_storage import Neo4jStorage


class TrackingEdgeList(list):
    """
    List wrapper that tracks the number of times the list is iterated over
    and the total number of element accesses during iteration.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.iteration_count = 0
        self.item_access_count = 0

    def __iter__(self):
        self.iteration_count += 1
        for item in super().__iter__():
            self.item_access_count += 1
            yield item


class TestEntityReaderComplexity:
    """Demonstrate O(|V|*|E|) nested loop bug in EntityReader.filter_defined_entities."""

    def test_filter_defined_entities_edge_iteration_complexity(self):
        """
        EntityReader.filter_defined_entities should index edges in O(|E|) time
        rather than iterating all edges repeatedly for each node (O(|V|*|E|)).

        With 500 defined entity nodes and 2000 edges:
        - Current unoptimized code iterates all_edges 500 times, performing 1,000,000 edge checks.
        - Optimized code indexes edges once (<= 2 passes), performing O(|E|) checks.
        """
        node_count = 500
        edge_count = 2000

        # Construct nodes that qualify as defined entities (custom label besides Entity/Node)
        nodes = [
            {
                "uuid": f"node_{i}",
                "name": f"Entity_{i}",
                "labels": ["Entity", "Character"],
                "summary": f"Summary for node {i}",
                "attributes": {},
            }
            for i in range(node_count)
        ]

        # Construct edges linking nodes
        raw_edges = [
            {
                "uuid": f"edge_{i}",
                "name": "interacts_with",
                "fact": f"Interaction {i}",
                "source_node_uuid": f"node_{i % node_count}",
                "target_node_uuid": f"node_{(i + 1) % node_count}",
            }
            for i in range(edge_count)
        ]

        edges = TrackingEdgeList(raw_edges)
        mock_storage = MagicMock(spec=GraphStorage)
        mock_storage.get_all_nodes.return_value = nodes
        mock_storage.get_all_edges.return_value = edges

        reader = EntityReader(mock_storage)

        t0 = time.perf_counter()
        result = reader.filter_defined_entities("test_graph", enrich_with_edges=True)
        elapsed = time.perf_counter() - t0

        assert result.filtered_count == node_count
        assert len(result.entities) == node_count

        # Regression check: In O(|V|*|E|) implementation, iteration_count == 500 and item_access_count == 1,000,000.
        # In optimized O(|V|+|E|) implementation, all_edges is traversed at most twice to build an adjacency index.
        assert edges.iteration_count <= 2, (
            f"O(|V|*|E|) nested loop detected: all_edges was iterated {edges.iteration_count} times "
            f"for {node_count} nodes (expected <= 2 passes for in-memory indexing)"
        )
        assert edges.item_access_count <= 2 * edge_count, (
            f"O(|V|*|E|) CPU starvation detected: {edges.item_access_count} edge inspections performed "
            f"(expected <= {2 * edge_count} for O(|V|+|E|) indexing)"
        )
        # Optimized indexed processing takes < 0.03s; unoptimized O(|V|*|E|) takes ~0.10s-0.20s+
        assert elapsed < 0.03, (
            f"O(|V|*|E|) CPU starvation: filtering took {elapsed:.4f}s (expected < 0.03s)"
        )

    def test_filter_defined_entities_supports_max_edges_bound(self):
        """EntityReader.filter_defined_entities should accept max_edges and forward to storage."""
        reader_sig = inspect.signature(EntityReader.filter_defined_entities)
        assert "max_edges" in reader_sig.parameters, (
            "EntityReader.filter_defined_entities does not accept 'max_edges' parameter"
        )


class TestNeo4jStorageGetAllEdgesBounds:
    """Demonstrate missing limit parameter and unbounded MATCH in Neo4jStorage.get_all_edges."""

    def _create_mock_storage(self):
        storage = Neo4jStorage.__new__(Neo4jStorage)
        storage._driver = MagicMock()
        mock_session = MagicMock()
        storage._driver.session.return_value.__enter__.return_value = mock_session
        mock_tx = MagicMock()
        mock_tx.run.return_value = []
        mock_session.execute_read.side_effect = lambda tx_fn: tx_fn(mock_tx)
        storage._call_with_retry = lambda func, *args, **kwargs: func(*args, **kwargs)
        return storage, mock_tx

    def test_get_all_edges_signature_supports_limit(self):
        """GraphStorage, Neo4jStorage, and EntityReader get_all_edges must declare a limit parameter."""
        neo4j_sig = inspect.signature(Neo4jStorage.get_all_edges)
        assert "limit" in neo4j_sig.parameters, (
            "Neo4jStorage.get_all_edges does not support 'limit' parameter"
        )

        storage_sig = inspect.signature(GraphStorage.get_all_edges)
        assert "limit" in storage_sig.parameters, (
            "GraphStorage.get_all_edges does not declare 'limit' parameter"
        )

        reader_sig = inspect.signature(EntityReader.get_all_edges)
        assert "limit" in reader_sig.parameters, (
            "EntityReader.get_all_edges does not declare 'limit' parameter"
        )

    def test_get_all_edges_with_explicit_limit_uses_cypher_limit(self):
        """Calling get_all_edges with explicit limit must apply LIMIT in Cypher query."""
        storage, mock_tx = self._create_mock_storage()

        storage.get_all_edges("test_graph", limit=150)

        assert mock_tx.run.called, "tx.run was not called"
        query, kwargs = mock_tx.run.call_args[0][0], mock_tx.run.call_args[1]

        assert "LIMIT" in query.upper(), f"Cypher query does not include LIMIT clause: {query}"
        assert kwargs.get("limit") == 150, f"limit parameter not passed to Cypher query: {kwargs}"

    def test_get_all_edges_default_is_bounded(self):
        """Calling get_all_edges without explicit limit must apply a safe default bound."""
        storage, mock_tx = self._create_mock_storage()

        storage.get_all_edges("test_graph")

        assert mock_tx.run.called, "tx.run was not called"
        query = mock_tx.run.call_args[0][0]

        assert "LIMIT" in query.upper(), (
            f"Neo4jStorage.get_all_edges default query is unbounded: {query}"
        )
