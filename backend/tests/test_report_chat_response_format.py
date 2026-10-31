"""
Unit tests for /api/report/chat response formatting and multi-turn chat history handling.
Item 043 (Issue #35).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from app import create_app
from app.models.project import Project, ProjectManager, ProjectStatus
from app.services.report_agent import ReportAgent
from app.services.simulation_manager import SimulationManager, SimulationState, SimulationStatus


@pytest.fixture
def test_app():
    """Create Flask test application with mocked Neo4jStorage."""
    with patch("app.storage.Neo4jStorage"):
        app = create_app()
    app.config["TESTING"] = True
    mock_storage = MagicMock()
    app.extensions["neo4j_storage"] = mock_storage
    return app


@pytest.fixture
def mock_project_and_sim(monkeypatch):
    """Setup mock simulation and project."""
    sim_id = "sim_test_043"
    proj_id = "proj_test_043"
    graph_id = "graph_test_043"

    sim_state = SimulationState(
        simulation_id=sim_id,
        project_id=proj_id,
        graph_id=graph_id,
        status=SimulationStatus.READY,
    )
    project = Project(
        project_id=proj_id,
        name="Test Project",
        status=ProjectStatus.GRAPH_COMPLETED,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        graph_id=graph_id,
        simulation_requirement="Test simulation requirement",
    )

    monkeypatch.setattr(
        SimulationManager,
        "get_simulation",
        lambda self, sid: sim_state if sid == sim_id else None,
    )
    monkeypatch.setattr(
        ProjectManager,
        "get_project",
        lambda pid: project if pid == proj_id else None,
    )

    return sim_id, proj_id, graph_id


class TestReportChatEndpointFormat:
    """Tests for /api/report/chat response unpacking and structure."""

    def test_chat_endpoint_flattens_nested_dict_response(self, test_app, mock_project_and_sim):
        """Verify endpoint unwraps agent.chat dict into flat string response and top-level fields."""
        sim_id, _, _ = mock_project_and_sim

        mock_chat_result = {
            "response": "This is the unpacked assistant reply.",
            "tool_calls": [{"name": "quick_search", "parameters": {"query": "trends"}}],
            "sources": ["trends"],
        }

        with patch("app.api.report.ReportAgent") as mock_report_agent_cls:
            mock_agent_instance = MagicMock()
            mock_agent_instance.chat.return_value = mock_chat_result
            mock_report_agent_cls.return_value = mock_agent_instance

            client = test_app.test_client()
            resp = client.post(
                "/api/report/chat",
                json={
                    "simulation_id": sim_id,
                    "message": "What are the latest findings?",
                    "chat_history": [
                        {"role": "user", "content": "hello"},
                        {"role": "assistant", "content": "hi there"},
                    ],
                },
            )

            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True

            payload = data["data"]
            # Response MUST be a flat string, NOT a nested dictionary
            assert isinstance(payload["response"], str)
            assert payload["response"] == "This is the unpacked assistant reply."
            assert payload["tool_calls"] == [{"name": "quick_search", "parameters": {"query": "trends"}}]
            assert payload["sources"] == ["trends"]
            assert payload["simulation_id"] == sim_id

            mock_agent_instance.chat.assert_called_once()

    def test_chat_endpoint_handles_string_result_fallback(self, test_app, mock_project_and_sim):
        """Verify endpoint safely handles a direct string return from agent.chat."""
        sim_id, _, _ = mock_project_and_sim

        with patch("app.api.report.ReportAgent") as mock_report_agent_cls:
            mock_agent_instance = MagicMock()
            mock_agent_instance.chat.return_value = "Direct string response"
            mock_report_agent_cls.return_value = mock_agent_instance

            client = test_app.test_client()
            resp = client.post(
                "/api/report/chat",
                json={
                    "simulation_id": sim_id,
                    "message": "Summarize please",
                },
            )

            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True
            payload = data["data"]
            assert payload["response"] == "Direct string response"
            assert payload["tool_calls"] == []
            assert payload["sources"] == []
            assert payload["simulation_id"] == sim_id

    def test_chat_endpoint_missing_parameters(self, test_app):
        """Verify endpoint returns 400 when required parameters are missing."""
        client = test_app.test_client()

        # Missing simulation_id
        resp1 = client.post("/api/report/chat", json={"message": "hello"})
        assert resp1.status_code == 400
        assert "simulation_id" in resp1.get_json()["error"]

        # Missing message
        resp2 = client.post("/api/report/chat", json={"simulation_id": "sim_123"})
        assert resp2.status_code == 400
        assert "message" in resp2.get_json()["error"]

    def test_chat_endpoint_nonexistent_simulation(self, test_app, monkeypatch):
        """Verify endpoint returns 404 for non-existent simulation."""
        monkeypatch.setattr(SimulationManager, "get_simulation", lambda self, sid: None)
        client = test_app.test_client()
        resp = client.post("/api/report/chat", json={"simulation_id": "sim_missing", "message": "hello"})
        assert resp.status_code == 404


class TestReportAgentChatHistoryHandling:
    """Tests for ReportAgent.chat multi-turn chat_history string coercion."""

    def test_chat_history_with_nested_dict_content_is_stringified(self):
        """Verify chat history with dict content (from prior buggy client) is coerced to string."""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = "I understand the historical context."
        mock_graph_tools = MagicMock()

        agent = ReportAgent(
            graph_id="test_graph",
            simulation_id="test_sim",
            simulation_requirement="Test requirement",
            llm_client=mock_llm,
            graph_tools=mock_graph_tools,
        )

        malformed_history = [
            {"role": "user", "content": "First question"},
            # Buggy previous turn with nested dictionary content
            {
                "role": "assistant",
                "content": {
                    "response": "Nested response text",
                    "tool_calls": [{"name": "quick_search"}],
                },
            },
            # None content
            {"role": "user", "content": None},
            # List content
            {"role": "assistant", "content": ["item 1", "item 2"]},
            # Invalid item that is not even a dict
            "invalid_non_dict_turn",
        ]

        result = agent.chat(
            message="Second question",
            chat_history=malformed_history,
        )

        assert isinstance(result, dict)
        assert result["response"] == "I understand the historical context."

        # Verify messages sent to llm.chat
        mock_llm.chat.assert_called_once()
        sent_messages = mock_llm.chat.call_args.kwargs["messages"]

        # All message contents MUST be strings
        for msg in sent_messages:
            assert isinstance(msg["role"], str)
            assert isinstance(msg["content"], str), f"Non-string content found in message: {msg}"

        # Check that the nested dict content was converted to json string
        nested_assistant_msg = sent_messages[2]
        assert nested_assistant_msg["role"] == "assistant"
        assert "Nested response text" in nested_assistant_msg["content"]

        # Check list was converted to json string
        list_assistant_msg = sent_messages[4]
        assert '["item 1", "item 2"]' in list_assistant_msg["content"]

        # Check user message is last
        assert sent_messages[-1]["role"] == "user"
        assert sent_messages[-1]["content"] == "Second question"

    def test_chat_with_non_string_message_coerced(self):
        """Verify non-string message parameter is safely coerced to string."""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = "Final answer."
        mock_graph_tools = MagicMock()

        agent = ReportAgent(
            graph_id="test_graph",
            simulation_id="test_sim",
            simulation_requirement="Test requirement",
            llm_client=mock_llm,
            graph_tools=mock_graph_tools,
        )

        # Message is a dictionary
        result = agent.chat(message={"query": "dict message"})
        assert result["response"] == "Final answer."

        sent_messages = mock_llm.chat.call_args.kwargs["messages"]
        last_msg = sent_messages[-1]
        assert last_msg["role"] == "user"
        assert isinstance(last_msg["content"], str)
        assert "dict message" in last_msg["content"]


class TestStep5InteractionVueContract:
    """Verify frontend Step5Interaction.vue contains defensive response unpacking and markdown safety."""

    def test_vue_file_safely_extracts_response_and_guards_markdown(self):
        repo_root = Path(__file__).resolve().parents[2]
        vue_file = repo_root / "frontend" / "src" / "components" / "Step5Interaction.vue"
        content = vue_file.read_text(encoding="utf-8")

        # Must check typeof res.data?.response === 'string'
        assert "typeof res.data?.response === 'string'" in content
        # Must unwrap nested response or answer fallback
        assert "res.data?.response?.response || res.data?.answer" in content
        # Must store stringified content
        assert "typeof respContent === 'string' ? respContent : JSON.stringify(respContent)" in content
        # renderMarkdown must guard against non-string input
        assert "typeof content === 'string' ? content : JSON.stringify(content)" in content
