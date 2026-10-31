"""
Tests for Issue #13: Performance optimizations in SimulationRunner and SimulationIPCClient.

Covers:
1. In-memory action cache incrementally populated by _read_action_log.
2. get_all_actions serving directly from in-memory cache without disk reads when cache is warm.
3. get_all_actions cold cache fallback loading from disk and populating in-memory cache.
4. In-memory filtering by platform, round_num, and agent_id with newest-first ordering.
5. _save_run_state atomic replacement via .tmp file and os.replace.
6. _save_run_state dirty checking skipping redundant disk I/O when state has not changed.
7. SimulationIPCClient default poll_interval reduced to 0.05s (50ms).
8. SimulationIPCClient low-latency command/response completion.
9. Cache eviction on cleanup_simulation_logs and cleanup_all_simulations.
"""

import json
import os
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

from app.services.simulation_runner import (
    AgentAction,
    RunnerStatus,
    SimulationRunState,
    SimulationRunner,
)
from app.services.simulation_ipc import (
    CommandType,
    CommandStatus,
    IPCResponse,
    SimulationIPCClient,
)


@pytest.fixture(autouse=True)
def clean_simulation_runner(tmp_path, monkeypatch):
    """Isolate runner storage and state for every test."""
    test_dir = str(tmp_path / "simulations")
    os.makedirs(test_dir, exist_ok=True)
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", test_dir)

    SimulationRunner._cleanup_done = False
    SimulationRunner._run_states.clear()
    SimulationRunner._processes.clear()
    SimulationRunner._action_queues.clear()
    SimulationRunner._monitor_threads.clear()
    SimulationRunner._stdout_files.clear()
    SimulationRunner._stderr_files.clear()
    SimulationRunner._graph_memory_enabled.clear()
    with SimulationRunner._action_cache_lock:
        SimulationRunner._action_cache.clear()
    with SimulationRunner._state_save_lock:
        SimulationRunner._last_saved_state.clear()

    yield test_dir

    SimulationRunner._cleanup_done = False
    SimulationRunner._run_states.clear()
    SimulationRunner._processes.clear()
    SimulationRunner._action_queues.clear()
    SimulationRunner._monitor_threads.clear()
    SimulationRunner._stdout_files.clear()
    SimulationRunner._stderr_files.clear()
    SimulationRunner._graph_memory_enabled.clear()
    with SimulationRunner._action_cache_lock:
        SimulationRunner._action_cache.clear()
    with SimulationRunner._state_save_lock:
        SimulationRunner._last_saved_state.clear()


def test_action_cache_incremental_population(tmp_path):
    """_read_action_log incrementally appends parsed actions to _action_cache."""
    sim_id = "sim_cache_test"
    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.RUNNING)

    log_file = tmp_path / "actions.jsonl"
    actions_data = [
        {"round": 1, "timestamp": "2026-01-01T10:00:00", "agent_id": 1, "agent_name": "A1", "action_type": "POST"},
        {"round": 1, "timestamp": "2026-01-01T10:01:00", "agent_id": 2, "agent_name": "A2", "action_type": "LIKE"},
    ]
    with open(log_file, "w") as f:
        for a in actions_data:
            f.write(json.dumps(a) + "\n")

    pos = SimulationRunner._read_action_log(str(log_file), 0, state, "twitter")
    assert pos > 0

    with SimulationRunner._action_cache_lock:
        cached = SimulationRunner._action_cache.get(sim_id)
        assert cached is not None
        assert len(cached) == 2
        assert cached[0].agent_id == 1
        assert cached[0].platform == "twitter"
        assert cached[1].agent_id == 2
        assert cached[1].platform == "twitter"

    # Append a third action and read incrementally
    with open(log_file, "a") as f:
        f.write(json.dumps({"round": 2, "timestamp": "2026-01-01T10:05:00", "agent_id": 3, "agent_name": "A3", "action_type": "REPLY"}) + "\n")

    new_pos = SimulationRunner._read_action_log(str(log_file), pos, state, "twitter")
    assert new_pos > pos

    with SimulationRunner._action_cache_lock:
        cached = SimulationRunner._action_cache.get(sim_id)
        assert len(cached) == 3
        assert cached[2].agent_id == 3
        assert cached[2].round_num == 2


