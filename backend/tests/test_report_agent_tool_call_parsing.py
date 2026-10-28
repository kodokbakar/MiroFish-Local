"""
Tests for tool call parsing, validation, and error handling in ReportAgent (Issue #9 / port 028).

Verifies:
1. ReportAgent._is_valid_tool_call:
   - Non-dict inputs safely return False.
   - Missing or invalid tool names return False.
   - Normalization of "tool" -> "name" and "params" -> "parameters".
   - Non-dict, null, or missing parameters normalize to empty dict {}.
2. ReportAgent._parse_tool_calls:
   - Format 1 XML-style blocks validated and normalized.
   - Non-tool JSON blocks (e.g., {"thought": ...}) in <tool_call> are filtered out.
   - Tool calls with null or missing parameters normalized.
   - Format 2 raw JSON fallback validated and normalized.
3. ReportAgent._execute_tool:
   - Safe execution when parameters is None or non-dict (no AttributeError on .get).
4. ReAct loop in _generate_section_react:
   - No KeyError when LLM outputs {"tool": ..., "params": ...}.
   - No KeyError when LLM outputs non-tool blocks in <tool_call>.
   - No AttributeError when parameters is None.
5. chat endpoint:
   - No KeyError during tool execution with alternative key formats or invalid blocks.
   - Safe sources extraction when parameters is None or missing.
"""

from unittest.mock import MagicMock
import pytest

from app.services.report_agent import (
    ReportAgent,
    ReportOutline,
    ReportSection,
)


def _make_test_agent(mock_llm=None, mock_graph_tools=None):
    """Helper to instantiate ReportAgent with mocked dependencies."""
    llm = mock_llm or MagicMock()
    graph_tools = mock_graph_tools or MagicMock()
    mock_result = MagicMock()
    mock_result.to_text.return_value = "Mock graph result"
    graph_tools.quick_search.return_value = mock_result
    graph_tools.panorama_search.return_value = mock_result
    graph_tools.insight_forge.return_value = mock_result
    graph_tools.interview_agents.return_value = mock_result

    return ReportAgent(
        graph_id="test_graph",
        simulation_id="test_sim",
        simulation_requirement="Test requirements",
        llm_client=llm,
        graph_tools=graph_tools,
    )


# ============================================================================
# 1. Unit tests for ReportAgent._is_valid_tool_call
# ============================================================================

