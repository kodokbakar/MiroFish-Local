"""Tests for Issue #82: Bolt payload projection to prune raw vector embeddings."""

import json
from unittest.mock import MagicMock, patch

from app import create_app
from app.services.graph_builder import GraphBuilderService
from app.storage.neo4j_records import edge_to_dict, node_to_dict
from app.storage.neo4j_storage import Neo4jStorage
from tests.test_neo4j_storage_ingestion import InMemoryNeo4jDriver


class MockNeo4jNode:
    """Simulates a neo4j.graph.Node instance."""

    def __init__(self, props):
        self._props = props

    def __getitem__(self, key):
        return self._props[key]

    def get(self, key, default=None):
        return self._props.get(key, default)

    def items(self):
        return self._props.items()

    def keys(self):
        return self._props.keys()

    def values(self):
        return self._props.values()

    def __iter__(self):
        return iter(self._props)


def test_node_to_dict_handles_map_projection_dict():
    """Verify node_to_dict processes a plain dictionary (from map projection)."""
    proj = {
        "uuid": "u-123",
        "name": "Alice",
        "summary": "Engineer",
        "attributes_json": json.dumps({"role": "Tech Lead"}),
        "created_at": "2026-01-01T00:00:00Z",
    }
    result = node_to_dict(proj, ["Entity", "Person"])
    assert result["uuid"] == "u-123"
    assert result["name"] == "Alice"
    assert result["summary"] == "Engineer"
    assert result["attributes"] == {"role": "Tech Lead"}
    assert result["labels"] == ["Person"]
    assert "embedding" not in result


def test_node_to_dict_handles_neo4j_node_object():
    """Verify node_to_dict processes a mock neo4j.graph.Node object."""
    raw_node = MockNeo4jNode({
        "uuid": "u-456",
        "name": "Bob",
        "summary": "Researcher",
        "attributes_json": json.dumps({"field": "AI"}),
        "embedding": [0.123] * 1536,
        "created_at": "2026-01-02T00:00:00Z",
    })
    result = node_to_dict(raw_node, ["Entity", "Scientist"])
    assert result["uuid"] == "u-456"
    assert result["name"] == "Bob"
    assert result["summary"] == "Researcher"
    assert result["attributes"] == {"field": "AI"}
    assert result["labels"] == ["Scientist"]
    assert "embedding" not in result


def test_edge_to_dict_handles_dict_and_object():
    """Verify edge_to_dict handles both dicts and mapping-like objects."""
    edge_dict = {
        "uuid": "rel-1",
        "name": "COLLABORATES_WITH",
        "fact": "Alice collaborates with Bob",
        "attributes_json": json.dumps({"strength": "high"}),
        "episode_ids": ["ep-1"],
    }
    res1 = edge_to_dict(edge_dict, "u-123", "u-456")
    assert res1["name"] == "COLLABORATES_WITH"
    assert res1["source_node_uuid"] == "u-123"
    assert res1["target_node_uuid"] == "u-456"
    assert res1["attributes"] == {"strength": "high"}
    assert "fact_embedding" not in res1

    edge_obj = MockNeo4jNode({
        "uuid": "rel-2",
        "name": "WORKS_AT",
        "fact": "Bob works at Lab",
        "fact_embedding": [0.456] * 1536,
        "attributes_json": "{}",
    })
    res2 = edge_to_dict(edge_obj, "u-456", "u-789")
    assert res2["name"] == "WORKS_AT"
    assert "fact_embedding" not in res2