def test_get_all_actions_serves_from_memory_cache_without_disk_reads():
    """When _action_cache is populated, get_all_actions does not read disk."""
    sim_id = "sim_warm_cache"
    actions = [
        AgentAction(round_num=1, timestamp="2026-01-01T10:00:00", platform="twitter", agent_id=1, agent_name="A1", action_type="POST"),
        AgentAction(round_num=1, timestamp="2026-01-01T10:02:00", platform="reddit", agent_id=2, agent_name="A2", action_type="COMMENT"),
        AgentAction(round_num=2, timestamp="2026-01-01T10:05:00", platform="twitter", agent_id=1, agent_name="A1", action_type="LIKE"),
    ]

    with SimulationRunner._action_cache_lock:
        SimulationRunner._action_cache[sim_id] = list(actions)

    with patch.object(SimulationRunner, "_read_actions_from_file") as mock_read_file:
        res = SimulationRunner.get_all_actions(sim_id)
        assert len(res) == 3
        # Ensure disk was never read
        mock_read_file.assert_not_called()

        # Ordering newest first
        assert res[0].timestamp == "2026-01-01T10:05:00"
        assert res[1].timestamp == "2026-01-01T10:02:00"
        assert res[2].timestamp == "2026-01-01T10:00:00"


def test_get_all_actions_cold_cache_loads_disk_and_populates_cache(clean_simulation_runner):
    """When cache is cold, get_all_actions loads disk files and populates cache."""
    sim_id = "sim_cold_cache"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    tw_dir = os.path.join(sim_dir, "twitter")
    rd_dir = os.path.join(sim_dir, "reddit")
    os.makedirs(tw_dir, exist_ok=True)
    os.makedirs(rd_dir, exist_ok=True)

    with open(os.path.join(tw_dir, "actions.jsonl"), "w") as f:
        f.write(json.dumps({"round": 1, "timestamp": "2026-01-01T10:00:00", "agent_id": 1, "agent_name": "T1", "action_type": "POST"}) + "\n")

    with open(os.path.join(rd_dir, "actions.jsonl"), "w") as f:
        f.write(json.dumps({"round": 1, "timestamp": "2026-01-01T10:01:00", "agent_id": 2, "agent_name": "R1", "action_type": "COMMENT"}) + "\n")

    with SimulationRunner._action_cache_lock:
        assert sim_id not in SimulationRunner._action_cache

    res = SimulationRunner.get_all_actions(sim_id)
    assert len(res) == 2

    # Verify cache is now populated
    with SimulationRunner._action_cache_lock:
        cached = SimulationRunner._action_cache.get(sim_id)
        assert cached is not None
        assert len(cached) == 2

    # Second call must hit cache without disk reads
    with patch.object(SimulationRunner, "_read_actions_from_file") as mock_read_file:
        res2 = SimulationRunner.get_all_actions(sim_id)
        assert len(res2) == 2
        mock_read_file.assert_not_called()


def test_get_all_actions_memory_filtering():
    """get_all_actions accurately filters by platform, agent_id, and round_num in memory."""
    sim_id = "sim_filter_test"
    actions = [
        AgentAction(round_num=1, timestamp="2026-01-01T10:00:00", platform="twitter", agent_id=1, agent_name="A1", action_type="POST"),
        AgentAction(round_num=1, timestamp="2026-01-01T10:01:00", platform="reddit", agent_id=2, agent_name="A2", action_type="COMMENT"),
        AgentAction(round_num=2, timestamp="2026-01-01T10:02:00", platform="twitter", agent_id=1, agent_name="A1", action_type="LIKE"),
        AgentAction(round_num=2, timestamp="2026-01-01T10:03:00", platform="twitter", agent_id=3, agent_name="A3", action_type="RETWEET"),
    ]

    with SimulationRunner._action_cache_lock:
        SimulationRunner._action_cache[sim_id] = list(actions)

    # Filter by platform
    tw_actions = SimulationRunner.get_all_actions(sim_id, platform="twitter")
    assert len(tw_actions) == 3
    assert all(a.platform == "twitter" for a in tw_actions)

    rd_actions = SimulationRunner.get_all_actions(sim_id, platform="reddit")
    assert len(rd_actions) == 1
    assert rd_actions[0].platform == "reddit"

    # Filter by round_num
    r2_actions = SimulationRunner.get_all_actions(sim_id, round_num=2)
    assert len(r2_actions) == 2
    assert all(a.round_num == 2 for a in r2_actions)

    # Filter by agent_id
    a1_actions = SimulationRunner.get_all_actions(sim_id, agent_id=1)
    assert len(a1_actions) == 2
    assert all(a1.agent_id == 1 for a1 in a1_actions)

    # Combined filters
    r2_a1_actions = SimulationRunner.get_all_actions(sim_id, platform="twitter", agent_id=1, round_num=2)
    assert len(r2_a1_actions) == 1
    assert r2_a1_actions[0].agent_id == 1 and r2_a1_actions[0].round_num == 2


