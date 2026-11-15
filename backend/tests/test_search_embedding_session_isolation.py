"""Regression tests for Issue #93: Neo4j connection pool starvation caused by blocking embedding calls inside active sessions."""

from unittest.mock import MagicMock, Mock

import pytest

from app.storage.neo4j_storage import Neo4jStorage
from app.storage.search_service import SearchService


def _setup_storage_with_session_tracker():
    """Helper creating Neo4jStorage with session-state and embed-order tracking."""
    storage = Neo4jStorage.__new__(Neo4jStorage)
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_session.run.return_value = []
    session_active = False

    def session_enter(*args):
        nonlocal session_active
        session_active = True
        return mock_session

    def session_exit(*args):
        nonlocal session_active
        session_active = False

    mock_driver.session.return_value.__enter__ = session_enter
    mock_driver.session.return_value.__exit__ = session_exit
    storage._driver = mock_driver

    active_during_embed = []
    mock_embedding = Mock()

    def fake_embed(text):
        active_during_embed.append(session_active)
        return [0.1, 0.2, 0.3]

    mock_embedding.embed.side_effect = fake_embed
    storage._search = SearchService(mock_embedding)

    return storage, mock_driver, mock_session, mock_embedding, active_during_embed


def test_search_computes_embeddings_before_acquiring_session():
    """
    Neo4jStorage.search() must not hold a database session open while calling
    embedding.embed(query). Holding a connection during external network requests
    leads to Neo4j connection pool starvation under concurrent queries.
    """
    storage, _, _, _, active_during_embed = _setup_storage_with_session_tracker()

    storage.search(graph_id="g1", query="test query", scope="edges")

    assert len(active_during_embed) == 1
    assert active_during_embed[0] is False, (
        "embedding.embed() was invoked while Neo4j session was active, "
        "holding the database connection during external embedding network call"
    )


def test_search_scope_both_reuses_query_vector_without_redundant_embed():
    """
    When scope='both', embedding.embed(query) should be called once and reused
    for both node and edge vector searches, rather than embedding redundantly twice.
    """
    storage = Neo4jStorage.__new__(Neo4jStorage)
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_session.run.return_value = []

    mock_driver.session.return_value.__enter__.return_value = mock_session
    storage._driver = mock_driver

    mock_embedding = Mock()
    mock_embedding.embed.return_value = [0.1, 0.2, 0.3]
    storage._search = SearchService(mock_embedding)

    storage.search(graph_id="g1", query="test query", scope="both")

    assert mock_embedding.embed.call_count == 1, (
        f"embedding.embed() called {mock_embedding.embed.call_count} times; "
        "expected exactly 1 call when scope='both'"
    )


def test_search_embedding_failure_does_not_acquire_session():
    """
    When embedding.embed(query) raises an exception (network failure/timeout),
    driver.session() must never be called, preventing database session leaks.
    """
    storage = Neo4jStorage.__new__(Neo4jStorage)
    mock_driver = MagicMock()
    storage._driver = mock_driver

    mock_embedding = Mock()
    mock_embedding.embed.side_effect = TimeoutError("Embedding service timed out")
    storage._search = SearchService(mock_embedding)

    with pytest.raises(TimeoutError, match="Embedding service timed out"):
        storage.search(graph_id="g1", query="failing query", scope="both")

    assert mock_driver.session.call_count == 0, (
        f"driver.session() called {mock_driver.session.call_count} times; "
        "expected zero sessions acquired when embedding fails"
    )


def test_search_single_scope_nodes_precomputes_embedding_once():
    """
    When scope='nodes', embedding.embed(query) must be called exactly once
    outside the active database session.
    """
    storage, mock_driver, _, mock_embedding, active_during_embed = (
        _setup_storage_with_session_tracker()
    )

    result = storage.search(graph_id="g1", query="target entity", scope="nodes")

    assert mock_embedding.embed.call_count == 1
    assert len(active_during_embed) == 1
    assert active_during_embed[0] is False
    assert mock_driver.session.call_count == 1
    assert "nodes" in result
    assert result["edges"] == []


