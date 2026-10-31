import json
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.services.graph_memory_updater import GraphMemoryManager
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
    SimulationRunner._graph_memory_enabled.clear()
    yield
    with SimulationRunner._action_cache_lock:
        SimulationRunner._action_cache.clear()
    SimulationRunner._run_states.clear()
    SimulationRunner._graph_memory_enabled.clear()


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


def test_concurrent_partial_reads_high_frequency_writes(tmp_path):
    """
    Test live streaming simulation with concurrent high-frequency chunked writes.
    A writer thread writes multiple actions split into arbitrary byte fragments.
    A reader thread concurrently polls _read_action_log.
    Verifies thread safety, offset preservation, no dropped/duplicate actions,
    and eventual consistency.
    """
    sim_id = "sim_concurrent_stream_test"
    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.RUNNING)
    log_file = tmp_path / "actions.jsonl"
    log_file.touch()

    total_actions = 40
    writer_done = threading.Event()
    writer_error = []

    def writer():
        try:
            with open(log_file, "a", encoding="utf-8") as f:
                for i in range(total_actions):
                    action_dict = {
                        "round": (i // 5) + 1,
                        "timestamp": f"2026-01-01T10:{i:02d}:00",
                        "agent_id": i,
                        "agent_name": f"Agent_{i}",
                        "action_type": "POST" if i % 2 == 0 else "COMMENT",
                        "success": True,
                    }
                    line = json.dumps(action_dict) + "\n"
                    # Split the line into variable fragments (e.g. 9 chars)
                    frag_size = 9
                    for start in range(0, len(line), frag_size):
                        chunk = line[start : start + frag_size]
                        f.write(chunk)
                        f.flush()
                        time.sleep(0.0005)
        except Exception as e:
            writer_error.append(e)
        finally:
            writer_done.set()

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()

    pos = 0
    poll_iterations = 0
    max_poll_iterations = 1000

    while (not writer_done.is_set() or len(state.actions) < total_actions) and poll_iterations < max_poll_iterations:
        pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
        poll_iterations += 1
        time.sleep(0.001)

    writer_thread.join(timeout=5.0)
    assert not writer_thread.is_alive(), "Writer thread did not terminate in time"
    assert len(writer_error) == 0, f"Writer encountered error: {writer_error}"

    # Final drain poll in case writer finished right after loop check
    pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")

    assert len(state.actions) == total_actions
    assert [a.agent_id for a in state.actions] == list(range(total_actions))
    assert all(a.platform == "twitter" for a in state.actions)
    assert state.twitter_actions_count == total_actions
    assert pos == os.path.getsize(str(log_file))

    with SimulationRunner._action_cache_lock:
        cached = SimulationRunner._action_cache.get(sim_id, [])
        assert len(cached) == total_actions
        assert [a.agent_id for a in cached] == list(range(total_actions))


def test_multi_platform_streaming_interleaved_partial_writes(tmp_path, monkeypatch):
    """
    Test multi-platform streaming where Twitter and Reddit log files experience
    interleaved partial writes, completions, round events, and simulation_end events.
    Verifies independent position tracking, per-platform counters, round tracking,
    and synchronized completion status.
    """
    sim_id = "sim_multi_platform_interleaved"
    sim_dir = tmp_path / sim_id
    twitter_dir = sim_dir / "twitter"
    reddit_dir = sim_dir / "reddit"
    twitter_dir.mkdir(parents=True)
    reddit_dir.mkdir(parents=True)
    twitter_log = twitter_dir / "actions.jsonl"
    reddit_log = reddit_dir / "actions.jsonl"

    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))

    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.RUNNING)
    tw_pos = 0
    rd_pos = 0

    # Step 1: Interleaved partial writes for both platforms
    with open(twitter_log, "w", encoding="utf-8") as f_tw:
        f_tw.write('{"round": 1, "agent_id": 10, "agent_name": "TwAgent1", "action_type": "TWEET"')
    with open(reddit_log, "w", encoding="utf-8") as f_rd:
        f_rd.write('{"round": 1, "agent_id": 20, "agent_name": "RdAgent1", "action_type": "POST"')

    # Reading both: offsets must stay 0, 0 actions
    tw_pos = SimulationRunner._read_action_log(str(twitter_log), tw_pos, state, "twitter")
    rd_pos = SimulationRunner._read_action_log(str(reddit_log), rd_pos, state, "reddit")
    assert tw_pos == 0
    assert rd_pos == 0
    assert state.twitter_actions_count == 0
    assert state.reddit_actions_count == 0
    assert len(state.actions) == 0

    # Step 2: Complete Twitter action 1; Reddit remains incomplete
    with open(twitter_log, "a", encoding="utf-8") as f_tw:
        f_tw.write(', "timestamp": "2026-01-01T10:00:00"}\n')

    tw_pos = SimulationRunner._read_action_log(str(twitter_log), tw_pos, state, "twitter")
    rd_pos = SimulationRunner._read_action_log(str(reddit_log), rd_pos, state, "reddit")
    assert tw_pos > 0
    assert rd_pos == 0
    assert state.twitter_actions_count == 1
    assert state.reddit_actions_count == 0
    assert len(state.actions) == 1
    assert state.actions[0].platform == "twitter"
    assert state.actions[0].agent_id == 10

    # Step 3: Reddit completes action 1 and writes partial action 2;
    # Twitter writes complete action 2 followed by round_end event
    with open(reddit_log, "a", encoding="utf-8") as f_rd:
        f_rd.write(', "timestamp": "2026-01-01T10:01:00"}\n{"round": 1, "agent_id": 21, "agent_name": "RdAgent2"')
    with open(twitter_log, "a", encoding="utf-8") as f_tw:
        f_tw.write(
            json.dumps({
                "round": 1,
                "agent_id": 11,
                "agent_name": "TwAgent2",
                "action_type": "LIKE",
                "timestamp": "2026-01-01T10:02:00",
            })
            + "\n"
            + json.dumps({
                "event_type": "round_end",
                "round": 1,
                "simulated_hours": 2,
            })
            + "\n"
        )

    rd_pos = SimulationRunner._read_action_log(str(reddit_log), rd_pos, state, "reddit")
    tw_pos = SimulationRunner._read_action_log(str(twitter_log), tw_pos, state, "twitter")

    assert state.reddit_actions_count == 1
    assert state.twitter_actions_count == 2
    assert state.twitter_current_round == 1
    assert state.twitter_simulated_hours == 2
    assert state.current_round == 1
    assert state.simulated_hours == 2

    # Step 4: Reddit finishes action 2 and writes round_end for round 2 with 5 simulated hours
    with open(reddit_log, "a", encoding="utf-8") as f_rd:
        f_rd.write(
            ', "action_type": "COMMENT", "timestamp": "2026-01-01T10:03:00"}\n'
            + json.dumps({
                "event_type": "round_end",
                "round": 2,
                "simulated_hours": 5,
            })
            + "\n"
        )

    rd_pos = SimulationRunner._read_action_log(str(reddit_log), rd_pos, state, "reddit")
    assert state.reddit_actions_count == 2
    assert state.reddit_current_round == 2
    assert state.reddit_simulated_hours == 5
    assert state.current_round == 2
    assert state.simulated_hours == 5

    # Step 5: Twitter completes simulation; runner remains RUNNING because Reddit is not completed
    with open(twitter_log, "a", encoding="utf-8") as f_tw:
        f_tw.write(
            json.dumps({
                "event_type": "simulation_end",
                "total_rounds": 1,
                "total_actions": 2,
            })
            + "\n"
        )

    tw_pos = SimulationRunner._read_action_log(str(twitter_log), tw_pos, state, "twitter")
    assert state.twitter_completed is True
    assert state.reddit_completed is False
    assert state.runner_status == RunnerStatus.RUNNING

    # Step 6: Reddit completes simulation; both platforms completed -> RunnerStatus.COMPLETED
    with open(reddit_log, "a", encoding="utf-8") as f_rd:
        f_rd.write(
            json.dumps({
                "event_type": "simulation_end",
                "total_rounds": 2,
                "total_actions": 2,
            })
            + "\n"
        )

    rd_pos = SimulationRunner._read_action_log(str(reddit_log), rd_pos, state, "reddit")
    assert state.reddit_completed is True
    assert state.runner_status == RunnerStatus.COMPLETED
    assert state.completed_at is not None

    with SimulationRunner._action_cache_lock:
        cached = SimulationRunner._action_cache.get(sim_id, [])
        assert len(cached) == 4
        assert [a.platform for a in cached] == ["twitter", "reddit", "twitter", "reddit"]


