"""
Tests for fabricated <tool_result> stripping in ReportAgent (port 008).

Verifies:
1. ReportAgent._strip_fake_tool_results unit cases:
   - Single <tool_result> block stripped while preserving <tool_call> and reasoning
   - Multiline <tool_result> with arbitrary nested tags and multiline text
   - Multiple <tool_result> blocks in one response
   - Normalization of extra newlines (\n{3,} -> \n\n) and leading/trailing whitespace
   - Response without <tool_result> preserved (trimmed)
   - Response containing only <tool_result> becomes empty string
2. SECTION_SYSTEM_PROMPT_TEMPLATE:
   - Rule 5 prohibits fabricated data and fake <tool_result> blocks
3. ReAct loop integration / mock checks:
   - _generate_section_react: messages history contains cleaned response without <tool_result>
   - chat: messages history contains cleaned response without <tool_result>
"""

from unittest.mock import MagicMock
import pytest

from app.services.report_agent import (
    ReportAgent,
    ReportOutline,
    ReportSection,
    SECTION_SYSTEM_PROMPT_TEMPLATE,
)


# ============================================================================
# 1. Unit tests for ReportAgent._strip_fake_tool_results
# ============================================================================

class TestStripFakeToolResults:
    """Unit tests for ReportAgent._strip_fake_tool_results."""

    def test_strip_single_tool_result_preserves_tool_call_and_reasoning(self):
        """Single <tool_result> block is stripped while keeping <tool_call> and reasoning."""
        response = (
            "I need to investigate student sentiments on the incident.\n"
            '<tool_call>{"name": "quick_search", "parameters": {"query": "student protests"}}</tool_call>\n'
            "<tool_result>\n"
            '{"status": "ok", "data": "fabricated search result by hallucinating LLM"}\n'
            "</tool_result>\n"
            "Based on the planned search, I will synthesize findings."
        )

        cleaned = ReportAgent._strip_fake_tool_results(response)

        assert "<tool_result>" not in cleaned
        assert "</tool_result>" not in cleaned
        assert "fabricated search result" not in cleaned
        assert "I need to investigate student sentiments on the incident." in cleaned
        assert '<tool_call>{"name": "quick_search", "parameters": {"query": "student protests"}}</tool_call>' in cleaned
        assert "Based on the planned search, I will synthesize findings." in cleaned

    def test_strip_multiline_tool_result_with_arbitrary_inner_tags(self):
        """Multiline <tool_result> with arbitrary inner tags / text is completely removed."""
        response = (
            "Reasoning step 1.\n"
            "<tool_result>\n"
            "<nested_tag attribute=\"val\">\n"
            "  <div>Complex multiline payload</div>\n"
            "  <item id='1'>Data line A</item>\n"
            "  <item id='2'>Data line B</item>\n"
            "</nested_tag>\n"
            "</tool_result>\n"
            "Reasoning step 2."
        )

        cleaned = ReportAgent._strip_fake_tool_results(response)

        assert "<tool_result>" not in cleaned
        assert "</tool_result>" not in cleaned
        assert "nested_tag" not in cleaned
        assert "Complex multiline payload" not in cleaned
        assert cleaned == "Reasoning step 1.\n\nReasoning step 2."

    def test_strip_multiple_tool_results(self):
        """Multiple <tool_result> blocks in a single response are all stripped."""
        response = (
            "Step 1:\n"
            '<tool_call>{"name": "quick_search", "parameters": {"query": "query1"}}</tool_call>\n'
            "<tool_result>Fake search result 1</tool_result>\n"
            "Step 2:\n"
            '<tool_call>{"name": "panorama_search", "parameters": {"query": "query2"}}</tool_call>\n'
            "<tool_result>\nFake search result 2\nwith multiple lines\n</tool_result>\n"
            "Step 3 conclusion."
        )

        cleaned = ReportAgent._strip_fake_tool_results(response)

        assert "<tool_result>" not in cleaned
        assert "</tool_result>" not in cleaned
        assert "Fake search result 1" not in cleaned
        assert "Fake search result 2" not in cleaned
        assert '<tool_call>{"name": "quick_search", "parameters": {"query": "query1"}}</tool_call>' in cleaned
        assert '<tool_call>{"name": "panorama_search", "parameters": {"query": "query2"}}</tool_call>' in cleaned
        assert "Step 1:" in cleaned
        assert "Step 2:" in cleaned
        assert "Step 3 conclusion." in cleaned

    def test_strip_newline_normalization_and_whitespace_trimming(self):
        """Extra newlines (\\n{3,} -> \\n\\n) and leading/trailing whitespace are normalized."""
        response = (
            "   \n\n\nLeading line\n\n\n\n\n"
            "<tool_result>fake payload</tool_result>"
            "\n\n\n\nMiddle line\n\n\n\n\n\nTrailing line\n\n   "
        )

        cleaned = ReportAgent._strip_fake_tool_results(response)

        assert cleaned == "Leading line\n\nMiddle line\n\nTrailing line"
        assert not cleaned.startswith(" ")
        assert not cleaned.endswith(" ")
        assert "\n\n\n" not in cleaned

    def test_strip_without_tool_result_preserved(self):
        """Response without <tool_result> is preserved (trimmed)."""
        response = (
            "   Thinking about next steps.\n"
            '<tool_call>{"name": "quick_search", "parameters": {"query": "trend"}}</tool_call>\n'
            "Awaiting system tool execution.   "
        )

        cleaned = ReportAgent._strip_fake_tool_results(response)

        expected = (
            "Thinking about next steps.\n"
            '<tool_call>{"name": "quick_search", "parameters": {"query": "trend"}}</tool_call>\n'
            "Awaiting system tool execution."
        )
        assert cleaned == expected

    def test_strip_only_tool_result_becomes_empty(self):
        """Response containing only <tool_result> becomes empty string."""
        response = "   \n\n<tool_result>\n{\"only\": \"fake result\"}\n</tool_result>\n\n   "

        cleaned = ReportAgent._strip_fake_tool_results(response)

        assert cleaned == ""