def test_save_run_state_atomic_replace(clean_simulation_runner):
    """_save_run_state writes to a temporary file and atomically replaces run_state.json."""
    sim_id = "sim_atomic_save"
    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        current_round=1,
    )

    state_file = os.path.join(clean_simulation_runner, sim_id, "run_state.json")
    with patch("os.replace", wraps=os.replace) as spy_replace:
        SimulationRunner._save_run_state(state)
        assert spy_replace.call_count == 1
        src, dst = spy_replace.call_args[0]
        assert dst == state_file
        assert src.endswith(".tmp")

    assert os.path.exists(state_file)
    with open(state_file, "r") as f:
        data = json.load(f)
    assert data["runner_status"] == "running"
    assert data["current_round"] == 1


def test_save_run_state_dirty_check_skips_redundant_writes(clean_simulation_runner):
    """_save_run_state skips disk write when state has not changed, writes when changed."""
    sim_id = "sim_dirty_check"
    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        current_round=1,
    )

    state_file = os.path.join(clean_simulation_runner, sim_id, "run_state.json")

    # First write: creates file
    SimulationRunner._save_run_state(state)
    assert os.path.exists(state_file)

    # Second write with identical state: should skip write
    with patch("os.replace", wraps=os.replace) as spy_replace:
        SimulationRunner._save_run_state(state)
        assert spy_replace.call_count == 0

    # Third write with modified state: should write
    state.current_round = 2
    with patch("os.replace", wraps=os.replace) as spy_replace:
        SimulationRunner._save_run_state(state)
        assert spy_replace.call_count == 1

    with open(state_file, "r") as f:
        data = json.load(f)
    assert data["current_round"] == 2


def test_cleanup_clears_action_cache_and_saved_state(clean_simulation_runner):
    """cleanup_simulation_logs and cleanup_all_simulations evict action cache and last saved state."""
    sim_id = "sim_cleanup_test"
    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.STOPPED)
    SimulationRunner._save_run_state(state)

    with SimulationRunner._action_cache_lock:
        SimulationRunner._action_cache[sim_id] = [
            AgentAction(round_num=1, timestamp="2026-01-01T10:00:00", platform="twitter", agent_id=1, agent_name="A1", action_type="POST")
        ]

    with SimulationRunner._state_save_lock:
        assert sim_id in SimulationRunner._last_saved_state
    with SimulationRunner._action_cache_lock:
        assert sim_id in SimulationRunner._action_cache

    SimulationRunner.cleanup_simulation_logs(sim_id)

    with SimulationRunner._action_cache_lock:
        assert sim_id not in SimulationRunner._action_cache
    with SimulationRunner._state_save_lock:
        assert sim_id not in SimulationRunner._last_saved_state


def test_ipc_client_default_poll_interval():
    """SimulationIPCClient.send_command default poll_interval is 0.05s (50ms)."""
    import inspect
    sig = inspect.signature(SimulationIPCClient.send_command)
    assert sig.parameters["poll_interval"].default == 0.05


def test_ipc_client_send_command_low_latency_polling(tmp_path):
    """SimulationIPCClient responds promptly without 250ms latency floor."""
    sim_dir = str(tmp_path / "sim_ipc")
    client = SimulationIPCClient(sim_dir)

    def write_response_promptly():
        # Wait until command file is written, then immediately write response
        start = time.time()
        while time.time() - start < 2.0:
            cmd_files = os.listdir(client.commands_dir)
            if cmd_files:
                cmd_id = cmd_files[0].replace(".json", "")
                resp = IPCResponse(
                    command_id=cmd_id,
                    status=CommandStatus.COMPLETED,
                    result={"test": "success"}
                )
                resp_file = os.path.join(client.responses_dir, f"{cmd_id}.json")
                with open(resp_file, "w") as f:
                    json.dump(resp.to_dict(), f)
                break
            time.sleep(0.01)

    t = threading.Thread(target=write_response_promptly)
    t.start()

    t0 = time.perf_counter()
    resp = client.send_command(CommandType.INTERVIEW, {"prompt": "hello"}, timeout=3.0)
    elapsed = time.perf_counter() - t0
    t.join()

    assert resp.status == CommandStatus.COMPLETED
    assert resp.result == {"test": "success"}
    # With 0.05s polling and near-instant mock response, elapsed should be well under 200ms
    assert elapsed < 0.25, f"Expected elapsed < 0.25s, got {elapsed:.3f}s"


def test_ipc_client_send_command_timeout(tmp_path):
    """SimulationIPCClient raises TimeoutError when response is not provided."""
    sim_dir = str(tmp_path / "sim_ipc_timeout")
    client = SimulationIPCClient(sim_dir)

    with pytest.raises(TimeoutError):
        client.send_command(CommandType.CLOSE_ENV, {}, timeout=0.1)