class TestIsValidToolCall:
    """Unit tests for tool call validation and normalization."""

    def test_non_dict_inputs_return_false(self):
        agent = _make_test_agent()
        assert agent._is_valid_tool_call(None) is False
        assert agent._is_valid_tool_call("not a dict") is False
        assert agent._is_valid_tool_call(123) is False
        assert agent._is_valid_tool_call(["quick_search"]) is False
        assert agent._is_valid_tool_call(True) is False

    def test_missing_or_unknown_tool_name_returns_false(self):
        agent = _make_test_agent()
        assert agent._is_valid_tool_call({"parameters": {"query": "foo"}}) is False
        assert agent._is_valid_tool_call({"thought": "I should search now"}) is False
        assert agent._is_valid_tool_call({"name": "non_existent_tool"}) is False
        assert agent._is_valid_tool_call({"tool": "random_bash"}) is False

    def test_standard_tool_call_is_valid(self):
        agent = _make_test_agent()
        data = {"name": "quick_search", "parameters": {"query": "test"}}
        assert agent._is_valid_tool_call(data) is True
        assert data["name"] == "quick_search"
        assert data["parameters"] == {"query": "test"}

    def test_normalizes_tool_and_params_keys(self):
        agent = _make_test_agent()
        data = {"tool": "quick_search", "params": {"query": "test"}}
        assert agent._is_valid_tool_call(data) is True
        assert data["name"] == "quick_search"
        assert data["parameters"] == {"query": "test"}
        assert "tool" not in data
        assert "params" not in data

    def test_normalizes_missing_parameters_to_dict(self):
        agent = _make_test_agent()
        data = {"name": "quick_search"}
        assert agent._is_valid_tool_call(data) is True
        assert data["parameters"] == {}

    def test_normalizes_null_parameters_to_dict(self):
        agent = _make_test_agent()
        data = {"name": "quick_search", "parameters": None}
        assert agent._is_valid_tool_call(data) is True
        assert data["parameters"] == {}

    def test_normalizes_non_dict_parameters_to_dict(self):
        agent = _make_test_agent()
        data = {"name": "quick_search", "parameters": "query=hello"}
        assert agent._is_valid_tool_call(data) is True
        assert data["parameters"] == {}

    def test_normalizes_null_params_to_dict(self):
        agent = _make_test_agent()
        data = {"tool": "quick_search", "params": None}
        assert agent._is_valid_tool_call(data) is True
        assert data["parameters"] == {}

    def test_params_overrides_null_parameters(self):
        agent = _make_test_agent()
        data = {"tool": "quick_search", "parameters": None, "params": {"query": "overridden"}}
        assert agent._is_valid_tool_call(data) is True
        assert data["parameters"] == {"query": "overridden"}

    def test_all_valid_tool_names_recognized(self):
        agent = _make_test_agent()
        for name in ReportAgent.VALID_TOOL_NAMES:
            assert agent._is_valid_tool_call({"name": name, "parameters": {}}) is True

    def test_additional_non_dict_and_whitespace_names_return_false(self):
        agent = _make_test_agent()
        # Non-dict inputs (floats, tuples, sets, empty list)
        assert agent._is_valid_tool_call(3.14) is False
        assert agent._is_valid_tool_call(("quick_search",)) is False
        assert agent._is_valid_tool_call({"quick_search"}) is False
        assert agent._is_valid_tool_call([]) is False
        # Empty dict
        assert agent._is_valid_tool_call({}) is False
        # Whitespace and invalid tool names
        assert agent._is_valid_tool_call({"name": "   "}) is False
        assert agent._is_valid_tool_call({"name": "\t\n"}) is False
        assert agent._is_valid_tool_call({"name": ""}) is False
        assert agent._is_valid_tool_call({"name": " quick_search "}) is False
        assert agent._is_valid_tool_call({"tool": "   "}) is False
        assert agent._is_valid_tool_call({"name": 123}) is False
        assert agent._is_valid_tool_call({"name": True}) is False

    def test_normalizes_primitive_and_collection_params_to_dict(self):
        agent = _make_test_agent()
        for bad_param in [123, 3.14, True, False, [1, 2], (3, 4), "string_params"]:
            data = {"name": "quick_search", "parameters": bad_param}
            assert agent._is_valid_tool_call(data) is True
            assert data["parameters"] == {}

            data_legacy = {"tool": "quick_search", "params": bad_param}
            assert agent._is_valid_tool_call(data_legacy) is True
            assert data_legacy["parameters"] == {}

    def test_params_and_parameters_mixed_types(self):
        agent = _make_test_agent()
        # parameters invalid, params valid dict -> params preserved
        data1 = {"name": "quick_search", "parameters": "bad", "params": {"query": "valid"}}
        assert agent._is_valid_tool_call(data1) is True
        assert data1["parameters"] == {"query": "valid"}

        # both invalid -> normalized to {}
        data2 = {"name": "quick_search", "parameters": "bad", "params": [1, 2]}
        assert agent._is_valid_tool_call(data2) is True
        assert data2["parameters"] == {}


# ============================================================================
# 2. Unit tests for ReportAgent._parse_tool_calls
# ============================================================================

