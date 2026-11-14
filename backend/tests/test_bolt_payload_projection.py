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


def test_node_to_dict_unexpected_null_attributes_and_missing_keys():
    """Verify node_to_dict handles missing keys and explicit nulls from map projections."""
    # Completely empty map projection
    empty_res = node_to_dict({}, [])
    assert empty_res["uuid"] == ""
    assert empty_res["name"] == ""
    assert empty_res["summary"] == ""
    assert empty_res["attributes"] == {}
    assert empty_res["created_at"] is None
    assert empty_res["labels"] == []

    # Map projection with unexpected nulls / non-dict attributes_json
    null_node = {
        "uuid": None,
        "name": None,
        "summary": None,
        "attributes_json": None,
        "created_at": None,
    }
    null_res = node_to_dict(null_node, ["Entity"])
    assert null_res["uuid"] is None
    assert null_res["name"] is None
    assert null_res["summary"] is None
    assert null_res["attributes"] == {}
    assert null_res["created_at"] is None
    assert null_res["labels"] == []

    # Malformed attributes_json
    bad_json_node = {
        "uuid": "u-999",
        "name": "BrokenJson",
        "attributes_json": "{invalid-json}",
    }
    bad_res = node_to_dict(bad_json_node, ["Entity", "Test"])
    assert bad_res["attributes"] == {}
    assert bad_res["labels"] == ["Test"]


def test_edge_to_dict_complex_attributes_and_include_embeddings_query():
    """Verify edge_to_dict preserves complex nested attributes and include_embeddings=True query includes raw return."""
    complex_attrs = {
        "weight": 0.88,
        "tags": ["ml", "nlp"],
        "nested": {"deep": {"value": 42}},
    }
    edge_dict = {
        "uuid": "rel-complex",
        "name": "INTERACTS_WITH",
        "fact": "Alpha interacts with Beta",
        "attributes_json": json.dumps(complex_attrs),
        "created_at": "2026-03-01",
    }
    res = edge_to_dict(edge_dict, "src-1", "tgt-1")
    assert res["attributes"] == complex_attrs
    assert res["source_node_uuid"] == "src-1"
    assert res["target_node_uuid"] == "tgt-1"

    # Test include_embeddings=True via Neo4jStorage
    fake_driver = InMemoryNeo4jDriver()
    mock_session = MagicMock()
    mock_tx = MagicMock()

    mock_tx.run.side_effect = [
        [
            {
                "n": {
                    "uuid": "u-1",
                    "name": "Alpha",
                    "attributes_json": "{}",
                },
                "labels": ["Entity", "Person"],
            }
        ],
        [
            {
                "r": edge_dict,
                "src_uuid": "src-1",
                "tgt_uuid": "tgt-1",
                "src_name": "Alpha",
                "tgt_name": "Beta",
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

        data = storage.get_graph_data("test-graph-id", include_embeddings=True)

        assert len(data["edges"]) == 1
        edge = data["edges"][0]
        assert edge["attributes"] == complex_attrs
        assert edge["source_node_name"] == "Alpha"
        assert edge["target_node_name"] == "Beta"

        # Verify Cypher query asked for raw n and r without projection
        node_query = mock_tx.run.call_args_list[0][0][0]
        assert "RETURN n, labels(n)" in node_query
        edge_query = mock_tx.run.call_args_list[1][0][0]
        assert "RETURN r, src.uuid AS src_uuid" in edge_query


def test_graph_data_endpoint_direct_storage_call_assert_include_embeddings_false():
    """Verify GET /api/graph/data/<graph_id> delegates to storage.get_graph_data with include_embeddings=False."""
    app = create_app()
    app.config["TESTING"] = True

    mock_storage = MagicMock()
    mock_storage.get_graph_data.return_value = {
        "graph_id": "test-mock-gid",
        "nodes": [],
        "edges": [],
        "node_count": 0,
        "edge_count": 0,
    }

    with patch("app.api.graph._get_storage", return_value=mock_storage):
        client = app.test_client()
        resp = client.get("/api/graph/data/test-mock-gid")
        assert resp.status_code == 200
        mock_storage.get_graph_data.assert_called_once_with(
            "test-mock-gid", include_embeddings=False
        )
