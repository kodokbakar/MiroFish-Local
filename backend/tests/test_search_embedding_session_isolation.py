"""Regression tests for Issue #93: Neo4j connection pool starvation caused by blocking embedding calls inside active sessions."""

from unittest.mock import MagicMock, Mock

from app.storage.neo4j_storage import Neo4jStorage
from app.storage.search_service import SearchService


def test_search_computes_embeddings_before_acquiring_session():
    """
    Neo4jStorage.search() must not hold a database session open while calling
    embedding.embed(query). Holding a connection during external network requests
    leads to Neo4j connection pool starvation under concurrent queries.
    """
    storage = Neo4jStorage.__new__(Neo4jStorage)
    mock_driver = MagicMock()
    mock_session = MagicMock()
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
