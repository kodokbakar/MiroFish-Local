"""Regression test for Issue #71: Fast-path structured social actions graph ingestion."""

from unittest.mock import MagicMock
import pytest

from app.services.graph_memory_updater import AgentActivity, GraphMemoryUpdater


def test_graph_memory_updater_uses_fast_path_bypassing_ner():
    """
    When AgentActivity instances are sent to graph, GraphMemoryUpdater must call
    storage.persist_structured_activities() directly, completely bypassing add_text() and LLM NER.
    """
    mock_storage = MagicMock()
    mock_storage.persist_structured_activities.return_value = "ep-batch-1"

    updater = GraphMemoryUpdater(graph_id="g_sim", storage=mock_storage)

    activities = [
        AgentActivity(
            platform="twitter",
            agent_id=1,
            agent_name="Alice",
            action_type="LIKE_POST",
            action_args={"post_author_name": "Bob", "post_content": "Hello world"},
            round_num=1,
            timestamp="2026-11-12T00:00:00Z",
        ),
        AgentActivity(
            platform="twitter",
            agent_id=2,
            agent_name="Bob",
            action_type="CREATE_POST",
            action_args={"content": "New announcement"},
            round_num=1,
            timestamp="2026-11-12T00:01:00Z",
        ),
    ]

    updater._send_batch_activities(activities, "twitter")

    # Storage add_text must NOT be called (bypasses LLM NER)
    assert mock_storage.add_text.call_count == 0
    # Fast path must be invoked with the activities
    assert mock_storage.persist_structured_activities.call_count == 1
    call_args = mock_storage.persist_structured_activities.call_args
    assert call_args[0][0] == "g_sim"
    assert len(call_args[0][1]) == 2