def test_corrupt_line_followed_by_valid_line_recovery(tmp_path):
    """
    Test error recovery when corrupt/invalid JSON lines occur:
    1. A complete corrupt line (ending in newline) is skipped via JSONDecodeError handling.
    2. Subsequent valid lines are successfully parsed and recorded.
    3. Trailing partial line halts offset advancement until completed.
    4. Graph updater is called only for valid actions, never for corrupt lines.
    """
    sim_id = "sim_corrupt_recovery_test"
    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.RUNNING)
    log_file = tmp_path / "actions.jsonl"

    mock_updater = MagicMock()
    SimulationRunner._graph_memory_enabled[sim_id] = True

    with patch.object(GraphMemoryManager, "get_updater", return_value=mock_updater):
        line1 = json.dumps({
            "round": 1,
            "agent_id": 1,
            "agent_name": "ValidAgent1",
            "action_type": "POST",
            "timestamp": "2026-01-01T10:00:00",
        }) + "\n"
        corrupt_line1 = '{"corrupt": invalid syntax without closing brace\n'
        corrupt_line2 = 'NOT_JSON_AT_ALL_GARBAGE_CONTENT\n'
        line2 = json.dumps({
            "round": 1,
            "agent_id": 2,
            "agent_name": "ValidAgent2",
            "action_type": "COMMENT",
            "timestamp": "2026-01-01T10:01:00",
        }) + "\n"
        partial_line = '{"round": 2, "agent_id": 3, "agent_name": "ValidAgent3"'

        with open(log_file, "w", encoding="utf-8") as f:
            f.write(line1)
            f.write(corrupt_line1)
            f.write(corrupt_line2)
            f.write(line2)
            expected_pos_after_line2 = f.tell()
            f.write(partial_line)

        # Read 1: Should parse line1 and line2, skip corrupt lines, stop before partial_line
        pos = SimulationRunner._read_action_log(str(log_file), 0, state, "twitter")
        assert pos == expected_pos_after_line2
        assert len(state.actions) == 2
        assert state.actions[0].agent_id == 1
        assert state.actions[1].agent_id == 2
        assert mock_updater.add_activity_from_dict.call_count == 2

        # Complete the partial line
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(', "action_type": "UPVOTE", "timestamp": "2026-01-01T10:02:00"}\n')

        # Read 2: Resumes and parses the newly completed line
        new_pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
        assert new_pos > pos
        assert len(state.actions) == 3
        assert state.actions[2].agent_id == 3
        assert state.actions[2].action_type == "UPVOTE"
        assert mock_updater.add_activity_from_dict.call_count == 3