def test_get_graph_data_cypher_projection_without_embeddings():
    """Verify get_graph_data(include_embeddings=False) queries using map projection."""
    fake_driver = InMemoryNeo4jDriver()
    mock_session = MagicMock()
    mock_tx = MagicMock()

    # Seed return records
    mock_tx.run.side_effect = [
        # Node query result: projected dict
        [
            {
                "n": {
                    "uuid": "u-1",
                    "name": "Entity1",
                    "summary": "Summary1",
                    "attributes_json": "{}",
                    "created_at": "2026-01-01",
                },
                "labels": ["Entity", "Concept"],
            }
        ],
        # Edge query result: projected dict
        [
            {
                "r": {
                    "uuid": "e-1",
                    "name": "CONNECTS",
                    "fact": "Entity1 connects to Entity2",
                    "attributes_json": "{}",
                    "episode_ids": ["ep-1"],
                },
                "src_uuid": "u-1",
                "tgt_uuid": "u-2",
                "src_name": "Entity1",
                "tgt_name": "Entity2",
            }
        ],
    ]

    mock_session.execute_read.side_effect = lambda fn: fn(mock_tx)
    mock_session.__enter__.return_value = mock_session

    with patch("app.storage.neo4j_storage.GraphDatabase.driver") as mock_db_driver:
        mock_db_driver.return_value = fake_driver
        fake_driver.session = MagicMock(return_value=mock_session)

        storage = Neo4jStorage(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="password",
            embedding_service=MagicMock(),
            ner_extractor=MagicMock(),
        )

        data = storage.get_graph_data("test-graph-id", include_embeddings=False)

        assert data["graph_id"] == "test-graph-id"
        assert len(data["nodes"]) == 1
        assert len(data["edges"]) == 1
        node = data["nodes"][0]
        assert node["name"] == "Entity1"
        assert "embedding" not in node
        edge = data["edges"][0]
        assert edge["fact_type"] == "CONNECTS"
        assert "fact_embedding" not in edge

        # Inspect queries run
        assert mock_tx.run.call_count == 2
        node_query = mock_tx.run.call_args_list[0][0][0]
        assert "RETURN n {" in node_query
        assert ".uuid" in node_query
        assert "embedding" not in node_query

        edge_query = mock_tx.run.call_args_list[1][0][0]
        assert "RETURN r {" in edge_query
        assert ".uuid" in edge_query
        assert "fact_embedding" not in edge_query


def test_get_graph_data_cypher_with_embeddings():
    """Verify get_graph_data(include_embeddings=True) queries full nodes and relationships."""
    fake_driver = InMemoryNeo4jDriver()
    mock_session = MagicMock()
    mock_tx = MagicMock()

    mock_tx.run.side_effect = [
        [],
        [],
    ]
    mock_session.execute_read.side_effect = lambda fn: fn(mock_tx)
    mock_session.__enter__.return_value = mock_session

    with patch("app.storage.neo4j_storage.GraphDatabase.driver") as mock_db_driver:
        mock_db_driver.return_value = fake_driver
        fake_driver.session = MagicMock(return_value=mock_session)

        storage = Neo4jStorage(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="password",
            embedding_service=MagicMock(),
            ner_extractor=MagicMock(),
        )

        storage.get_graph_data("test-graph-id", include_embeddings=True)

        assert mock_tx.run.call_count == 2
        node_query = mock_tx.run.call_args_list[0][0][0]
        assert "RETURN n, labels(n)" in node_query
        assert "RETURN n {" not in node_query

        edge_query = mock_tx.run.call_args_list[1][0][0]
        assert "RETURN r, src.uuid AS src_uuid" in edge_query
        assert "RETURN r {" not in edge_query


def test_graph_data_api_endpoint_backward_compatibility():
    """Verify GET /api/graph/data/<graph_id> returns 200 and expected payload structure."""
    app = create_app()
    app.config["TESTING"] = True

    mock_graph_data = {
        "graph_id": "gid-123",
        "nodes": [
            {
                "uuid": "u-1",
                "name": "Alpha",
                "labels": ["Concept"],
                "summary": "Alpha summary",
                "attributes": {},
                "created_at": "2026-01-01",
            }
        ],
        "edges": [],
        "node_count": 1,
        "edge_count": 0,
    }

    mock_storage = MagicMock()
    mock_builder = MagicMock(spec=GraphBuilderService)
    mock_builder.get_graph_data.return_value = mock_graph_data

    with (
        patch("app.api.graph._get_storage", return_value=mock_storage),
        patch("app.api.graph.GraphBuilderService", return_value=mock_builder),
    ):
        client = app.test_client()

        # Default call (no query parameter)
        resp = client.get("/api/graph/data/gid-123")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["data"]["graph_id"] == "gid-123"
        assert body["data"]["nodes"][0]["name"] == "Alpha"
        mock_builder.get_graph_data.assert_called_with("gid-123", include_embeddings=False)

        # Call with include_embeddings=true
        resp_emb = client.get("/api/graph/data/gid-123?include_embeddings=true")
        assert resp_emb.status_code == 200
        mock_builder.get_graph_data.assert_called_with("gid-123", include_embeddings=True)
