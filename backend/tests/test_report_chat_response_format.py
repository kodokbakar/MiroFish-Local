"""
Unit tests for /api/report/chat response formatting and multi-turn chat history handling.
Item 043 (Issue #35).
"""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from app import create_app
from app.models.project import Project, ProjectManager, ProjectStatus
from app.services.report_agent import ReportAgent, ReportManager, ReportStatus
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

    def test_chat_endpoint_tool_calls_and_sources_presence(self, test_app, mock_project_and_sim):
        """Verify endpoint correctly includes present tool_calls and sources."""
        sim_id, _, _ = mock_project_and_sim

        mock_chat_result = {
            "response": "Detailed analysis from retrieved sources.",
            "tool_calls": [
                {"name": "panorama_search", "parameters": {"query": "safety incident"}},
                {"name": "interview_agents", "parameters": {"interview_topic": "student views", "max_agents": 3}},
            ],
            "sources": ["safety incident", "student views"],
        }

        with patch("app.api.report.ReportAgent") as mock_report_agent_cls:
            mock_agent_instance = MagicMock()
            mock_agent_instance.chat.return_value = mock_chat_result
            mock_report_agent_cls.return_value = mock_agent_instance

            client = test_app.test_client()
            resp = client.post(
                "/api/report/chat",
                json={"simulation_id": sim_id, "message": "Give me detailed feedback"},
            )

            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True
            payload = data["data"]
            assert payload["response"] == "Detailed analysis from retrieved sources."
            assert len(payload["tool_calls"]) == 2
            assert payload["tool_calls"][0]["name"] == "panorama_search"
            assert payload["tool_calls"][1]["name"] == "interview_agents"
            assert payload["sources"] == ["safety incident", "student views"]
            assert payload["simulation_id"] == sim_id

    def test_chat_endpoint_tool_calls_and_sources_absence_when_empty_or_omitted(self, test_app, mock_project_and_sim):
        """Verify endpoint returns empty lists for tool_calls and sources when omitted or empty."""
        sim_id, _, _ = mock_project_and_sim

        with patch("app.api.report.ReportAgent") as mock_report_agent_cls:
            mock_agent_instance = MagicMock()
            mock_report_agent_cls.return_value = mock_agent_instance
            client = test_app.test_client()

            # Case A: Explicit empty lists
            mock_agent_instance.chat.return_value = {
                "response": "Answer with empty tools",
                "tool_calls": [],
                "sources": [],
            }
            resp_a = client.post(
                "/api/report/chat",
                json={"simulation_id": sim_id, "message": "Test empty lists"},
            )
            assert resp_a.status_code == 200
            payload_a = resp_a.get_json()["data"]
            assert payload_a["tool_calls"] == []
            assert payload_a["sources"] == []

            # Case B: Omitted tool_calls and sources keys
            mock_agent_instance.chat.return_value = {"response": "Answer with missing keys"}
            resp_b = client.post(
                "/api/report/chat",
                json={"simulation_id": sim_id, "message": "Test missing keys"},
            )
            assert resp_b.status_code == 200
            payload_b = resp_b.get_json()["data"]
            assert payload_b["tool_calls"] == []
            assert payload_b["sources"] == []

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

    def test_chat_endpoint_missing_or_empty_simulation_id(self, test_app):
        """Verify endpoint returns 400 when simulation_id is empty, null, or missing."""
        client = test_app.test_client()

        # Empty string simulation_id
        resp_empty = client.post("/api/report/chat", json={"simulation_id": "", "message": "hi"})
        assert resp_empty.status_code == 400
        assert "simulation_id" in resp_empty.get_json()["error"]

        # None simulation_id
        resp_none = client.post("/api/report/chat", json={"simulation_id": None, "message": "hi"})
        assert resp_none.status_code == 400
        assert "simulation_id" in resp_none.get_json()["error"]

        # Empty body
        resp_empty_body = client.post("/api/report/chat", json={})
        assert resp_empty_body.status_code == 400
        assert "simulation_id" in resp_empty_body.get_json()["error"]

    def test_chat_endpoint_missing_or_empty_message(self, test_app):
        """Verify endpoint returns 400 when message is empty or null."""
        client = test_app.test_client()

        # Empty string message
        resp_empty = client.post("/api/report/chat", json={"simulation_id": "sim_test", "message": ""})
        assert resp_empty.status_code == 400
        assert "message" in resp_empty.get_json()["error"]

        # None message
        resp_none = client.post("/api/report/chat", json={"simulation_id": "sim_test", "message": None})
        assert resp_none.status_code == 400
        assert "message" in resp_none.get_json()["error"]

    def test_chat_endpoint_nonexistent_simulation(self, test_app, monkeypatch):
        """Verify endpoint returns 404 for non-existent simulation."""
        monkeypatch.setattr(SimulationManager, "get_simulation", lambda self, sid: None)
        client = test_app.test_client()
        resp = client.post("/api/report/chat", json={"simulation_id": "sim_missing", "message": "hello"})
        assert resp.status_code == 404

    def test_chat_endpoint_simulation_without_report(self, test_app, mock_project_and_sim, monkeypatch):
        """Verify chat endpoint still succeeds when simulation exists but report does not."""
        sim_id, _, _ = mock_project_and_sim
        monkeypatch.setattr(ReportManager, "get_report_by_simulation", lambda sid: None)

        with patch("app.api.report.ReportAgent") as mock_report_agent_cls:
            mock_agent = MagicMock()
            mock_agent.chat.return_value = {
                "response": "Response generated even before report is created.",
                "tool_calls": [],
                "sources": [],
            }
            mock_report_agent_cls.return_value = mock_agent

            client = test_app.test_client()
            resp = client.post(
                "/api/report/chat",
                json={"simulation_id": sim_id, "message": "Preliminary check"},
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True
            assert data["data"]["response"] == "Response generated even before report is created."

    def test_report_endpoints_nonexistent_report_id(self, test_app, monkeypatch):
        """Verify report endpoints return 404 for non-existent report_id."""
        monkeypatch.setattr(ReportManager, "get_progress", lambda rid: None)
        monkeypatch.setattr(ReportManager, "delete_report", lambda rid: False)
        monkeypatch.setattr(ReportManager, "_get_section_path", lambda rid, idx: "/tmp/nonexistent_section_path.md")

        client = test_app.test_client()

        # Nonexistent progress
        resp_prog = client.get("/api/report/nonexistent_report_id/progress")
        assert resp_prog.status_code == 404
        assert "does not exist" in resp_prog.get_json()["error"]

        # Nonexistent section
        resp_sec = client.get("/api/report/nonexistent_report_id/section/0")
        assert resp_sec.status_code == 404
        assert "does not exist" in resp_sec.get_json()["error"]

        # Nonexistent delete
        resp_del = client.delete("/api/report/nonexistent_report_id")
        assert resp_del.status_code == 404
        assert "does not exist" in resp_del.get_json()["error"]

    def test_check_report_status_nonexistent_report(self, test_app, monkeypatch):
        """Verify /check/<simulation_id> handles non-existent report gracefully."""
        monkeypatch.setattr(ReportManager, "get_report_by_simulation", lambda sid: None)

        client = test_app.test_client()
        resp = client.get("/api/report/check/sim_no_report")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["data"]["has_report"] is False
        assert data["data"]["report_id"] is None
        assert data["data"]["report_status"] is None
        assert data["data"]["interview_unlocked"] is False


class TestReportAgentChatHistoryHandling:
    """Tests for ReportAgent.chat multi-turn chat_history string coercion and ReACT tool loops."""

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

    def test_multiturn_chat_history_with_complex_nested_payloads(self):
        """Verify multi-turn history with complex nested payloads (dicts, lists, nulls, booleans, ints)."""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = "Multi-turn context understood."
        mock_graph_tools = MagicMock()

        agent = ReportAgent(
            graph_id="test_graph",
            simulation_id="test_sim",
            simulation_requirement="Test requirement",
            llm_client=mock_llm,
            graph_tools=mock_graph_tools,
        )

        complex_history = [
            # Turn 1: standard user query
            {"role": "user", "content": "What is the status of the simulation?"},
            # Turn 2: assistant with complex nested dict containing lists, booleans, and nulls
            {
                "role": "assistant",
                "content": {
                    "summary": "Simulation reached round 10.",
                    "metadata": {
                        "active": True,
                        "terminated": False,
                        "checkpoint": None,
                        "agents": ["agent_a", "agent_b"],
                    },
                    "tags": [{"id": 1, "valid": True}, None],
                },
            },
            # Turn 3: user message with boolean True content
            {"role": "user", "content": True},
            # Turn 4: assistant message with boolean False content
            {"role": "assistant", "content": False},
            # Turn 5: user message with None content
            {"role": "user", "content": None},
            # Turn 6: assistant message with nested list containing booleans and nulls
            {"role": "assistant", "content": [True, False, None, {"status": "ok"}]},
            # Turn 7: user message with integer
            {"role": "user", "content": 42},
            # Turn 8: assistant message with float
            {"role": "assistant", "content": 99.9},
            # Turn 9: user message with empty dictionary
            {"role": "user", "content": {}},
            # Turn 10: assistant message with empty list
            {"role": "assistant", "content": []},
            # Turn 11: turn with missing role
            {"content": "missing role content"},
        ]

        result = agent.chat(
            message="Final summary question",
            chat_history=complex_history,
        )

        assert isinstance(result, dict)
        assert result["response"] == "Multi-turn context understood."

        mock_llm.chat.assert_called_once()
        sent_messages = mock_llm.chat.call_args.kwargs["messages"]

        # Ensure all message fields are string types
        for idx, msg in enumerate(sent_messages):
            assert isinstance(msg["role"], str), f"Message {idx} role is not string: {type(msg['role'])}"
            assert isinstance(msg["content"], str), f"Message {idx} content is not string: {type(msg['content'])}"

        # Check that chat_history[-10:] preserved the last 10 turns plus system and current message
        assert len(sent_messages) == 1 + 10 + 1

        # Check JSON stringification of nested dict (item 1 in complex_history, first after trim)
        turn_2_msg = sent_messages[1]
        parsed_turn_2 = json.loads(turn_2_msg["content"])
        assert parsed_turn_2["summary"] == "Simulation reached round 10."
        assert parsed_turn_2["metadata"]["active"] is True
        assert parsed_turn_2["metadata"]["checkpoint"] is None

        # Check booleans are stringified
        assert sent_messages[2]["content"] == "True"
        assert sent_messages[3]["content"] == "False"

        # Check None is converted to empty string
        assert sent_messages[4]["content"] == ""

        # Check list is JSON-encoded
        turn_6_msg = sent_messages[5]
        parsed_turn_6 = json.loads(turn_6_msg["content"])
        assert parsed_turn_6[0] is True
        assert parsed_turn_6[2] is None

        # Check number stringification
        assert sent_messages[6]["content"] == "42"
        assert sent_messages[7]["content"] == "99.9"

        # Check empty dict/list stringification
        assert sent_messages[8]["content"] == "{}"
        assert sent_messages[9]["content"] == "[]"

        # Check missing role defaults to user
        assert sent_messages[10]["role"] == "user"
        assert sent_messages[10]["content"] == "missing role content"

        # Final message is user query
        assert sent_messages[-1]["role"] == "user"
        assert sent_messages[-1]["content"] == "Final summary question"

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

    def test_report_agent_chat_tool_call_react_execution_and_sources(self):
        """Verify ReportAgent.chat executes tool calls in ReACT loop and surfaces sources."""
        mock_llm = MagicMock()
        # Turn 1: tool call request; Turn 2: final answer after tool observation
        mock_llm.chat.side_effect = [
            '<tool_call>{"name": "quick_search", "parameters": {"query": "campus safety"}}</tool_call>',
            "Based on the search results, campus safety regulations have been tightened.",
        ]
        mock_graph_tools = MagicMock()

        agent = ReportAgent(
            graph_id="test_graph",
            simulation_id="test_sim",
            simulation_requirement="Test requirement",
            llm_client=mock_llm,
            graph_tools=mock_graph_tools,
        )
        agent._execute_tool = MagicMock(return_value="Records show 5 new safety protocols enacted.")

        result = agent.chat(message="What happened regarding safety?")

        assert isinstance(result, dict)
        assert result["response"] == "Based on the search results, campus safety regulations have been tightened."
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["name"] == "quick_search"
        assert result["tool_calls"][0]["parameters"]["query"] == "campus safety"
        assert result["sources"] == ["campus safety"]

        agent._execute_tool.assert_called_once_with("quick_search", {"query": "campus safety"})
        assert mock_llm.chat.call_count == 2

    def test_report_agent_chat_absence_of_tool_calls(self):
        """Verify ReportAgent.chat returns empty tool_calls and sources when no tools are invoked."""
        mock_llm = MagicMock()
        mock_llm.chat.return_value = "Direct assistant response without any tool calls."
        mock_graph_tools = MagicMock()

        agent = ReportAgent(
            graph_id="test_graph",
            simulation_id="test_sim",
            simulation_requirement="Test requirement",
            llm_client=mock_llm,
            graph_tools=mock_graph_tools,
        )

        result = agent.chat(message="Simple greeting")

        assert isinstance(result, dict)
        assert result["response"] == "Direct assistant response without any tool calls."
        assert result["tool_calls"] == []
        assert result["sources"] == []
        mock_llm.chat.assert_called_once()

    def test_report_agent_chat_with_nonexistent_report(self, monkeypatch):
        """Verify ReportAgent.chat gracefully handles simulations with no generated report."""
        monkeypatch.setattr(ReportManager, "get_report_by_simulation", lambda sid: None)

        mock_llm = MagicMock()
        mock_llm.chat.return_value = "Answer generated without pre-existing report."
        mock_graph_tools = MagicMock()

        agent = ReportAgent(
            graph_id="test_graph",
            simulation_id="test_sim_no_report",
            simulation_requirement="Test requirement",
            llm_client=mock_llm,
            graph_tools=mock_graph_tools,
        )

        result = agent.chat(message="Can you answer without a report?")

        assert result["response"] == "Answer generated without pre-existing report."
        sent_messages = mock_llm.chat.call_args.kwargs["messages"]
        system_msg = sent_messages[0]["content"]
        assert "（nonereport）" in system_msg


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

    def test_vue_file_interaction_contracts(self):
        """Verify Step5Interaction.vue adheres to chat API parameters and error handling contracts."""
        repo_root = Path(__file__).resolve().parents[2]
        vue_file = repo_root / "frontend" / "src" / "components" / "Step5Interaction.vue"
        content = vue_file.read_text(encoding="utf-8")

        # Verifies chatWithReport invocation
        assert "chatWithReport({" in content
        assert "simulation_id: props.simulationId" in content
        assert "chat_history: historyForApi" in content

        # Verifies history slicing
        assert ".slice(0, -1)" in content
        assert ".slice(-10)" in content

        # Verifies error throwing on unsuccessful response
        assert "throw new Error(res.error || 'Request failed')" in content
        assert "res.success && res.data" in content

    def test_frontend_json_structure_conforms_to_step5_expectations(self):
        """Verify backend response structures conform to Step5 frontend consumption logic."""
        # Emulate Step5 JavaScript unwrapping logic
        def step5_unpack(res):
            if not res.get("success") or not res.get("data"):
                raise RuntimeError(res.get("error") or "Request failed")
            data = res["data"]
            resp_field = data.get("response")
            if isinstance(resp_field, str):
                resp_content = resp_field
            elif isinstance(resp_field, dict):
                resp_content = resp_field.get("response") or data.get("answer") or ""
            else:
                resp_content = data.get("answer") or ""
            return resp_content if isinstance(resp_content, str) else json.dumps(resp_content)

        # Emulate Step5 renderMarkdown logic
        def step5_render_markdown(content):
            if not content:
                return ""
            str_val = content if isinstance(content, str) else json.dumps(content)
            processed = re.sub(r"^##\s+.+\n+", "", str_val)
            html = re.sub(r"```(\w*)\n([\s\S]*?)```", r'<pre class="code-block"><code>\2</code></pre>', processed)
            html = re.sub(r"`([^`]+)`", r'<code class="inline-code">\1</code>', html)
            html = re.sub(r"^### (.+)$", r'<h4 class="md-h4">\1</h4>', html, flags=re.MULTILINE)
            html = re.sub(r"^## (.+)$", r'<h3 class="md-h3">\1</h3>', html, flags=re.MULTILINE)
            return html

        # Case 1: Standard API response with flat string and tool calls
        standard_res = {
            "success": True,
            "data": {
                "response": "## Section Analysis\n\nKey finding: `metric = 0.95`\n\n```python\nprint('verified')\n```",
                "tool_calls": [{"name": "quick_search", "parameters": {"query": "trends"}}],
                "sources": ["trends"],
                "simulation_id": "sim_043",
            },
        }
        content_1 = step5_unpack(standard_res)
        assert isinstance(content_1, str)
        assert "`metric = 0.95`" in content_1
        rendered_html_1 = step5_render_markdown(content_1)
        assert '<code class="inline-code">metric = 0.95</code>' in rendered_html_1
        assert '<pre class="code-block"><code>print(\'verified\')\n</code></pre>' in rendered_html_1

        # Case 2: Legacy nested dictionary fallback response
        nested_res = {
            "success": True,
            "data": {
                "response": {
                    "response": "Fallback nested text",
                },
                "tool_calls": [],
                "sources": [],
                "simulation_id": "sim_043",
            },
        }
        content_2 = step5_unpack(nested_res)
        assert content_2 == "Fallback nested text"

        # Case 3: Legacy answer fallback response
        answer_fallback_res = {
            "success": True,
            "data": {
                "answer": "Direct answer key content",
                "tool_calls": [],
                "sources": [],
                "simulation_id": "sim_043",
            },
        }
        content_3 = step5_unpack(answer_fallback_res)
        assert content_3 == "Direct answer key content"

        # Case 4: API Error structure
        error_res = {
            "success": False,
            "error": "Simulation does not exist: sim_missing",
        }
        with pytest.raises(RuntimeError) as exc_info:
            step5_unpack(error_res)
        assert "Simulation does not exist" in str(exc_info.value)
