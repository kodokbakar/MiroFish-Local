"""Regression test for Issue #63: Neo4j vector search zero-recall under multi-project graphs."""

from unittest.mock import Mock
import pytest

from app.storage.search_service import SearchService


def test_vector_search_oversamples_candidates_to_prevent_zero_recall():
    """
    In a multi-project Neo4j database, global vector indexes return top candidates
    across all graphs. If another graph dominates the first 2*limit candidates,
    filtering by graph_id discards them all unless candidate_limit is oversampled.
    """
    embedding = Mock()
    embedding.embed.return_value = [0.1, 0.2, 0.3]
    session = Mock()

    executed_queries = []

    def mock_run(query, **params):
        executed_queries.append((query, params))
        if "queryNodes" in query and "vector" in query:
            # Verify candidate_limit is passed and is oversampled (at least 10x limit or min 100)
            assert "candidate_limit" in params or params.get("limit", 0) >= 100, (
                f"Vector queryNodes called with insufficient candidate limit: {params}"
            )
            # Simulate multi-project: first 20 candidates belong to g_other, next 5 to g_target
            results = []
            target_gid = params.get("graph_id")
            for i in range(20):
                results.append({
                    "n": {"uuid": f"other_{i}", "name": f"Other {i}", "summary": "Other", "graph_id": "g_other"},
                    "labels": ["Entity"],
                    "score": 0.95 - (i * 0.01),
                })
            for i in range(5):
                results.append({
                    "n": {"uuid": f"target_{i}", "name": f"Target {i}", "summary": "Target", "graph_id": target_gid},
                    "labels": ["Entity"],
                    "score": 0.70 - (i * 0.01),
                })
            return [
                r for r in results
                if r["n"]["graph_id"] == target_gid
            ][:params.get("limit", 10)]
        elif "queryRelationships" in query and "vector" in query:
            assert "candidate_limit" in params or params.get("limit", 0) >= 100
            return []
        return []

    session.run.side_effect = mock_run
    service = SearchService(embedding)

    # Search with limit=10
    nodes = service.search_nodes(session, "g_target", "Find target entities", limit=10)

    # Must find target nodes, not return empty (zero-recall)
    assert len(nodes) == 5
    assert nodes[0]["uuid"] == "target_0"
    assert nodes[0]["graph_id"] == "g_target"
