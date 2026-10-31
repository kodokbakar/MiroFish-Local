import json
from unittest.mock import MagicMock

import pytest

from app.services.simulation_runner import (
    AgentAction,
    RunnerStatus,
    SimulationRunner,
    SimulationRunState,
)


@pytest.fixture(autouse=True)
def clean_simulation_runner_state():
    with SimulationRunner._action_cache_lock:
        SimulationRunner._action_cache.clear()
    SimulationRunner._run_states.clear()
    yield
    with SimulationRunner._action_cache_lock:
        SimulationRunner._action_cache.clear()
    SimulationRunner._run_states.clear()


def test_partial_line_preserves_offset_and_parses_on_completion(tmp_path):
    """
    Test writing a partial JSON line without \n, verifying _read_action_log
    preserves the offset, does not drop the action, and then parses it
    successfully when the remainder with \n is appended.
    """
    sim_id = "sim_partial_read_test"
    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.RUNNING)
    log_file = tmp_path / "actions.jsonl"

    partial_line = '{"round": 1, "agent_id": 42, "agent_name": "AgentX", "action_type": "POST"'
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(partial_line)

    # First read: line does not end with \n
    pos = SimulationRunner._read_action_log(str(log_file), 0, state, "twitter")
    assert pos == 0, "Byte offset must remain at start when incomplete line is encountered"
    assert len(state.recent_actions) == 0
    assert len(state.actions) == 0
    with SimulationRunner._action_cache_lock:
        assert len(SimulationRunner._action_cache.get(sim_id, [])) == 0

    # Append the remainder of the JSON line with newline
    remainder = ', "timestamp": "2026-01-01T10:00:00", "success": true}\n'
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(remainder)

    # Second read: line is now complete with \n
    new_pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
    assert new_pos > 0, "Byte offset must advance after full line read"
    assert len(state.recent_actions) == 1
    assert len(state.actions) == 1

    action = state.actions[0]
    assert action.agent_id == 42
    assert action.agent_name == "AgentX"
    assert action.action_type == "POST"
    assert action.platform == "twitter"

    with SimulationRunner._action_cache_lock:
        cached = SimulationRunner._action_cache.get(sim_id)
        assert cached is not None
        assert len(cached) == 1
        assert cached[0].agent_id == 42
        assert cached[0].platform == "twitter"


def test_multiple_complete_lines_followed_by_partial_line(tmp_path):
    """
    Test multiple complete lines followed by a partial line.
    Verify offset advances past complete lines but stops before partial line.
    """
    sim_id = "sim_multi_lines_partial_test"
    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.RUNNING)
    log_file = tmp_path / "actions.jsonl"

    line1 = json.dumps({
        "round": 1,
        "timestamp": "2026-01-01T10:00:00",
        "agent_id": 1,
        "agent_name": "Agent1",
        "action_type": "POST",
    }) + "\n"
    line2 = json.dumps({
        "round": 1,
        "timestamp": "2026-01-01T10:01:00",
        "agent_id": 2,
        "agent_name": "Agent2",
        "action_type": "COMMENT",
    }) + "\n"
    partial_line3 = '{"round": 2, "agent_id": 3, "agent_name": "Agent3"'

    with open(log_file, "w", encoding="utf-8") as f:
        f.write(line1)
        f.write(line2)
        expected_valid_offset = f.tell()
        f.write(partial_line3)

    # First read: should read line1 and line2, but rewind at partial_line3
    pos = SimulationRunner._read_action_log(str(log_file), 0, state, "reddit")
    assert pos == expected_valid_offset
    assert len(state.actions) == 2
    assert state.actions[0].agent_id == 1
    assert state.actions[1].agent_id == 2

    with SimulationRunner._action_cache_lock:
        cached = SimulationRunner._action_cache.get(sim_id, [])
        assert len(cached) == 2

    # Append remainder for line3
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(', "action_type": "UPVOTE", "timestamp": "2026-01-01T10:02:00"}\n')

    # Second read: should resume from pos and parse line3
    new_pos = SimulationRunner._read_action_log(str(log_file), pos, state, "reddit")
    assert new_pos > pos
    assert len(state.actions) == 3
    assert state.actions[2].agent_id == 3
    assert state.actions[2].action_type == "UPVOTE"

    with SimulationRunner._action_cache_lock:
        cached = SimulationRunner._action_cache.get(sim_id, [])
        assert len(cached) == 3
        assert cached[2].agent_id == 3


def test_partial_event_log_preserved_until_completed(tmp_path):
    """
    Test that event entries (simulation_end, round_end) with partial line writes
    are also preserved and not applied until fully written with \n.
    """
    sim_id = "sim_event_partial_test"
    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.RUNNING)
    log_file = tmp_path / "actions.jsonl"

    action_line = json.dumps({
        "round": 1,
        "timestamp": "2026-01-01T10:00:00",
        "agent_id": 1,
        "agent_name": "Agent1",
        "action_type": "POST",
    }) + "\n"
    partial_event = '{"event_type": "round_end", "round": 1, "simulated_hours": 2'

    with open(log_file, "w", encoding="utf-8") as f:
        f.write(action_line)
        expected_offset = f.tell()
        f.write(partial_event)

    pos = SimulationRunner._read_action_log(str(log_file), 0, state, "twitter")
    assert pos == expected_offset
    assert state.twitter_simulated_hours == 0
    assert len(state.actions) == 1

    # Finish writing event line
    with open(log_file, "a", encoding="utf-8") as f:
        f.write('}\n')

    new_pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
    assert new_pos > pos
    assert state.twitter_simulated_hours == 2
    assert state.twitter_current_round == 1


def test_mock_state_with_actions_list_without_add_action(tmp_path):
    """
    Verify state.actions receives actions even if state is a custom/mock object
    with only an actions list attribute and simulation_id.
    """
    class MockState:
        def __init__(self):
            self.simulation_id = "mock_sim_123"
            self.actions = []

    mock_state = MockState()
    log_file = tmp_path / "actions.jsonl"
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "round": 1,
            "agent_id": 99,
            "agent_name": "MockAgent",
            "action_type": "TWEET",
        }) + "\n")

    pos = SimulationRunner._read_action_log(str(log_file), 0, mock_state, "twitter")
    assert pos > 0
    assert len(mock_state.actions) == 1
    assert mock_state.actions[0].agent_id == 99
    assert mock_state.actions[0].action_type == "TWEET"