# ============================================================================
# 2. Tests for SECTION_SYSTEM_PROMPT_TEMPLATE
# ============================================================================

class TestSectionSystemPromptTemplate:
    """Tests for SECTION_SYSTEM_PROMPT_TEMPLATE rules."""

    def test_section_system_prompt_prohibits_fake_tool_results(self):
        """Verify prompt template contains rule 5 prohibiting fabricated data and fake <tool_result> blocks."""
        assert "5. [Prohibit Fabricating Data]" in SECTION_SYSTEM_PROMPT_TEMPLATE
        assert "<tool_result>" in SECTION_SYSTEM_PROMPT_TEMPLATE
        assert "Forbidden to include <tool_result> blocks in your response" in SECTION_SYSTEM_PROMPT_TEMPLATE
        assert "only the system provides tool results" in SECTION_SYSTEM_PROMPT_TEMPLATE


# ============================================================================
# 3. Integration / mock checks for ReAct loop
# ============================================================================

class TestReactLoopFakeToolResultsStripping:
    """Tests verifying ReAct loop strips fake tool results before appending to messages."""

    def test_generate_section_react_strips_fake_tool_results_from_messages(self):
        """In _generate_section_react, assistant message in history contains cleaned response without <tool_result>."""
        mock_llm = MagicMock()
        mock_graph_tools = MagicMock()

        # Mock tool results for graph tool executions
        mock_result = MagicMock()
        mock_result.to_text.return_value = "Real graph execution observation data"
        mock_graph_tools.quick_search.return_value = mock_result
        mock_graph_tools.panorama_search.return_value = mock_result
        mock_graph_tools.insight_forge.return_value = mock_result

        agent = ReportAgent(
            graph_id="test_graph",
            simulation_id="test_sim",
            simulation_requirement="Analyze campus reactions",
            llm_client=mock_llm,
            graph_tools=mock_graph_tools,
        )

        # Round 1: LLM outputs tool call + hallucinated fake <tool_result>
        round1_response = (
            "Thinking about campus reactions.\n"
            '<tool_call>{"name": "quick_search", "parameters": {"query": "reaction"}}</tool_call>\n'
            "<tool_result>\nFake hallucinated quick_search output\n</tool_result>"
        )
        # Round 2: LLM outputs tool call + fake <tool_result>
        round2_response = (
            "Analyzing previous findings.\n"
            '<tool_call>{"name": "panorama_search", "parameters": {"query": "sentiment"}}</tool_call>\n'
            "<tool_result>Fake panorama output</tool_result>"
        )
        # Round 3: LLM outputs tool call + fake <tool_result>
        round3_response = (
            "Synthesizing.\n"
            '<tool_call>{"name": "insight_forge", "parameters": {"query": "summary"}}</tool_call>\n'
            "<tool_result>Fake insight output</tool_result>"
        )
        # Round 4: LLM provides Final Answer
        round4_response = (
            "Final Answer:\n"
            "Campus reactions show widespread concern among students."
        )

        mock_llm.chat.side_effect = [
            round1_response,
            round2_response,
            round3_response,
            round4_response,
        ]

        section = ReportSection(title="Student Reactions", content="")
        outline = ReportOutline(
            title="Campus Incident Report",
            summary="Incident summary",
            sections=[section],
        )

        final_content = agent._generate_section_react(
            section=section,
            outline=outline,
            previous_sections=[],
        )

        assert "Campus reactions show widespread concern among students." in final_content
        assert mock_llm.chat.call_count == 4

        # Verify call 2 messages payload (sent after round 1 processed)
        call2_messages = mock_llm.chat.call_args_list[1].kwargs["messages"]
        assistant_msg_round1 = call2_messages[2]
        assert assistant_msg_round1["role"] == "assistant"
        assert "<tool_result>" not in assistant_msg_round1["content"]
        assert "</tool_result>" not in assistant_msg_round1["content"]
        assert "Fake hallucinated quick_search output" not in assistant_msg_round1["content"]
        assert '<tool_call>{"name": "quick_search", "parameters": {"query": "reaction"}}</tool_call>' in assistant_msg_round1["content"]
        assert "Thinking about campus reactions." in assistant_msg_round1["content"]

        # Verify real observation was injected by the system as user message
        user_observation_round1 = call2_messages[3]
        assert user_observation_round1["role"] == "user"
        assert "Real graph execution observation data" in user_observation_round1["content"]

        # Verify call 3 messages payload (sent after round 2 processed)
        call3_messages = mock_llm.chat.call_args_list[2].kwargs["messages"]
        assistant_msg_round2 = call3_messages[4]
        assert assistant_msg_round2["role"] == "assistant"
        assert "<tool_result>" not in assistant_msg_round2["content"]
        assert "Fake panorama output" not in assistant_msg_round2["content"]
        assert '<tool_call>{"name": "panorama_search"' in assistant_msg_round2["content"]

    def test_chat_react_loop_strips_fake_tool_results_from_messages(self):
        """In chat, assistant message in ReAct history contains cleaned response without <tool_result>."""
        mock_llm = MagicMock()
        mock_graph_tools = MagicMock()

        mock_result = MagicMock()
        mock_result.to_text.return_value = "Real quick search chat data"
        mock_graph_tools.quick_search.return_value = mock_result

        agent = ReportAgent(
            graph_id="test_graph",
            simulation_id="test_sim",
            simulation_requirement="Simulation overview",
            llm_client=mock_llm,
            graph_tools=mock_graph_tools,
        )

        chat_round1_response = (
            "Let me search the simulation data for your question.\n"
            '<tool_call>{"name": "quick_search", "parameters": {"query": "event timeline"}}</tool_call>\n'
            "<tool_result>\nFake hallucinated chat search result\n</tool_result>"
        )
        chat_round2_response = (
            "Based on the verified event timeline, here is what occurred."
        )

        mock_llm.chat.side_effect = [
            chat_round1_response,
            chat_round2_response,
        ]

        chat_result = agent.chat("What happened during the event?")

        assert "Based on the verified event timeline, here is what occurred." in chat_result["response"]
        assert mock_llm.chat.call_count == 2

        call2_messages = mock_llm.chat.call_args_list[1].kwargs["messages"]
        assistant_msg = call2_messages[2]
        assert assistant_msg["role"] == "assistant"
        assert "<tool_result>" not in assistant_msg["content"]
        assert "</tool_result>" not in assistant_msg["content"]
        assert "Fake hallucinated chat search result" not in assistant_msg["content"]
        assert '<tool_call>{"name": "quick_search", "parameters": {"query": "event timeline"}}</tool_call>' in assistant_msg["content"]
        assert "Let me search the simulation data for your question." in assistant_msg["content"]
