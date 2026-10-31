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