def test_search_single_scope_edges_precomputes_embedding_once():
    """
    When scope='edges', embedding.embed(query) must be called exactly once
    outside the active database session.
    """
    storage, mock_driver, _, mock_embedding, active_during_embed = (
        _setup_storage_with_session_tracker()
    )

    result = storage.search(graph_id="g1", query="target relation", scope="edges")

    assert mock_embedding.embed.call_count == 1
    assert len(active_during_embed) == 1
    assert active_during_embed[0] is False
    assert mock_driver.session.call_count == 1
    assert "edges" in result
    assert result["nodes"] == []


@pytest.mark.parametrize("empty_query", ["", "   ", "\t\n  ", None])
def test_search_empty_or_whitespace_query_skips_embedding(empty_query):
    """
    Whitespace or empty queries should not invoke the embedding model,
    preventing wasted API calls and latency.
    """
    storage, _mock_driver, _, mock_embedding, _ = (
        _setup_storage_with_session_tracker()
    )

    result = storage.search(graph_id="g1", query=empty_query, scope="both")

    assert mock_embedding.embed.call_count == 0
    assert result["query"] == empty_query
    assert isinstance(result["edges"], list)
    assert isinstance(result["nodes"], list)


def test_direct_search_edges_without_precomputed_vector():
    """
    Calling search_edges directly with query_vector=None should fall back to
    calling embedding.embed(query) internally for backward compatibility.
    """
    mock_embedding = Mock()
    mock_embedding.embed.return_value = [0.4, 0.5, 0.6]
    service = SearchService(mock_embedding)
    mock_session = Mock()
    mock_session.run.return_value = []

    service.search_edges(mock_session, "g1", "legacy query", limit=5, query_vector=None)

    mock_embedding.embed.assert_called_once_with("legacy query")
    vector_calls = [
        call for call in mock_session.run.call_args_list
        if "query_vector" in call.kwargs
    ]
    assert len(vector_calls) == 1
    assert vector_calls[0].kwargs["query_vector"] == [0.4, 0.5, 0.6]


def test_direct_search_edges_with_precomputed_vector():
    """
    Calling search_edges directly with an explicit query_vector must not call
    embedding.embed(), using the supplied vector directly.
    """
    mock_embedding = Mock()
    service = SearchService(mock_embedding)
    mock_session = Mock()
    mock_session.run.return_value = []

    service.search_edges(
        mock_session, "g1", "legacy query", limit=5, query_vector=[0.7, 0.8, 0.9]
    )

    mock_embedding.embed.assert_not_called()
    vector_calls = [
        call for call in mock_session.run.call_args_list
        if "query_vector" in call.kwargs
    ]
    assert len(vector_calls) == 1
    assert vector_calls[0].kwargs["query_vector"] == [0.7, 0.8, 0.9]


def test_direct_search_nodes_without_precomputed_vector():
    """
    Calling search_nodes directly with query_vector=None should fall back to
    calling embedding.embed(query) internally for backward compatibility.
    """
    mock_embedding = Mock()
    mock_embedding.embed.return_value = [0.4, 0.5, 0.6]
    service = SearchService(mock_embedding)
    mock_session = Mock()
    mock_session.run.return_value = []

    service.search_nodes(mock_session, "g1", "legacy query", limit=5, query_vector=None)

    mock_embedding.embed.assert_called_once_with("legacy query")
    vector_calls = [
        call for call in mock_session.run.call_args_list
        if "query_vector" in call.kwargs
    ]
    assert len(vector_calls) == 1
    assert vector_calls[0].kwargs["query_vector"] == [0.4, 0.5, 0.6]


def test_direct_search_nodes_with_precomputed_vector():
    """
    Calling search_nodes directly with an explicit query_vector must not call
    embedding.embed(), using the supplied vector directly.
    """
    mock_embedding = Mock()
    service = SearchService(mock_embedding)
    mock_session = Mock()
    mock_session.run.return_value = []

    service.search_nodes(
        mock_session, "g1", "legacy query", limit=5, query_vector=[0.7, 0.8, 0.9]
    )

    mock_embedding.embed.assert_not_called()
    vector_calls = [
        call for call in mock_session.run.call_args_list
        if "query_vector" in call.kwargs
    ]
    assert len(vector_calls) == 1
    assert vector_calls[0].kwargs["query_vector"] == [0.7, 0.8, 0.9]