class TestParseToolCalls:
    """Unit tests for _parse_tool_calls format parsing and filtering."""

    def test_parse_standard_xml_tool_call(self):
        agent = _make_test_agent()
        response = '<tool_call>{"name": "quick_search", "parameters": {"query": "campus"}}</tool_call>'
        calls = agent._parse_tool_calls(response)
        assert len(calls) == 1
        assert calls[0]["name"] == "quick_search"
        assert calls[0]["parameters"] == {"query": "campus"}

    def test_parse_xml_normalizes_tool_and_params(self):
        agent = _make_test_agent()
        response = '<tool_call>{"tool": "panorama_search", "params": {"query": "sentiment"}}</tool_call>'
        calls = agent._parse_tool_calls(response)
        assert len(calls) == 1
        assert calls[0]["name"] == "panorama_search"
        assert calls[0]["parameters"] == {"query": "sentiment"}

    def test_parse_xml_filters_out_non_tool_blocks(self):
        agent = _make_test_agent()
        response = (
            '<tool_call>{"thought": "I should first search student reactions"}</tool_call>\n'
            '<tool_call>{"tool": "quick_search", "params": {"query": "reactions"}}</tool_call>'
        )
        calls = agent._parse_tool_calls(response)
        assert len(calls) == 1
        assert calls[0]["name"] == "quick_search"
        assert calls[0]["parameters"] == {"query": "reactions"}

    def test_parse_xml_filters_out_non_dict_json(self):
        agent = _make_test_agent()
        response = (
            '<tool_call>["quick_search", "query"]</tool_call>\n'
            '<tool_call>{"name": "quick_search", "parameters": {}}</tool_call>'
        )
        calls = agent._parse_tool_calls(response)
        assert len(calls) == 1
        assert calls[0]["name"] == "quick_search"

    def test_parse_xml_normalizes_null_parameters(self):
        agent = _make_test_agent()
        response = '<tool_call>{"name": "insight_forge", "parameters": null}</tool_call>'
        calls = agent._parse_tool_calls(response)
        assert len(calls) == 1
        assert calls[0]["name"] == "insight_forge"
        assert calls[0]["parameters"] == {}

    def test_parse_xml_only_invalid_blocks_returns_empty(self):
        agent = _make_test_agent()
        response = '<tool_call>{"thought": "just thinking"}</tool_call>'
        calls = agent._parse_tool_calls(response)
        assert calls == []

    def test_parse_fallback_raw_json(self):
        agent = _make_test_agent()
        response = '{"tool": "quick_search", "params": {"query": "fallback"}}'
        calls = agent._parse_tool_calls(response)
        assert len(calls) == 1
        assert calls[0]["name"] == "quick_search"
        assert calls[0]["parameters"] == {"query": "fallback"}

    def test_parse_fallback_trailing_raw_json(self):
        agent = _make_test_agent()
        response = 'Thinking about the data...\n{"name": "quick_search", "parameters": {"query": "last"}}'
        calls = agent._parse_tool_calls(response)
        assert len(calls) == 1
        assert calls[0]["name"] == "quick_search"
        assert calls[0]["parameters"] == {"query": "last"}

    def test_parse_xml_malformed_json_ignored(self):
        agent = _make_test_agent()
        response = (
            '<tool_call>{name: "quick_search", unclosed</tool_call>\n'
            '<tool_call><not_json></tool_call>\n'
            '<tool_call>{"incomplete": </tool_call>'
        )
        calls = agent._parse_tool_calls(response)
        assert calls == []

    def test_parse_xml_primitive_payloads_ignored(self):
        agent = _make_test_agent()
        response = (
            '<tool_call>12345</tool_call>\n'
            '<tool_call>"just a plain string"</tool_call>\n'
            '<tool_call>true</tool_call>\n'
            '<tool_call>null</tool_call>'
        )
        calls = agent._parse_tool_calls(response)
        assert calls == []

    def test_parse_xml_mixed_valid_and_invalid_tool_calls(self):
        agent = _make_test_agent()
        response = (
            'Thinking about the response...\n'
            '<tool_call>{invalid json</tool_call>\n'
            '<tool_call>[1, 2, 3]</tool_call>\n'
            '<tool_call>999</tool_call>\n'
            '<tool_call>{"thought": "exploring graph"}</tool_call>\n'
            '<tool_call>{"name": "quick_search", "parameters": {"query": "campus events"}}</tool_call>\n'
            '<tool_call>{"name": "unknown_tool", "parameters": {}}</tool_call>\n'
            '<tool_call>{"tool": "panorama_search", "params": {"query": "student reactions"}}</tool_call>\n'
            '<tool_call>"trailing string"</tool_call>'
        )
        calls = agent._parse_tool_calls(response)
        assert len(calls) == 2
        assert calls[0]["name"] == "quick_search"
        assert calls[0]["parameters"] == {"query": "campus events"}
        assert calls[1]["name"] == "panorama_search"
        assert calls[1]["parameters"] == {"query": "student reactions"}


