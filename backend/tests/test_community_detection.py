"""
Unit and integration tests for community detection (Issue #83 / Item 062).
"""

from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.storage.neo4j_storage import Neo4jStorage


class FakeResult:
    def __init__(self, records):
        self._records = records

    def __iter__(self):
        return iter(self._records)

    def single(self):
        return self._records[0] if self._records else None


class TestCommunityDetection:
    @pytest.fixture
    def app(self):
        app = create_app()
        app.config["TESTING"] = True
        return app

    def test_gds_fallback_to_networkx(self):
        """When GDS procedure check returns false or throws, falls back cleanly to networkx."""
        fake_driver = MagicMock()
        mock_session = MagicMock()
        fake_driver.session.return_value.__enter__.return_value = mock_session

        # Simulate SHOW PROCEDURES returning empty result
        def mock_execute_read(fn, *args, **kwargs):
            mock_tx = MagicMock()
            def mock_run(query, **params):
                if "SHOW PROCEDURES" in query:
                    return FakeResult([])
                if "MATCH (n:Entity {graph_id: $gid})" in query:
                    return FakeResult([
                        {"uuid": "u1", "name": "Alice", "labels": ["Entity", "Person"]},
                        {"uuid": "u2", "name": "Bob", "labels": ["Entity", "Person"]},
                    ])
                if "MATCH (src:Entity)-[r:RELATION" in query:
                    return FakeResult([
                        {"source": "u1", "target": "u2", "weight": 1.0}
                    ])
                return FakeResult([])
            mock_tx.run = mock_run
            return fn(mock_tx, *args, **kwargs)

        mock_session.execute_read = mock_execute_read

        updates_captured = []
        def mock_execute_write(fn, *args, **kwargs):
            mock_tx = MagicMock()
            def mock_run(query, **params):
                if "UNWIND $updates AS u" in query:
                    updates_captured.extend(params.get("updates", []))
                return FakeResult([])
            mock_tx.run = mock_run
            return fn(mock_tx, *args, **kwargs)

        mock_session.execute_write = mock_execute_write

        with patch("app.storage.neo4j_storage.GraphDatabase.driver", return_value=fake_driver):
            storage = Neo4jStorage(
                uri="bolt://localhost:7687",
                user="neo4j",
                password="password",
                embedding_service=MagicMock(),
                ner_extractor=MagicMock(),
            )
            summary = storage.detect_communities("test-graph")

        assert summary["total_nodes"] == 2
        assert summary["total_communities"] == 1
        assert len(summary["communities"]) == 1
        comm = summary["communities"][0]
        assert comm["id"] == 0
        assert comm["size"] == 2
        assert set(comm["top_entities"]) == {"Alice", "Bob"}
        assert comm["dominant_labels"] == ["Person"]
        assert len(updates_captured) == 2
        assert {u["uuid"] for u in updates_captured} == {"u1", "u2"}
        assert all(u["community_id"] == 0 for u in updates_captured)

    def test_empty_graph(self):
        """Edge case: empty graph returns 0 communities without error."""
        fake_driver = MagicMock()
        mock_session = MagicMock()
        fake_driver.session.return_value.__enter__.return_value = mock_session

        def mock_execute_read(fn, *args, **kwargs):
            mock_tx = MagicMock()
            mock_tx.run.return_value = FakeResult([])
            return fn(mock_tx, *args, **kwargs)

        mock_session.execute_read = mock_execute_read

        with patch("app.storage.neo4j_storage.GraphDatabase.driver", return_value=fake_driver):
            storage = Neo4jStorage(
                uri="bolt://localhost:7687",
                user="neo4j",
                password="password",
                embedding_service=MagicMock(),
                ner_extractor=MagicMock(),
            )
            summary = storage.detect_communities("empty-graph")

        assert summary == {
            "communities": [],
            "total_communities": 0,
            "total_nodes": 0,
        }

    def test_single_node_graph(self):
        """Edge case: single node graph produces 1 community with size 1."""
        fake_driver = MagicMock()
        mock_session = MagicMock()
        fake_driver.session.return_value.__enter__.return_value = mock_session

        def mock_execute_read(fn, *args, **kwargs):
            mock_tx = MagicMock()
            def mock_run(query, **params):
                if "SHOW PROCEDURES" in query:
                    return FakeResult([])
                if "MATCH (n:Entity {graph_id: $gid})" in query:
                    return FakeResult([
                        {"uuid": "u1", "name": "Solo", "labels": ["Entity", "Item"]}
                    ])
                if "MATCH (src:Entity)-[r:RELATION" in query:
                    return FakeResult([])
                return FakeResult([])
            mock_tx.run = mock_run
            return fn(mock_tx, *args, **kwargs)

        mock_session.execute_read = mock_execute_read
        mock_session.execute_write = lambda fn, *a, **kw: fn(MagicMock(), *a, **kw)

        with patch("app.storage.neo4j_storage.GraphDatabase.driver", return_value=fake_driver):
            storage = Neo4jStorage(
                uri="bolt://localhost:7687",
                user="neo4j",
                password="password",
                embedding_service=MagicMock(),
                ner_extractor=MagicMock(),
            )
            summary = storage.detect_communities("solo-graph")

        assert summary["total_nodes"] == 1
        assert summary["total_communities"] == 1
        assert summary["communities"][0]["size"] == 1
        assert summary["communities"][0]["top_entities"] == ["Solo"]
        assert summary["communities"][0]["dominant_labels"] == ["Item"]

    def test_disconnected_clusters(self):
        """Edge case: disconnected clusters detected into distinct communities."""
        fake_driver = MagicMock()
        mock_session = MagicMock()
        fake_driver.session.return_value.__enter__.return_value = mock_session

        nodes = [
            {"uuid": "a1", "name": "A1", "labels": ["Entity", "GroupA"]},
            {"uuid": "a2", "name": "A2", "labels": ["Entity", "GroupA"]},
            {"uuid": "b1", "name": "B1", "labels": ["Entity", "GroupB"]},
            {"uuid": "b2", "name": "B2", "labels": ["Entity", "GroupB"]},
        ]
        edges = [
            {"source": "a1", "target": "a2", "weight": 2.0},
            {"source": "b1", "target": "b2", "weight": 1.0},
        ]

        def mock_execute_read(fn, *args, **kwargs):
            mock_tx = MagicMock()
            def mock_run(query, **params):
                if "SHOW PROCEDURES" in query:
                    return FakeResult([])
                if "MATCH (n:Entity {graph_id: $gid})" in query:
                    return FakeResult(nodes)
                if "MATCH (src:Entity)-[r:RELATION" in query:
                    return FakeResult(edges)
                return FakeResult([])
            mock_tx.run = mock_run
            return fn(mock_tx, *args, **kwargs)

        mock_session.execute_read = mock_execute_read
        updates = []
        def mock_execute_write(fn, *args, **kwargs):
            mock_tx = MagicMock()
            def mock_run(query, **params):
                if "UNWIND $updates AS u" in query:
                    updates.extend(params.get("updates", []))
                return FakeResult([])
            mock_tx.run = mock_run
            return fn(mock_tx, *args, **kwargs)
        mock_session.execute_write = mock_execute_write

        with patch("app.storage.neo4j_storage.GraphDatabase.driver", return_value=fake_driver):
            storage = Neo4jStorage(
                uri="bolt://localhost:7687",
                user="neo4j",
                password="password",
                embedding_service=MagicMock(),
                ner_extractor=MagicMock(),
            )
            summary = storage.detect_communities("multi-graph")

        assert summary["total_nodes"] == 4
        assert summary["total_communities"] == 2
        assert len(updates) == 4
        u_map = {u["uuid"]: u["community_id"] for u in updates}
        assert u_map["a1"] == u_map["a2"]
        assert u_map["b1"] == u_map["b2"]
        assert u_map["a1"] != u_map["b1"]

    def test_api_endpoint_post_communities(self, app):
        """Verify POST /api/graph/<graph_id>/communities returns correct JSON schema."""
        mock_storage = MagicMock()
        mock_storage.detect_communities.return_value = {
            "communities": [{"id": 0, "size": 2, "top_entities": ["Alice", "Bob"], "dominant_labels": ["Person"]}],
            "total_communities": 1,
            "total_nodes": 2,
        }

        with patch("app.api.graph._get_storage", return_value=mock_storage):
            client = app.test_client()
            res = client.post("/api/graph/graph-xyz/communities")

        assert res.status_code == 200
        data = res.get_json()
        assert data["code"] == 0
        assert data["message"] == "success"
        assert data["data"]["total_communities"] == 1
        assert data["data"]["total_nodes"] == 2
        mock_storage.detect_communities.assert_called_once_with("graph-xyz")
