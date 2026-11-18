"""
Regression tests for Issue #102:
Optimize action log deduplication complexity and synchronize concurrent action reads.

Covers:
1. `_read_action_log()` checks action deduplication with `if action not in state.actions:`
   on a plain list of actions, scaling quadratically O(N^2) as log lines increase instead of
   using a bounded hash set/deque of action IDs for O(1) lookups.
2. `_read_action_log()` fails to deduplicate repeated action log lines because `add_action`
   appends unconditionally prior to the deduplication check.
3. Concurrent iteration and mutation of `state.recent_actions` during SSE telemetry generation
   or status polls causes `RuntimeError: list changed size during iteration` or race conditions
   because `recent_actions` is modified in-place without a dedicated mutex lock or atomic shallow snapshot.
"""

import json
import os
import threading
import time

import pytest

from app.services.simulation_runner import (
    AgentAction,
    RunnerStatus,
    SimulationRunner,
    SimulationRunState,
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


def test_read_action_log_deduplicates_duplicate_lines(tmp_path):
    """
    Test that duplicate action log lines in actions.jsonl are deduplicated.
    Currently, _read_action_log calls state.add_action unconditionally before the check,
    resulting in duplicate entries in state.actions and state.recent_actions.
    """
    sim_id = "sim_dedup_test"
    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.RUNNING)
    log_file = tmp_path / "actions.jsonl"

    action_entry = {
        "round": 1,
        "timestamp": "2026-01-01T10:00:00",
        "agent_id": 42,
        "agent_name": "AgentX",
        "action_type": "POST",
        "action_args": {"text": "hello world"},
        "success": True,
    }
    line = json.dumps(action_entry) + "\n"

    # Write the identical action line twice
    with open(log_file, "w", encoding="utf-8") as f:
        f.writelines([line, line])

    SimulationRunner._read_action_log(str(log_file), 0, state, "twitter")

    assert len(state.actions) == 1, (
        f"Expected deduplication to keep exactly 1 action, but found {len(state.actions)} in state.actions"
    )
    assert len(state.recent_actions) == 1, (
        f"Expected deduplication to keep exactly 1 recent action, but found {len(state.recent_actions)}"
    )


def test_read_action_log_uses_bounded_action_ids_set_not_linear_scan(tmp_path):
    """
    Test that action deduplication does not perform O(N^2) linear scans over state.actions,
    and instead uses a bounded hash set/deque of action IDs for O(1) lookups.
    """
    sim_id = "sim_action_ids_set_test"
    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.RUNNING)

    # 1. State must maintain a bounded hash set / collection of action IDs
    has_action_id_tracker = (
        hasattr(state, "_action_ids")
        or hasattr(state, "seen_action_ids")
        or hasattr(state, "action_ids")
        or hasattr(state, "_seen_action_ids")
    )
    assert has_action_id_tracker, (
        "SimulationRunState must maintain a bounded hash set/deque of action IDs for O(1) deduplication lookups"
    )

    # 2. _read_action_log must not perform linear membership scans against state.actions
    class MonitoredActionList(list):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.linear_contains_count = 0

        def __contains__(self, item):
            self.linear_contains_count += 1
            return super().__contains__(item)

    monitored_actions = MonitoredActionList()
    state.actions = monitored_actions

    log_file = tmp_path / "actions_perf.jsonl"
    lines = [
        json.dumps({
            "round": 1,
            "timestamp": f"2026-01-01T10:{i:02d}:00",
            "agent_id": i,
            "agent_name": f"Agent_{i}",
            "action_type": "POST",
            "action_args": {"text": f"action {i}"},
            "success": True,
        })
        + "\n"
        for i in range(20)
    ]
    with open(log_file, "w", encoding="utf-8") as f:
        f.writelines(lines)

    SimulationRunner._read_action_log(str(log_file), 0, state, "twitter")

    assert monitored_actions.linear_contains_count == 0, (
        f"_read_action_log() performed {monitored_actions.linear_contains_count} linear membership scans "
        "on state.actions list (O(N^2) complexity) instead of O(1) lookup on action IDs set"
    )


def test_simulation_run_state_has_dedicated_mutex_lock():
    """
    Test that SimulationRunState provides a dedicated mutex lock to synchronize
    concurrent action additions and reads.
    """
    state = SimulationRunState(simulation_id="sim_lock_test")
    has_lock = (
        hasattr(state, "_lock")
        or hasattr(state, "lock")
        or hasattr(state, "_actions_lock")
    )
    assert has_lock, (
        "SimulationRunState must have a dedicated mutex lock attribute (_lock or lock) "
        "to synchronize concurrent mutations and reads of recent_actions"
    )


def test_concurrent_recent_actions_mutation_and_reads_thread_safe():
    """
    Test that concurrent mutations (add_action) and reads (to_detail_dict / recent_actions)
    do not suffer from race conditions or capacity invariant violations.
    Without synchronization, writers insert in-place before slicing, causing readers to
    observe len(recent_actions) > max_recent_actions or torn/duplicate reads.
    """
    state = SimulationRunState(simulation_id="sim_concurrency_test", max_recent_actions=50)

    capacity_violations = []
    errors = []
    stop_event = threading.Event()

    def writer(thread_id):
        idx = 0
        while not stop_event.is_set():
            action = AgentAction(
                round_num=idx,
                timestamp=f"t_{thread_id}_{idx}",
                platform="twitter" if thread_id % 2 == 0 else "reddit",
                agent_id=thread_id * 1000 + idx,
                agent_name=f"agent_{thread_id}",
                action_type="POST",
                action_args={"seq": idx},
            )
            try:
                state.add_action(action)
            except (RuntimeError, IndexError, ValueError) as e:
                errors.append(e)
            idx += 1

    def reader():
        while not stop_event.is_set():
            try:
                detail = state.to_detail_dict()
                recents = detail.get("recent_actions", [])
                if len(recents) > state.max_recent_actions:
                    capacity_violations.append(
                        f"Observed recent_actions length {len(recents)} > max {state.max_recent_actions}"
                    )
            except (RuntimeError, IndexError, ValueError) as e:
                errors.append(e)

    writers = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    readers = [threading.Thread(target=reader) for _ in range(4)]

    for t in writers + readers:
        t.start()

    time.sleep(0.4)
    stop_event.set()

    for t in writers + readers:
        t.join()

    assert not errors, f"Encountered concurrency errors: {errors}"
    assert not capacity_violations, (
        f"Concurrent iteration/mutation race condition detected: {capacity_violations[:5]}"
    )
