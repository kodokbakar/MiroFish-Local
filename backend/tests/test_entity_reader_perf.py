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


class TestEntityReaderEdgeCases:
    """Edge cases for EntityReader graph filtering and edge indexing."""

    def test_filter_defined_entities_empty_graph(self):
        """Empty graph with 0 nodes and 0 edges returns empty FilteredEntities without error."""
        mock_storage = MagicMock(spec=GraphStorage)
        mock_storage.get_all_nodes.return_value = []
        mock_storage.get_all_edges.return_value = []

        reader = EntityReader(mock_storage)
        result = reader.filter_defined_entities("empty_graph", enrich_with_edges=True)

        assert result.filtered_count == 0
        assert result.total_count == 0
        assert result.entities == []
        assert result.entity_types == set()
        assert result.to_dict()["entity_types"] == []

    def test_filter_defined_entities_isolated_nodes(self):
        """Graph with isolated nodes (0 edges) properly preserves nodes with empty relations."""
        nodes = [
            {
                "uuid": "n1",
                "name": "Isolated 1",
                "labels": ["Entity", "Item"],
                "summary": "Item 1",
                "attributes": {"weight": 10},
            },
            {
                "uuid": "n2",
                "name": "Isolated 2",
                "labels": ["Entity", "Item"],
                "summary": "Item 2",
                "attributes": {},
            },
        ]
        mock_storage = MagicMock(spec=GraphStorage)
        mock_storage.get_all_nodes.return_value = nodes
        mock_storage.get_all_edges.return_value = []

        reader = EntityReader(mock_storage)
        result = reader.filter_defined_entities("isolated_graph", enrich_with_edges=True)

        assert result.total_count == 2
        assert result.filtered_count == 2
        assert len(result.entities) == 2
        for entity in result.entities:
            assert entity.related_edges == []
            assert entity.related_nodes == []
            assert entity.get_entity_type() == "Item"

    def test_filter_defined_entities_extreme_limits(self):
        """EntityReader.filter_defined_entities handles max_edges=0 and max_edges=1 cleanly."""
        nodes = [
            {"uuid": "n1", "name": "Node 1", "labels": ["Entity", "Role"]},
            {"uuid": "n2", "name": "Node 2", "labels": ["Entity", "Role"]},
        ]
        edges = [
            {"uuid": "e1", "name": "rel1", "source_node_uuid": "n1", "target_node_uuid": "n2"},
            {"uuid": "e2", "name": "rel2", "source_node_uuid": "n2", "target_node_uuid": "n1"},
        ]
        mock_storage = MagicMock(spec=GraphStorage)
        mock_storage.get_all_nodes.return_value = nodes
        mock_storage.get_all_edges.side_effect = (
            lambda gid, limit=None: edges[:limit] if limit is not None else list(edges)
        )

        reader = EntityReader(mock_storage)

        # max_edges = 0
        res0 = reader.filter_defined_entities("test_graph", max_edges=0)
        assert len(res0.entities) == 2
        for ent in res0.entities:
            assert ent.related_edges == []
            assert ent.related_nodes == []

        # max_edges = 1
        res1 = reader.filter_defined_entities("test_graph", max_edges=1)
        assert len(res1.entities) == 2
        total_related = sum(len(ent.related_edges) for ent in res1.entities)
        # e1 is outgoing for n1 and incoming for n2
        assert total_related == 2

    def test_filter_defined_entities_duplicate_and_bidirectional_edges(self):
        """Edge indexing handles duplicate edges and bidirectional relations between same nodes."""
        nodes = [
            {"uuid": "n1", "name": "Alice", "labels": ["Entity", "Person"]},
            {"uuid": "n2", "name": "Bob", "labels": ["Entity", "Person"]},
        ]
        edges = [
            # Two outgoing edges from n1 to n2 (duplicate relations with different facts)
            {
                "uuid": "e1",
                "name": "FRIENDS_WITH",
                "fact": "Friends since school",
                "source_node_uuid": "n1",
                "target_node_uuid": "n2",
            },
            {
                "uuid": "e2",
                "name": "COLLEAGUES",
                "fact": "Work at same company",
                "source_node_uuid": "n1",
                "target_node_uuid": "n2",
            },
            # Reverse edge from n2 to n1 (bidirectional)
            {
                "uuid": "e3",
                "name": "MENTORS",
                "fact": "Bob mentors Alice",
                "source_node_uuid": "n2",
                "target_node_uuid": "n1",
            },
            # Self-loop on n1
            {
                "uuid": "e4",
                "name": "SELF_AWARE",
                "fact": "Alice knows herself",
                "source_node_uuid": "n1",
                "target_node_uuid": "n1",
            },
        ]
        mock_storage = MagicMock(spec=GraphStorage)
        mock_storage.get_all_nodes.return_value = nodes
        mock_storage.get_all_edges.return_value = edges

        reader = EntityReader(mock_storage)
        result = reader.filter_defined_entities("test_graph", enrich_with_edges=True)

        entities_by_uuid = {e.uuid: e for e in result.entities}
        alice = entities_by_uuid["n1"]
        bob = entities_by_uuid["n2"]

        # Alice:
        # Outgoing: e1 (to n2), e2 (to n2), e4 (to n1)
        # Incoming: e3 (from n2)
        alice_outgoing = [e for e in alice.related_edges if e["direction"] == "outgoing"]
        alice_incoming = [e for e in alice.related_edges if e["direction"] == "incoming"]
        assert len(alice_outgoing) == 3
        assert len(alice_incoming) == 1
        assert alice_incoming[0]["source_node_uuid"] == "n2"
        assert {rn["uuid"] for rn in alice.related_nodes} == {"n1", "n2"}

        # Bob:
        # Outgoing: e3 (to n1)
        # Incoming: e1 (from n1), e2 (from n1)
        bob_outgoing = [e for e in bob.related_edges if e["direction"] == "outgoing"]
        bob_incoming = [e for e in bob.related_edges if e["direction"] == "incoming"]
        assert len(bob_outgoing) == 1
        assert bob_outgoing[0]["target_node_uuid"] == "n1"
        assert len(bob_incoming) == 2
        assert {e["edge_name"] for e in bob_incoming} == {"FRIENDS_WITH", "COLLEAGUES"}
        assert {rn["uuid"] for rn in bob.related_nodes} == {"n1"}


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

    def test_get_all_edges_explicit_limit_none_omits_cypher_limit(self):
        """Passing limit=None to Neo4jStorage.get_all_edges must omit LIMIT clause from Cypher."""
        storage, mock_tx = self._create_mock_storage()

        storage.get_all_edges("test_graph", limit=None)

        assert mock_tx.run.called, "tx.run was not called"
        query, kwargs = mock_tx.run.call_args[0][0], mock_tx.run.call_args[1]

        assert "LIMIT" not in query.upper(), f"Cypher query unexpectedly contained LIMIT: {query}"
        assert "limit" not in kwargs, f"limit param unexpectedly passed to Cypher: {kwargs}"

    def test_get_all_edges_extreme_limits(self):
        """Neo4jStorage.get_all_edges with limit=0 and limit=1 applies the respective limit in Cypher."""
        storage, mock_tx = self._create_mock_storage()

        for extreme_limit in (0, 1):
            mock_tx.reset_mock()
            result = storage.get_all_edges("test_graph", limit=extreme_limit)
            assert result == []
            assert mock_tx.run.called
            query, kwargs = mock_tx.run.call_args[0][0], mock_tx.run.call_args[1]
            assert "LIMIT" in query.upper()
            assert kwargs.get("limit") == extreme_limit
