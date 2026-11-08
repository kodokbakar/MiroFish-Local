from unittest.mock import Mock

import pytest

from app.storage.search_service import SearchService


@pytest.mark.parametrize("path", ["vector", "keyword", "both"])
def test_edge_search_returns_endpoints_and_standard_fields(path):
    embedding = Mock()
    embedding.embed.return_value = [0.1]
    session = Mock()
    record = {
        "r": {"uuid": "r1", "name": "KNOWS", "fact": "Alice knows Bob", "graph_id": "g",
              "fact_embedding": [0.1], "attributes_json": '{"weight": 2}'},
        "src_uuid": "alice", "tgt_uuid": "bob", "score": 0.8,
    }

    def run(query, **kwargs):
        vector = "queryRelationships" in query and "vector" in query
        assert "startNode(relationship).uuid AS src_uuid" in query
        assert "endNode(relationship).uuid AS tgt_uuid" in query
        return [record] if path == "both" or (path == "vector") == vector else []

    session.run.side_effect = run
    results = SearchService(embedding).search_edges(session, "g", "Alice")
    assert len(results) == 1
    result = results[0]
    assert (result["source_node_uuid"], result["target_node_uuid"]) == ("alice", "bob")
    assert result["attributes"] == {"weight": 2}
    assert result["graph_id"] == "g"
    assert "fact_embedding" not in result and "attributes_json" not in result
    assert result["score"] == pytest.approx({"vector": 0.7, "keyword": 0.3, "both": 1.0}[path])


@pytest.mark.parametrize("path", ["vector", "keyword", "both"])
def test_node_search_returns_labels_and_standard_fields(path):
    embedding = Mock()
    embedding.embed.return_value = [0.1]
    session = Mock()
    record = {
        "n": {"uuid": "n1", "name": "Alice", "summary": "Person", "graph_id": "g",
              "embedding": [0.1], "name_lower": "alice", "attributes_json": '{"age": 30}'},
        "labels": ["Entity", "Person"], "score": 0.8,
    }

    def run(query, **kwargs):
        vector = "vector" in query
        assert "labels(node) AS labels" in query
        return [record] if path == "both" or (path == "vector") == vector else []

    session.run.side_effect = run
    results = SearchService(embedding).search_nodes(session, "g", "Alice")
    assert len(results) == 1
    result = results[0]
    assert result["labels"] == ["Person"]
    assert result["attributes"] == {"age": 30}
    assert result["graph_id"] == "g"
    assert not {"embedding", "name_lower", "attributes_json"} & result.keys()
    assert result["score"] == pytest.approx({"vector": 0.7, "keyword": 0.3, "both": 1.0}[path])


def test_search_edges_empty_query_skips_vector_and_uses_match_fallback():
    embedding = Mock()
    session = Mock()
    record = {
        "uuid": "r1", "name": "KNOWS", "fact": "Alice knows Bob",
        "src_uuid": "alice", "tgt_uuid": "bob", "score": 1.0,
    }

    def run(query, **kwargs):
        assert "vector" not in query
        assert "queryRelationships" not in query
        assert "MATCH ()-[r:RELATION" in query
        return [record]

    session.run.side_effect = run
    service = SearchService(embedding)
    results = service.search_edges(session, "g", "")
    assert len(results) == 1
    embedding.embed.assert_not_called()
    assert results[0]["uuid"] == "r1"
    assert results[0]["score"] == pytest.approx(0.3)


def test_search_nodes_empty_query_skips_vector_and_uses_match_fallback():
    embedding = Mock()
    session = Mock()
    record = {
        "uuid": "n1", "name": "Alice", "summary": "Person",
        "labels": ["Entity", "Person"], "score": 1.0,
    }

    def run(query, **kwargs):
        assert "vector" not in query
        assert "queryNodes" not in query
        assert "MATCH (n:Entity" in query
        return [record]

    session.run.side_effect = run
    service = SearchService(embedding)
    results = service.search_nodes(session, "g", "   ")
    assert len(results) == 1
    embedding.embed.assert_not_called()
    assert results[0]["uuid"] == "n1"
    assert results[0]["labels"] == ["Person"]
    assert results[0]["score"] == pytest.approx(0.3)


def test_search_edges_none_and_whitespace_query_skips_vector():
    embedding = Mock()
    session = Mock()
    record = {
        "uuid": "r2", "name": "WORKS_AT", "fact": "Bob works at ACME",
        "src_uuid": "bob", "tgt_uuid": "acme", "score": 1.0,
    }

    def run(query, **kwargs):
        assert "vector" not in query
        assert "MATCH ()-[r:RELATION" in query
        return [record]

    session.run.side_effect = run
    service = SearchService(embedding)
    for q in [None, "\t\n  \r"]:
        results = service.search_edges(session, "g", q)
        assert len(results) == 1
        embedding.embed.assert_not_called()
        assert results[0]["uuid"] == "r2"
        assert results[0]["score"] == pytest.approx(0.3)


def test_search_nodes_none_and_whitespace_query_skips_vector():
    embedding = Mock()
    session = Mock()
    record = {
        "uuid": "n2", "name": "Bob", "summary": "Engineer",
        "labels": ["Entity", "Person"], "score": 1.0,
    }

    def run(query, **kwargs):
        assert "vector" not in query
        assert "MATCH (n:Entity" in query
        return [record]

    session.run.side_effect = run
    service = SearchService(embedding)
    for q in [None, "\r\n  "]:
        results = service.search_nodes(session, "g", q)
        assert len(results) == 1
        embedding.embed.assert_not_called()
        assert results[0]["uuid"] == "n2"
        assert results[0]["labels"] == ["Person"]
        assert results[0]["score"] == pytest.approx(0.3)