# ============================================================================
# 3. Unit tests for ReportAgent._execute_tool
# ============================================================================

class TestExecuteTool:
    """Unit tests for _execute_tool safe parameter handling."""

    def test_execute_tool_with_none_parameters_does_not_crash(self):
        agent = _make_test_agent()
        result = agent._execute_tool("quick_search", None)
        assert result == "Mock graph result"
        agent.graph_tools.quick_search.assert_called_once_with(
            graph_id="test_graph",
            query="",
            limit=10,
        )

    def test_execute_tool_with_non_dict_parameters_does_not_crash(self):
        agent = _make_test_agent()
        result = agent._execute_tool("insight_forge", "invalid_param")
        assert result == "Mock graph result"

    def test_execute_tool_insight_forge_with_empty_parameters(self):
        agent = _make_test_agent()
        result = agent._execute_tool("insight_forge", {})
        assert result == "Mock graph result"

    def test_execute_tool_panorama_search_with_parameters(self):
        agent = _make_test_agent()
        result = agent._execute_tool("panorama_search", {"query": "test", "include_expired": False})
        assert result == "Mock graph result"
        agent.graph_tools.panorama_search.assert_called_once_with(
            graph_id="test_graph",
            query="test",
            include_expired=False,
        )

    def test_execute_tool_all_tools_with_none_and_non_dict_params(self):
        agent = _make_test_agent()
        for tool_name in ["insight_forge", "panorama_search", "quick_search", "interview_agents"]:
            for bad_param in [None, 42, "invalid", [1, 2], True]:
                result = agent._execute_tool(tool_name, bad_param)
                assert result == "Mock graph result"

    def test_execute_tool_parameter_type_coercion_and_defaults(self):
        agent = _make_test_agent()

        # quick_search defaults and string limit coercion
        agent._execute_tool("quick_search", {})
        agent.graph_tools.quick_search.assert_called_with(
            graph_id="test_graph",
            query="",
            limit=10,
        )
        agent._execute_tool("quick_search", {"query": "q", "limit": "5"})
        agent.graph_tools.quick_search.assert_called_with(
            graph_id="test_graph",
            query="q",
            limit=5,
        )

        # panorama_search defaults and boolean string coercion
        agent._execute_tool("panorama_search", {})
        agent.graph_tools.panorama_search.assert_called_with(
            graph_id="test_graph",
            query="",
            include_expired=True,
        )
        agent._execute_tool("panorama_search", {"include_expired": "false"})
        agent.graph_tools.panorama_search.assert_called_with(
            graph_id="test_graph",
            query="",
            include_expired=False,
        )

        # interview_agents defaults and topic fallback
        agent._execute_tool("interview_agents", {})
        agent.graph_tools.interview_agents.assert_called_with(
            simulation_id="test_sim",
            interview_requirement="",
            simulation_requirement="Test requirements",
            max_agents=5,
        )
        agent._execute_tool("interview_agents", {"query": "topic_fallback", "max_agents": "8"})
        agent.graph_tools.interview_agents.assert_called_with(
            simulation_id="test_sim",
            interview_requirement="topic_fallback",
            simulation_requirement="Test requirements",
            max_agents=8,
        )

    def test_execute_tool_unknown_tool_and_exception(self):
        agent = _make_test_agent()
        unknown_res = agent._execute_tool("nonexistent_tool", {})
        assert "Unknown tool: nonexistent_tool" in unknown_res

        agent.graph_tools.quick_search.side_effect = RuntimeError("Database connection down")
        fail_res = agent._execute_tool("quick_search", {"query": "fail"})
        assert "Tool execution failed: Database connection down" in fail_res


