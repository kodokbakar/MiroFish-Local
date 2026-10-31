"""
Tests for ReACT loop context budgeting, adaptive early-exit policy,
in-memory simulation report cache, and incremental agent log reading (Issue #14).
"""

import os
import json
import shutil
import threading
import time
from unittest.mock import MagicMock, patch
import pytest

from app.config import Config
from app.services.report_agent import (
    ReportAgent,
    ReportOutline,
    ReportSection,
    Report,
    ReportStatus,
    ReportManager,
)


def _make_test_agent(mock_llm=None, mock_graph_tools=None):
    """Helper to instantiate ReportAgent with mocked dependencies."""
    llm = mock_llm or MagicMock()
    if mock_graph_tools is None:
        graph_tools = MagicMock()
        mock_result = MagicMock()
        mock_result.to_text.return_value = "Mock graph observation"
        graph_tools.quick_search.return_value = mock_result
        graph_tools.panorama_search.return_value = mock_result
        graph_tools.insight_forge.return_value = mock_result
        graph_tools.interview_agents.return_value = mock_result
    else:
        graph_tools = mock_graph_tools

    return ReportAgent(
        graph_id="test_graph",
        simulation_id="test_sim",
        simulation_requirement="Analyze campus reactions",
        llm_client=llm,
        graph_tools=graph_tools,
    )


# ============================================================================
# 1. Observation Context Budgeting Tests
# ============================================================================