def test_seeking_empty_file_single_partial_multiple_chunks_over_poll_cycles(tmp_path):
    """
    Test seeking behavior across multiple poll cycles:
    - Empty file (0 bytes): returns offset 0, no actions.
    - Single partial line: returns offset 0 across multiple polls as chunks arrive.
    - Completed line: returns new offset, action parsed.
    - Second action arriving in 3+ partial chunks across poll cycles:
      returns unchanged offset until newline is written, then returns new offset.
    - Non-existent file path: returns provided position safely.
    """
    sim_id = "sim_poll_seeking_test"
    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.RUNNING)
    log_file = tmp_path / "actions.jsonl"
    log_file.touch()

    # Cycle 1: Empty file
    pos = SimulationRunner._read_action_log(str(log_file), 0, state, "twitter")
    assert pos == 0
    assert len(state.actions) == 0

    # Cycle 2: First partial chunk of Action 1
    with open(log_file, "a", encoding="utf-8") as f:
        f.write('{"round": 1, "agent_id": 100')
    pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
    assert pos == 0
    assert len(state.actions) == 0

    # Cycle 3: Second partial chunk of Action 1
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(', "agent_name": "Tester100"')
    pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
    assert pos == 0
    assert len(state.actions) == 0

    # Cycle 4: Third partial chunk of Action 1
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(', "action_type": "TWEET"')
    pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
    assert pos == 0
    assert len(state.actions) == 0

    # Cycle 5: Final chunk completing Action 1 with newline
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(', "timestamp": "2026-01-01T12:00:00"}\n')
    pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
    assert pos > 0
    assert len(state.actions) == 1
    assert state.actions[0].agent_id == 100
    first_action_pos = pos

    # Cycle 6: Action 2 - Chunk 1 (over 3+ poll cycles)
    with open(log_file, "a", encoding="utf-8") as f:
        f.write('{"round": 2, "agent_id": 200')
    pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
    assert pos == first_action_pos
    assert len(state.actions) == 1

    # Cycle 7: Action 2 - Chunk 2
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(', "agent_name": "Tester200"')
    pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
    assert pos == first_action_pos
    assert len(state.actions) == 1

    # Cycle 8: Action 2 - Chunk 3
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(', "action_type": "REPLY"')
    pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
    assert pos == first_action_pos
    assert len(state.actions) == 1

    # Cycle 9: Action 2 - Final chunk with newline
    with open(log_file, "a", encoding="utf-8") as f:
        f.write(', "timestamp": "2026-01-01T12:05:00"}\n')
    pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
    assert pos > first_action_pos
    assert len(state.actions) == 2
    assert state.actions[1].agent_id == 200

    # Non-existent file error recovery test
    non_existent = str(tmp_path / "does_not_exist.jsonl")
    safe_pos = SimulationRunner._read_action_log(non_existent, 42, state, "twitter")
    assert safe_pos == 42