# ============================================================================
# 4. Integration tests for ReAct loop (_generate_section_react)
# ============================================================================

class TestGenerateSectionReactParsing:
    """Integration tests verifying _generate_section_react handles malformed tool calls."""

    def test_react_loop_handles_tool_params_format_without_keyerror(self):
        """When LLM returns {"tool": ..., "params": ...}, loop succeeds without KeyError."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = '<tool_call>{"tool": "quick_search", "params": {"query": "test 1"}}</tool_call>'
        round2 = '<tool_call>{"tool": "panorama_search", "params": {"query": "test 2"}}</tool_call>'
        round3 = '<tool_call>{"tool": "insight_forge", "params": {"query": "test 3"}}</tool_call>'
        round4 = "Final Answer:\nSection content completed."

        mock_llm.chat.side_effect = [round1, round2, round3, round4]

        section = ReportSection(title="Overview", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        content = agent._generate_section_react(section, outline, previous_sections=[], section_index=0)
        assert content == "Section content completed."
        assert mock_llm.chat.call_count == 4

    def test_react_loop_handles_thought_block_in_tool_call(self):
        """When LLM outputs non-tool block in <tool_call>, loop handles it without KeyError."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        # Round 1 has a thought block and a valid tool call
        round1 = (
            '<tool_call>{"thought": "Need to search for context"}</tool_call>\n'
            '<tool_call>{"name": "quick_search", "parameters": {"query": "search 1"}}</tool_call>'
        )
        round2 = '<tool_call>{"name": "panorama_search", "parameters": {"query": "search 2"}}</tool_call>'
        round3 = '<tool_call>{"name": "insight_forge", "parameters": {"query": "search 3"}}</tool_call>'
        round4 = "Final Answer:\nSection generated successfully."

        mock_llm.chat.side_effect = [round1, round2, round3, round4]

        section = ReportSection(title="Section 1", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        content = agent._generate_section_react(section, outline, previous_sections=[], section_index=0)
        assert content == "Section generated successfully."

    def test_react_loop_handles_null_parameters_without_attribute_error(self):
        """When LLM outputs null parameters, loop executes without AttributeError."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = '<tool_call>{"name": "quick_search", "parameters": null}</tool_call>'
        round2 = '<tool_call>{"name": "panorama_search", "parameters": null}</tool_call>'
        round3 = '<tool_call>{"name": "insight_forge", "parameters": null}</tool_call>'
        round4 = "Final Answer:\nSection finished."

        mock_llm.chat.side_effect = [round1, round2, round3, round4]

        section = ReportSection(title="Null Params Section", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        content = agent._generate_section_react(section, outline, previous_sections=[], section_index=0)
        assert content == "Section finished."

    def test_react_loop_recovers_when_response_contains_only_invalid_tool_calls(self):
        """When LLM returns only invalid tool calls, loop continues and completes when valid calls arrive."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        # Round 1: only invalid tool call blocks (malformed JSON + thought + non-dict)
        round1 = (
            "Thinking about approach...\n"
            "<tool_call>{bad json</tool_call>\n"
            '<tool_call>{"thought": "planning next step"}</tool_call>\n'
            "<tool_call>123</tool_call>"
        )
        # Round 2: valid tool call
        round2 = '<tool_call>{"name": "quick_search", "parameters": {"query": "student query"}}</tool_call>'
        # Round 3: another valid tool call
        round3 = '<tool_call>{"name": "panorama_search", "parameters": {"query": "reactions"}}</tool_call>'
        # Round 4: another valid tool call to meet default min_tool_calls
        round4 = '<tool_call>{"name": "insight_forge", "parameters": {"query": "forge"}}</tool_call>'
        # Round 5: Final Answer
        round5 = "Final Answer:\nSection content recovered and completed successfully."

        mock_llm.chat.side_effect = [round1, round2, round3, round4, round5]

        section = ReportSection(title="Recovery Section", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        content = agent._generate_section_react(section, outline, previous_sections=[], section_index=0)
        assert content == "Section content recovered and completed successfully."
        assert mock_llm.chat.call_count == 5


# ============================================================================
# 5. Integration tests for chat endpoint
# ============================================================================

class TestChatToolCallParsing:
    """Integration tests verifying chat handles malformed tool calls."""

    def test_chat_handles_tool_params_format_without_keyerror(self):
        """chat executes tool successfully when format is {"tool": ..., "params": ...}."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = (
            "Searching for details...\n"
            '<tool_call>{"tool": "quick_search", "params": {"query": "incident details"}}</tool_call>'
        )
        round2 = "The incident occurred on Friday morning."

        mock_llm.chat.side_effect = [round1, round2]

        result = agent.chat("What happened?")
        assert result["response"] == "The incident occurred on Friday morning."
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["name"] == "quick_search"
        assert result["sources"] == ["incident details"]

    def test_chat_handles_thought_block_without_keyerror(self):
        """chat safely ignores non-tool blocks in <tool_call> without crashing."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = (
            "Let me consider the context.\n"
            '<tool_call>{"thought": "The user is asking about the timeline."}</tool_call>\n'
            "Here is the answer directly."
        )

        mock_llm.chat.side_effect = [round1]

        result = agent.chat("When did it happen?")
        assert "Here is the answer directly." in result["response"]
        assert result["tool_calls"] == []
        assert result["sources"] == []

    def test_chat_sources_extraction_with_null_parameters_does_not_crash(self):
        """chat safely extracts sources when tool call parameters is null or missing query."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = (
            "Looking up graph data...\n"
            '<tool_call>{"name": "quick_search", "parameters": null}</tool_call>'
        )
        round2 = "Summary from graph."

        mock_llm.chat.side_effect = [round1, round2]

        result = agent.chat("Show graph data")
        assert result["response"] == "Summary from graph."
        assert len(result["tool_calls"]) == 1
        assert result["sources"] == [""]

    def test_chat_all_tool_calls_invalid_falls_back_to_direct_reply(self):
        """When chat LLM returns only invalid tool calls, tags are stripped and direct reply is returned."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = (
            "Here is the synthesized overview.\n"
            "<tool_call>{malformed json: true</tool_call>\n"
            "<tool_call>42</tool_call>\n"
            '<tool_call>{"name": "non_existent_tool", "parameters": {}}</tool_call>\n'
            "Everything looks stable."
        )
        mock_llm.chat.side_effect = [round1]

        result = agent.chat("What is the current status?")
        assert "Here is the synthesized overview." in result["response"]
        assert "Everything looks stable." in result["response"]
        assert "<tool_call>" not in result["response"]
        assert result["tool_calls"] == []
        assert result["sources"] == []

    def test_chat_handles_non_dict_parameters_during_execution(self):
        """chat safely executes when parameters is non-dict string or list."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = '<tool_call>{"name": "quick_search", "parameters": "non_dict_param"}</tool_call>'
        round2 = "The quick search succeeded despite string param."

        mock_llm.chat.side_effect = [round1, round2]

        result = agent.chat("Check status")
        assert result["response"] == "The quick search succeeded despite string param."
        assert len(result["tool_calls"]) == 1
        assert result["sources"] == [""]