class TestObservationContextBudgeting:
    """Verifies that large tool observation results are truncated for context budgeting."""

    def test_observation_truncated_when_exceeding_budget(self):
        """Tool observation exceeding 4000 characters is truncated with suffix."""
        mock_llm = MagicMock()
        mock_graph_tools = MagicMock()

        # Tool result with 6000 characters
        large_tool_result = "A" * 6000
        mock_res = MagicMock()
        mock_res.to_text.return_value = large_tool_result
        mock_graph_tools.quick_search.return_value = mock_res

        agent = _make_test_agent(mock_llm=mock_llm, mock_graph_tools=mock_graph_tools)

        round1 = '<tool_call>{"name": "quick_search", "parameters": {"query": "large"}}</tool_call>'
        round2 = (
            "Final Answer:\n"
            "Key evidence shows:\n\n"
            '> "The student body expressed sustained concern."\n\n'
            "Simulation confirms continued divergence across academic cohorts."
        )

        mock_llm.chat.side_effect = [round1, round2]

        section = ReportSection(title="Budget Section", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        content = agent._generate_section_react(section, outline, previous_sections=[])

        assert "student body expressed sustained concern" in content
        assert mock_llm.chat.call_count == 2

        # Check the observation message sent in round 2
        call2_messages = mock_llm.chat.call_args_list[1].kwargs["messages"]
        user_obs_msg = call2_messages[-1]["content"]

        # Truncation suffix must be present
        assert "... [observation truncated for context budgeting]" in user_obs_msg
        # The raw 6000 character string must NOT be present in full
        assert large_tool_result not in user_obs_msg
        # Observation content should be bounded
        assert len(user_obs_msg) < 5000

    def test_observation_preserved_when_under_budget(self):
        """Tool observation within 4000 characters is preserved without truncation suffix."""
        mock_llm = MagicMock()
        mock_graph_tools = MagicMock()

        short_tool_result = "Brief graph finding regarding sentiment."
        mock_res = MagicMock()
        mock_res.to_text.return_value = short_tool_result
        mock_graph_tools.quick_search.return_value = mock_res

        agent = _make_test_agent(mock_llm=mock_llm, mock_graph_tools=mock_graph_tools)

        round1 = '<tool_call>{"name": "quick_search", "parameters": {"query": "short"}}</tool_call>'
        round2 = (
            "Final Answer:\n"
            "Student sentiment was synthesized as follows:\n\n"
            '> "Campus groups mobilized swiftly."\n\n'
            "The data indicates rapid engagement."
        )

        mock_llm.chat.side_effect = [round1, round2]

        section = ReportSection(title="Short Budget Section", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        agent._generate_section_react(section, outline, previous_sections=[])

        call2_messages = mock_llm.chat.call_args_list[1].kwargs["messages"]
        user_obs_msg = call2_messages[-1]["content"]

        assert "... [observation truncated for context budgeting]" not in user_obs_msg
        assert short_tool_result in user_obs_msg

    def test_observation_boundary_exactly_4000_chars(self):
        """Tool observation of exactly 4000 characters is preserved without truncation notice."""
        mock_llm = MagicMock()
        mock_graph_tools = MagicMock()

        exact_4000_result = "X" * 4000
        mock_res = MagicMock()
        mock_res.to_text.return_value = exact_4000_result
        mock_graph_tools.quick_search.return_value = mock_res

        agent = _make_test_agent(mock_llm=mock_llm, mock_graph_tools=mock_graph_tools)

        round1 = '<tool_call>{"name": "quick_search", "parameters": {"query": "exact4000"}}</tool_call>'
        round2 = (
            "Final Answer:\n"
            "Key finding at exact boundary:\n\n"
            '> "Simulation data confirms stable metrics."\n\n'
            "Sustained convergence across all parameters."
        )

        mock_llm.chat.side_effect = [round1, round2]

        section = ReportSection(title="Exact 4000 Section", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        agent._generate_section_react(section, outline, previous_sections=[])

        call2_messages = mock_llm.chat.call_args_list[1].kwargs["messages"]
        user_obs_msg = call2_messages[-1]["content"]

        assert ReportAgent.OBSERVATION_TRUNCATION_SUFFIX not in user_obs_msg
        assert exact_4000_result in user_obs_msg

    def test_observation_boundary_4001_chars_truncated_with_exact_suffix_format(self):
        """Tool observation of 4001 characters is truncated at 4000 with exact suffix format."""
        mock_llm = MagicMock()
        mock_graph_tools = MagicMock()

        # 4000 of 'Y' followed by 1 of 'Z'
        chars_4001_result = ("Y" * 4000) + "Z"
        mock_res = MagicMock()
        mock_res.to_text.return_value = chars_4001_result
        mock_graph_tools.quick_search.return_value = mock_res

        agent = _make_test_agent(mock_llm=mock_llm, mock_graph_tools=mock_graph_tools)

        round1 = '<tool_call>{"name": "quick_search", "parameters": {"query": "boundary4001"}}</tool_call>'
        round2 = (
            "Final Answer:\n"
            "Boundary observation test passed:\n\n"
            '> "Verified boundary truncation logic."\n\n'
            "Consistent observation budgeting."
        )

        mock_llm.chat.side_effect = [round1, round2]

        section = ReportSection(title="Boundary 4001 Section", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        agent._generate_section_react(section, outline, previous_sections=[])

        call2_messages = mock_llm.chat.call_args_list[1].kwargs["messages"]
        user_obs_msg = call2_messages[-1]["content"]

        # Exact suffix format and position
        expected_budgeted = ("Y" * 4000) + f"\n{ReportAgent.OBSERVATION_TRUNCATION_SUFFIX}"
        assert expected_budgeted in user_obs_msg
        assert "Z" not in user_obs_msg

    def test_observation_truncation_notice_format_and_placement(self):
        """Verify the exact truncation notice format string and its placement in the observation template."""
        assert ReportAgent.OBSERVATION_TRUNCATION_SUFFIX == "... [observation truncated for context budgeting]"
        assert ReportAgent.MAX_OBSERVATION_CHARS == 4000

        mock_llm = MagicMock()
        mock_graph_tools = MagicMock()

        header = "HEADER_START\n"
        body = "M" * 4200
        tail = "\nTAIL_DATA"
        full_result = header + body + tail
        mock_res = MagicMock()
        mock_res.to_text.return_value = full_result
        mock_graph_tools.quick_search.return_value = mock_res

        agent = _make_test_agent(mock_llm=mock_llm, mock_graph_tools=mock_graph_tools)

        round1 = '<tool_call>{"name": "quick_search", "parameters": {"query": "template_test"}}</tool_call>'
        round2 = "Final Answer:\n> \"Verified format\"\n\nSynthesis complete."
        mock_llm.chat.side_effect = [round1, round2]

        section = ReportSection(title="Notice Format Section", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        agent._generate_section_react(section, outline, previous_sections=[])

        call2_messages = mock_llm.chat.call_args_list[1].kwargs["messages"]
        user_obs_msg = call2_messages[-1]["content"]

        # Formatted template structure
        assert "Observation (Retrieval Result):" in user_obs_msg
        assert "═══ Tool quick_search Returned ═══" in user_obs_msg
        assert header in user_obs_msg
        assert tail not in user_obs_msg
        assert f"\n{ReportAgent.OBSERVATION_TRUNCATION_SUFFIX}\n\n═══════════════════════════════════════════════════════════════" in user_obs_msg

    def test_previous_sections_context_budgeting_boundary(self):
        """Completed previous sections are truncated if exceeding 4000 characters."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        sec1_exact = "P" * 4000
        sec2_excess = ("Q" * 4000) + "EXTRA_TAIL"

        round1 = '<tool_call>{"name": "quick_search", "parameters": {"query": "boundary"}}</tool_call>'
        round2 = (
            "Final Answer:\n"
            "Key observation from simulation:\n\n"
            '> "Complete validation confirms convergence."\n\n'
            "End of section."
        )
        mock_llm.chat.side_effect = [round1, round2]

        section = ReportSection(title="Current Section", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        agent._generate_section_react(section, outline, previous_sections=[sec1_exact, sec2_excess])

        call1_messages = mock_llm.chat.call_args_list[0].kwargs["messages"]
        user_prompt = call1_messages[1]["content"]

        # sec1 exactly 4000 chars should not have '...' appended
        assert sec1_exact in user_prompt
        assert f"{sec1_exact}..." not in user_prompt
        # sec2 exceeding 4000 chars should be truncated to 4000 + '...'
        assert ("Q" * 4000) + "..." in user_prompt
        assert "EXTRA_TAIL" not in user_prompt


# ============================================================================
# 2. Adaptive Early-Exit Policy Tests
# ============================================================================

class TestAdaptiveEarlyExitPolicy:
    """Verifies that synthesized Final Answers are not discarded prematurely."""

    def test_early_exit_after_one_tool_call_with_quoted_evidence(self):
        """When 1 tool call is made and valid Final Answer with quote is present, loop completes."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = '<tool_call>{"name": "quick_search", "parameters": {"query": "quotes"}}</tool_call>'
        round2 = (
            "Final Answer:\n"
            "Initial campus observations revealed sharp polarization:\n\n"
            '> "The administrative response was completely detached from student needs."\n\n'
            "Further trajectory analysis shows an escalation in organized community forums."
        )

        mock_llm.chat.side_effect = [round1, round2]

        section = ReportSection(title="Quote Section", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        content = agent._generate_section_react(section, outline, previous_sections=[])

        assert "administrative response was completely detached" in content
        assert mock_llm.chat.call_count == 2  # Completed in 2 rounds rather than rejected

    def test_early_exit_after_one_tool_call_with_structured_synthesis(self):
        """When 1 tool call is made and Final Answer contains structured synthesis, loop completes."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = '<tool_call>{"name": "quick_search", "parameters": {"query": "synthesis"}}</tool_call>'
        round2 = (
            "Final Answer:\n"
            "The simulation reveals three dominant behavioral pathways:\n"
            "- **Pathway Alpha**: Swift grassroots mobilization within initial 6 hours\n"
            "- **Pathway Beta**: Administrative delays producing information vacuum\n"
            "- **Pathway Gamma**: Secondary support networks bridging communication gaps\n\n"
            "Overall trajectory projects stable convergence after 72 simulated hours."
        )

        mock_llm.chat.side_effect = [round1, round2]

        section = ReportSection(title="Synthesis Section", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        content = agent._generate_section_react(section, outline, previous_sections=[])

        assert "Pathway Alpha" in content
        assert mock_llm.chat.call_count == 2

    def test_no_early_exit_with_zero_tool_calls(self):
        """When 0 tool calls have been made, premature Final Answer is rejected."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        # Round 1 tries to output Final Answer immediately without calling any tools
        round1 = (
            "Final Answer:\n"
            "I already know everything about the future.\n\n"
            '> "Fabricated quote"\n\n'
            "No tools were needed."
        )
        round2 = '<tool_call>{"name": "quick_search", "parameters": {"query": "q1"}}</tool_call>'
        round3 = '<tool_call>{"name": "panorama_search", "parameters": {"query": "q2"}}</tool_call>'
        round4 = '<tool_call>{"name": "insight_forge", "parameters": {"query": "q3"}}</tool_call>'
        round5 = "Final Answer:\nProper section completed after observation."

        mock_llm.chat.side_effect = [round1, round2, round3, round4, round5]

        section = ReportSection(title="Zero Tool Calls", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        content = agent._generate_section_react(section, outline, previous_sections=[])

        assert content == "Proper section completed after observation."
        # Round 1 was rejected and requested more tools
        assert mock_llm.chat.call_count == 5

    def test_no_early_exit_with_stub_or_refusal_answer(self):
        """When Final Answer is an unsubstantiated stub or refusal, it is rejected."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = '<tool_call>{"name": "quick_search", "parameters": {"query": "q1"}}</tool_call>'
        # Stub/refusal response that is not synthesized
        round2 = "Final Answer:\nNeed more information to complete this section."
        round3 = '<tool_call>{"name": "panorama_search", "parameters": {"query": "q2"}}</tool_call>'
        round4 = '<tool_call>{"name": "insight_forge", "parameters": {"query": "q3"}}</tool_call>'
        round5 = "Final Answer:\nComplete section finally synthesized."

        mock_llm.chat.side_effect = [round1, round2, round3, round4, round5]

        section = ReportSection(title="Stub Answer", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        content = agent._generate_section_react(section, outline, previous_sections=[])

        assert content == "Complete section finally synthesized."
        assert mock_llm.chat.call_count == 5

    def test_adaptive_early_exit_case3_no_final_answer_prefix(self):
        """When LLM provides synthesized markdown body without 'Final Answer:' prefix after 1 tool call."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = '<tool_call>{"name": "quick_search", "parameters": {"query": "case3"}}</tool_call>'
        round2 = (
            "Analysis of simulated dynamics reveals significant structural shifts:\n\n"
            '> "Community trust eroded during the initial response phase."\n\n'
            "Key observed dynamics indicate proactive alignment among departmental leads."
        )

        mock_llm.chat.side_effect = [round1, round2]

        section = ReportSection(title="Case 3 Section", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        content = agent._generate_section_react(section, outline, previous_sections=[])

        assert "Community trust eroded" in content
        assert mock_llm.chat.call_count == 2

    def test_is_valid_synthesized_answer_empty_and_subminimal(self):
        """Answers that are None, empty, whitespace, or < 40 characters are invalid."""
        assert not ReportAgent._is_valid_synthesized_answer(None)
        assert not ReportAgent._is_valid_synthesized_answer("")
        assert not ReportAgent._is_valid_synthesized_answer("   \n\t  ")
        assert not ReportAgent._is_valid_synthesized_answer("Brief line under forty.")  # 23 chars

    def test_is_valid_synthesized_answer_rejection_phrases_boundary(self):
        """Phrases indicating need for more search or refusal are rejected under 150 characters."""
        rejection_cases = [
            "Need more information to complete this section about future campus trends.",
            "Insufficient data from simulation graph to draw any conclusions here.",
            "Cannot generate without further details regarding student cohorts.",
            "Please call more tools to explore the network nodes and relationships.",
            "Need to search the graph first before synthesizing conclusions.",
            "Please provide more guidance on what aspects to focus on.",
        ]
        for phrase in rejection_cases:
            assert not ReportAgent._is_valid_synthesized_answer(phrase)

        # Substantive analysis >= 150 chars containing these words in context should pass
        substantive = (
            "The simulated students voiced frustration regarding campus transparency. "
            "Many reported they need more information from administration, while grassroots collectives began autonomous deliberation.\n\n"
            '> "We cannot rely on official updates alone."\n\n'
            "Overall alignment remained fragile."
        )
        assert len(substantive) >= 150
        assert ReportAgent._is_valid_synthesized_answer(substantive)

    def test_is_valid_synthesized_answer_plain_unstructured_text_rejected(self):
        """Unstructured plain text without quotes, formatting, or paragraphs under 120 chars is rejected."""
        unstructured = "The campus simulation proceeded with various agents interacting across Twitter and Reddit platforms."
        assert len(unstructured) < 120
        assert not ReportAgent._is_valid_synthesized_answer(unstructured)

    def test_is_valid_synthesized_answer_evidence_quotes(self):
        """Evidence quotes in various styles (blockquote, double quotes, single quotes, CJK quotes) are accepted."""
        # Markdown blockquote
        assert ReportAgent._is_valid_synthesized_answer(
            "Initial observation showed:\n\n> \"Trust deteriorated swiftly.\"\n\nTrends emerged."
        )
        # Double quotes
        assert ReportAgent._is_valid_synthesized_answer(
            'The initial trajectory highlights "rapid divergence" across cohorts in the simulation.'
        )
        # Single quotes
        assert ReportAgent._is_valid_synthesized_answer(
            "The initial trajectory highlights 'rapid divergence' across cohorts in the simulation."
        )
        # Chinese double quotes
        assert ReportAgent._is_valid_synthesized_answer(
            "在针对校园突发事件的模拟演化中，学生群体普遍认为 “学校响应过于迟缓” 并发起了线上讨论议题。"
        )
        # Chinese corner brackets
        assert ReportAgent._is_valid_synthesized_answer(
            "在针对后续政策推进的模拟演化中，观察显示 「关键意见领袖」 在演化过程中起到了决定性作用。"
        )

    def test_is_valid_synthesized_answer_structural_patterns(self):
        """Markdown bold, bullet lists, numbered lists, multi-paragraphs, or >=120 chars are accepted."""
        # Bold text
        assert ReportAgent._is_valid_synthesized_answer(
            "Analysis demonstrates **sharp polarization** during the initial twelve hours."
        )
        # Hyphen bullet
        assert ReportAgent._is_valid_synthesized_answer(
            "Key observed dynamic:\n- Student organizations coordinated community forums"
        )
        # Asterisk bullet
        assert ReportAgent._is_valid_synthesized_answer(
            "Key observed dynamic:\n* Department chairs scheduled emergency meetings"
        )
        # Unicode bullet
        assert ReportAgent._is_valid_synthesized_answer(
            "Key observed dynamic:\n• Student leadership drafted collective statements"
        )
        # Numbered list
        assert ReportAgent._is_valid_synthesized_answer(
            "Observed phases:\n1. Initial shockwave spread across digital platforms"
        )
        # Multi-paragraph
        assert ReportAgent._is_valid_synthesized_answer(
            "First paragraph analyzing initial community reaction.\n\nSecond paragraph evaluating subsequent trajectory."
        )
        # Substantial length (>= 120 chars)
        substantial_plain = "A" * 125
        assert ReportAgent._is_valid_synthesized_answer(substantial_plain)

    def test_unsubstantiated_answer_rejected_forces_further_tool_calls(self):
        """An unsubstantiated answer without quote or structure is rejected, forcing loop to continue."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = '<tool_call>{"name": "quick_search", "parameters": {"query": "q1"}}</tool_call>'
        # Unsubstantiated answer: no quotes, no markdown structure, single paragraph under 120 chars
        round2 = "Final Answer:\nThe student body had mixed reactions during the event."
        round3 = '<tool_call>{"name": "panorama_search", "parameters": {"query": "q2"}}</tool_call>'
        round4 = '<tool_call>{"name": "insight_forge", "parameters": {"query": "q3"}}</tool_call>'
        round5 = (
            "Final Answer:\n"
            "Verified structured conclusion:\n\n"
            '> "Comprehensive simulation validation completed."\n\n'
            "End of section."
        )

        mock_llm.chat.side_effect = [round1, round2, round3, round4, round5]

        section = ReportSection(title="Unsubstantiated Rejection", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        content = agent._generate_section_react(section, outline, previous_sections=[])

        assert "Comprehensive simulation validation completed" in content
        assert mock_llm.chat.call_count == 5

        # Check that round 2 rejection prompted for more tools
        call3_messages = mock_llm.chat.call_args_list[2].kwargs["messages"]
        rejection_messages = [
            m["content"] for m in call3_messages
            if "[Notice] You have only called 1 tools" in m.get("content", "")
        ]
        assert len(rejection_messages) == 1
        assert "need at least 3" in rejection_messages[0]

    def test_early_exit_after_single_tool_call_with_cjk_quotes(self):
        """When 1 tool call is made and Final Answer contains CJK quotes, loop completes early."""
        mock_llm = MagicMock()
        agent = _make_test_agent(mock_llm=mock_llm)

        round1 = '<tool_call>{"name": "quick_search", "parameters": {"query": "cjk"}}</tool_call>'
        round2 = (
            "Final Answer:\n"
            "模拟演化数据显示各群体呈现明显分歧趋势：\n\n"
            '> “管理层公开声明未能有效平息争议。”\n\n'
            "随后多社群展开进一步协商并形成共识机制。"
        )

        mock_llm.chat.side_effect = [round1, round2]

        section = ReportSection(title="CJK Quote Section", content="")
        outline = ReportOutline(title="Report", summary="Summary", sections=[section])

        content = agent._generate_section_react(section, outline, previous_sections=[])

        assert "管理层公开声明未能有效平息争议" in content
        assert mock_llm.chat.call_count == 2


# ============================================================================
# 3. In-Memory Simulation Report Cache Tests
# ============================================================================

class TestSimulationReportCache:
    """Verifies that ReportManager maintains an in-memory thread-safe simulation_id -> report_id cache."""

    @pytest.fixture(autouse=True)
    def clean_cache(self, tmp_path, monkeypatch):
        """Set up temporary reports directory and clean cache before and after test."""
        reports_dir = os.path.join(tmp_path, "reports")
        os.makedirs(reports_dir, exist_ok=True)
        monkeypatch.setattr(ReportManager, "REPORTS_DIR", str(reports_dir))
        ReportManager.clear_cache()
        yield
        ReportManager.clear_cache()

    def test_save_report_populates_in_memory_cache(self):
        """save_report updates _simulation_report_cache."""
        report = Report(
            report_id="rep_101",
            simulation_id="sim_202",
            graph_id="graph_303",
            simulation_requirement="Test requirement",
            status=ReportStatus.COMPLETED,
            markdown_content="# Test Content",
        )

        ReportManager.save_report(report)

        with ReportManager._cache_lock:
            assert ReportManager._simulation_report_cache.get("sim_202") == "rep_101"

    def test_get_report_by_simulation_uses_cache_o1(self):
        """get_report_by_simulation retrieves from cache without scanning directory on hit."""
        report = Report(
            report_id="rep_cache_hit",
            simulation_id="sim_cache_hit",
            graph_id="graph_test",
            simulation_requirement="Test requirement",
            status=ReportStatus.COMPLETED,
            markdown_content="# Hit Content",
        )
        ReportManager.save_report(report)

        # Patch os.listdir to verify that it is NOT called when cache hits
        with patch("os.listdir") as mock_listdir:
            retrieved = ReportManager.get_report_by_simulation("sim_cache_hit")
            assert retrieved is not None
            assert retrieved.report_id == "rep_cache_hit"
            assert retrieved.simulation_id == "sim_cache_hit"
            mock_listdir.assert_not_called()

    def test_get_report_by_simulation_evicts_stale_cache_and_falls_back_to_disk(self):
        """Stale cache entry is evicted and falls back to disk scanning."""
        report = Report(
            report_id="rep_real",
            simulation_id="sim_test",
            graph_id="graph_test",
            simulation_requirement="Test requirement",
            status=ReportStatus.COMPLETED,
            markdown_content="# Real Content",
        )
        ReportManager.save_report(report)

        # Inject stale cache entry pointing to non-existent report_id
        with ReportManager._cache_lock:
            ReportManager._simulation_report_cache["sim_test"] = "non_existent_report"

        retrieved = ReportManager.get_report_by_simulation("sim_test")
        assert retrieved is not None
        assert retrieved.report_id == "rep_real"

        # Cache should now be corrected
        with ReportManager._cache_lock:
            assert ReportManager._simulation_report_cache.get("sim_test") == "rep_real"

    def test_delete_report_invalidates_cache(self):
        """delete_report removes mapping from in-memory cache."""
        report = Report(
            report_id="rep_del",
            simulation_id="sim_del",
            graph_id="graph_test",
            simulation_requirement="Test requirement",
            status=ReportStatus.COMPLETED,
        )
        ReportManager.save_report(report)

        with ReportManager._cache_lock:
            assert "sim_del" in ReportManager._simulation_report_cache

        ReportManager.delete_report("rep_del")

        with ReportManager._cache_lock:
            assert "sim_del" not in ReportManager._simulation_report_cache

    def test_concurrent_cache_access_thread_safety(self):
        """Concurrent multi-threaded writes and reads to the cache do not raise exceptions."""
        errors = []

        def worker(thread_idx: int):
            try:
                for i in range(20):
                    sim_id = f"sim_thread_{thread_idx}_{i}"
                    rep_id = f"rep_thread_{thread_idx}_{i}"
                    report = Report(
                        report_id=rep_id,
                        simulation_id=sim_id,
                        graph_id="graph_test",
                        simulation_requirement="Concurrency test",
                        status=ReportStatus.COMPLETED,
                    )
                    ReportManager.save_report(report)
                    retrieved = ReportManager.get_report_by_simulation(sim_id)
                    assert retrieved is not None
                    assert retrieved.report_id == rep_id
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors in concurrent cache access: {errors}"

    def test_delete_report_legacy_json_and_md_evicts_cache(self, tmp_path):
        """delete_report invalidates cache when deleting legacy flat-file reports."""
        reports_dir = os.path.join(tmp_path, "reports")
        legacy_rep_id = "rep_legacy_999"
        legacy_sim_id = "sim_legacy_888"

        json_path = os.path.join(reports_dir, f"{legacy_rep_id}.json")
        md_path = os.path.join(reports_dir, f"{legacy_rep_id}.md")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "report_id": legacy_rep_id,
                "simulation_id": legacy_sim_id,
                "graph_id": "graph_legacy",
                "simulation_requirement": "Legacy requirement",
                "status": "completed",
            }, f)
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# Legacy Report Content")

        with ReportManager._cache_lock:
            ReportManager._simulation_report_cache[legacy_sim_id] = legacy_rep_id

        assert ReportManager.delete_report(legacy_rep_id) is True

        with ReportManager._cache_lock:
            assert legacy_sim_id not in ReportManager._simulation_report_cache

        assert not os.path.exists(json_path)
        assert not os.path.exists(md_path)

    def test_delete_report_selective_eviction_multiple_entries(self):
        """delete_report only removes the targeted report's simulation_id, leaving others intact."""
        rep1 = Report(
            report_id="rep_target",
            simulation_id="sim_target",
            graph_id="graph_test",
            simulation_requirement="Target sim",
            status=ReportStatus.COMPLETED,
        )
        rep2 = Report(
            report_id="rep_keep",
            simulation_id="sim_keep",
            graph_id="graph_test",
            simulation_requirement="Keep sim",
            status=ReportStatus.COMPLETED,
        )
        ReportManager.save_report(rep1)
        ReportManager.save_report(rep2)

        with ReportManager._cache_lock:
            assert ReportManager._simulation_report_cache.get("sim_target") == "rep_target"
            assert ReportManager._simulation_report_cache.get("sim_keep") == "rep_keep"

        ReportManager.delete_report("rep_target")

        with ReportManager._cache_lock:
            assert "sim_target" not in ReportManager._simulation_report_cache
            assert ReportManager._simulation_report_cache.get("sim_keep") == "rep_keep"

    def test_disk_miss_recovery_evicts_stale_cache_when_no_disk_report(self):
        """get_report_by_simulation evicts cache and returns None when report is absent on disk."""
        with ReportManager._cache_lock:
            ReportManager._simulation_report_cache["sim_ghost"] = "rep_ghost"

        retrieved = ReportManager.get_report_by_simulation("sim_ghost")
        assert retrieved is None

        with ReportManager._cache_lock:
            assert "sim_ghost" not in ReportManager._simulation_report_cache

    def test_disk_miss_recovery_when_disk_report_simulation_id_mismatched(self):
        """get_report_by_simulation evicts stale entry if cached report points to wrong simulation."""
        rep = Report(
            report_id="rep_mismatched",
            simulation_id="sim_actual",
            graph_id="graph_test",
            simulation_requirement="Actual sim",
            status=ReportStatus.COMPLETED,
        )
        ReportManager.save_report(rep)

        # Inject stale cache mapping "sim_stale" -> "rep_mismatched"
        with ReportManager._cache_lock:
            ReportManager._simulation_report_cache["sim_stale"] = "rep_mismatched"

        retrieved = ReportManager.get_report_by_simulation("sim_stale")
        assert retrieved is None

        with ReportManager._cache_lock:
            assert "sim_stale" not in ReportManager._simulation_report_cache

    def test_concurrent_cache_lookups_and_disk_reloads_thread_safety(self):
        """Concurrent lookups with interleaved cache clearing and deletes maintain consistency."""
        errors = []

        # Prepopulate several reports
        for i in range(10):
            rep = Report(
                report_id=f"rep_conc_{i}",
                simulation_id=f"sim_conc_{i}",
                graph_id="graph_test",
                simulation_requirement=f"Conc test {i}",
                status=ReportStatus.COMPLETED,
            )
            ReportManager.save_report(rep)

        def reader(idx: int):
            try:
                for _ in range(30):
                    sim_id = f"sim_conc_{idx % 10}"
                    rep = ReportManager.get_report_by_simulation(sim_id)
                    # When report exists on disk, it must either be returned or recovered
                    if rep is not None:
                        assert rep.simulation_id == sim_id
            except Exception as e:
                errors.append(e)

        def modifier():
            try:
                for _ in range(15):
                    ReportManager.clear_cache()
                    time.sleep(0.002)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader, args=(i,)) for i in range(8)]
        threads.append(threading.Thread(target=modifier))

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"Errors in concurrent cache lookups/reloads: {errors}"


# ============================================================================
# 4. Incremental Agent Log Reading Tests
# ============================================================================

class TestIncrementalAgentLog:
    """Verifies that get_agent_log reads incrementally and skips JSON decoding for earlier lines."""

    @pytest.fixture(autouse=True)
    def setup_log(self, tmp_path, monkeypatch):
        """Set up temporary report directory with agent_log.jsonl."""
        reports_dir = os.path.join(tmp_path, "reports")
        report_dir = os.path.join(reports_dir, "rep_test_log")
        os.makedirs(report_dir, exist_ok=True)
        monkeypatch.setattr(ReportManager, "REPORTS_DIR", str(reports_dir))

        log_path = os.path.join(report_dir, "agent_log.jsonl")
        entries = []
        for i in range(50):
            entries.append(json.dumps({
                "timestamp": "2026-10-31T12:00:00",
                "elapsed_seconds": i * 0.5,
                "report_id": "rep_test_log",
                "action": f"action_{i}",
                "stage": "generating",
                "details": {"line_num": i}
            }))
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(entries) + "\n")

        yield "rep_test_log"

    def test_get_agent_log_incremental_reads_from_line(self, setup_log):
        """get_agent_log with from_line=40 only returns lines 40 to 49."""
        report_id = setup_log
        res = ReportManager.get_agent_log(report_id, from_line=40)

        assert res["total_lines"] == 50
        assert res["from_line"] == 40
        assert len(res["logs"]) == 10
        assert res["logs"][0]["action"] == "action_40"
        assert res["logs"][-1]["action"] == "action_49"

    def test_get_agent_log_from_line_greater_than_total(self, setup_log):
        """get_agent_log with from_line > total_lines returns empty logs."""
        report_id = setup_log
        res = ReportManager.get_agent_log(report_id, from_line=100)

        assert res["total_lines"] == 50
        assert res["from_line"] == 100
        assert len(res["logs"]) == 0

    def test_get_agent_log_negative_from_line_clamped(self, setup_log):
        """get_agent_log with negative from_line starts from 0."""
        report_id = setup_log
        res = ReportManager.get_agent_log(report_id, from_line=-10)

        assert res["total_lines"] == 50
        assert res["from_line"] == 0
        assert len(res["logs"]) == 50

    def test_get_agent_log_boundary_offsets(self, setup_log):
        """get_agent_log with boundary offsets (0, 1, middle, last, exact total, past total, negative)."""
        report_id = setup_log

        # Offset 0: all lines
        res0 = ReportManager.get_agent_log(report_id, from_line=0)
        assert res0["total_lines"] == 50
        assert res0["from_line"] == 0
        assert len(res0["logs"]) == 50
        assert res0["logs"][0]["action"] == "action_0"

        # Offset 1: all except line 0
        res1 = ReportManager.get_agent_log(report_id, from_line=1)
        assert res1["total_lines"] == 50
        assert res1["from_line"] == 1
        assert len(res1["logs"]) == 49
        assert res1["logs"][0]["action"] == "action_1"

        # Offset 25: middle
        res25 = ReportManager.get_agent_log(report_id, from_line=25)
        assert res25["total_lines"] == 50
        assert res25["from_line"] == 25
        assert len(res25["logs"]) == 25
        assert res25["logs"][0]["action"] == "action_25"

        # Offset 49: last single line
        res49 = ReportManager.get_agent_log(report_id, from_line=49)
        assert res49["total_lines"] == 50
        assert res49["from_line"] == 49
        assert len(res49["logs"]) == 1
        assert res49["logs"][0]["action"] == "action_49"

        # Offset 50: exactly at EOF
        res50 = ReportManager.get_agent_log(report_id, from_line=50)
        assert res50["total_lines"] == 50
        assert res50["from_line"] == 50
        assert len(res50["logs"]) == 0

        # Offset 51: beyond EOF
        res51 = ReportManager.get_agent_log(report_id, from_line=51)
        assert res51["total_lines"] == 50
        assert res51["from_line"] == 51
        assert len(res51["logs"]) == 0

        # Negative offset: clamped to 0
        res_neg = ReportManager.get_agent_log(report_id, from_line=-1)
        assert res_neg["total_lines"] == 50
        assert res_neg["from_line"] == 0
        assert len(res_neg["logs"]) == 50

    def test_get_agent_log_skips_malformed_json_before_from_line(self, tmp_path, monkeypatch):
        """get_agent_log skips lines before from_line without JSON decoding errors."""
        reports_dir = os.path.join(tmp_path, "reports")
        report_dir = os.path.join(reports_dir, "rep_corrupt")
        os.makedirs(report_dir, exist_ok=True)
        monkeypatch.setattr(ReportManager, "REPORTS_DIR", str(reports_dir))

        log_path = os.path.join(report_dir, "agent_log.jsonl")
        # Lines 0-4 are invalid JSON; Lines 5-9 are valid JSON
        lines = [
            "NOT_JSON_LINE_0",
            "{bad json line 1",
            "!!! CORRUPT 2",
            "INVALID 3",
            "BROKEN 4",
        ]
        for i in range(5, 10):
            lines.append(json.dumps({
                "report_id": "rep_corrupt",
                "action": f"valid_action_{i}",
                "stage": "generating",
            }))

        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        # from_line=5 skips lines 0-4 without attempting json.loads
        res = ReportManager.get_agent_log("rep_corrupt", from_line=5)
        assert res["total_lines"] == 10
        assert res["from_line"] == 5
        assert len(res["logs"]) == 5
        assert res["logs"][0]["action"] == "valid_action_5"
        assert res["logs"][-1]["action"] == "valid_action_9"

    def test_get_agent_log_handles_malformed_json_after_from_line_gracefully(self, tmp_path, monkeypatch):
        """get_agent_log ignores invalid JSON lines after from_line without crashing."""
        reports_dir = os.path.join(tmp_path, "reports")
        report_dir = os.path.join(reports_dir, "rep_corrupt_tail")
        os.makedirs(report_dir, exist_ok=True)
        monkeypatch.setattr(ReportManager, "REPORTS_DIR", str(reports_dir))

        log_path = os.path.join(report_dir, "agent_log.jsonl")
        lines = [
            json.dumps({"action": "act_0"}),
            "MALFORMED_JSON_LINE",
            json.dumps({"action": "act_2"}),
        ]
        with open(log_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

        res = ReportManager.get_agent_log("rep_corrupt_tail", from_line=0)
        assert res["total_lines"] == 3
        assert len(res["logs"]) == 2
        assert res["logs"][0]["action"] == "act_0"
        assert res["logs"][1]["action"] == "act_2"

    def test_get_agent_log_missing_and_empty_file(self, tmp_path, monkeypatch):
        """get_agent_log handles non-existent or 0-byte log files cleanly."""
        reports_dir = os.path.join(tmp_path, "reports")
        os.makedirs(reports_dir, exist_ok=True)
        monkeypatch.setattr(ReportManager, "REPORTS_DIR", str(reports_dir))

        # Missing report folder
        res_missing = ReportManager.get_agent_log("non_existent_rep", from_line=0)
        assert res_missing == {"logs": [], "total_lines": 0, "from_line": 0, "has_more": False}

        # Empty log file (0 bytes)
        empty_dir = os.path.join(reports_dir, "rep_empty")
        os.makedirs(empty_dir, exist_ok=True)
        open(os.path.join(empty_dir, "agent_log.jsonl"), "w").close()

        res_empty = ReportManager.get_agent_log("rep_empty", from_line=0)
        assert res_empty == {"logs": [], "total_lines": 0, "from_line": 0, "has_more": False}

    def test_get_console_log_incremental_reading_and_boundary_offsets(self, tmp_path, monkeypatch):
        """get_console_log reads console_log.txt incrementally with boundary offsets."""
        reports_dir = os.path.join(tmp_path, "reports")
        report_dir = os.path.join(reports_dir, "rep_console")
        os.makedirs(report_dir, exist_ok=True)
        monkeypatch.setattr(ReportManager, "REPORTS_DIR", str(reports_dir))

        console_path = os.path.join(report_dir, "console_log.txt")
        raw_lines = [f"[12:00:{i:02d}] INFO: Log message line {i}" for i in range(30)]
        with open(console_path, "w", encoding="utf-8") as f:
            f.write("\n".join(raw_lines) + "\n")

        # Offset 0: all 30 lines
        res0 = ReportManager.get_console_log("rep_console", from_line=0)
        assert res0["total_lines"] == 30
        assert len(res0["logs"]) == 30
        assert res0["logs"][0] == raw_lines[0]

        # Offset 15: 15 lines
        res15 = ReportManager.get_console_log("rep_console", from_line=15)
        assert res15["total_lines"] == 30
        assert len(res15["logs"]) == 15
        assert res15["logs"][0] == raw_lines[15]

        # Offset 29: last line
        res29 = ReportManager.get_console_log("rep_console", from_line=29)
        assert res29["total_lines"] == 30
        assert len(res29["logs"]) == 1
        assert res29["logs"][0] == raw_lines[29]

        # Offset 30: at EOF
        res30 = ReportManager.get_console_log("rep_console", from_line=30)
        assert res30["total_lines"] == 30
        assert len(res30["logs"]) == 0

        # Offset 100: beyond EOF
        res100 = ReportManager.get_console_log("rep_console", from_line=100)
        assert res100["total_lines"] == 30
        assert len(res100["logs"]) == 0

        # Negative offset: clamped to 0
        res_neg = ReportManager.get_console_log("rep_console", from_line=-5)
        assert res_neg["total_lines"] == 30
        assert len(res_neg["logs"]) == 30

        # Missing console log
        res_missing = ReportManager.get_console_log("rep_missing", from_line=0)
        assert res_missing == {"logs": [], "total_lines": 0, "from_line": 0, "has_more": False}
