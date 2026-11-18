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


def test_action_deduplication_stress_10000_items():
    """
    10,000 action deduplication stress test:
    Verify inserting 10,000 items with duplicates takes minimal CPU time (< 1s)
    and bounds deduplicated output.
    """
    state = SimulationRunState(simulation_id="sim_perf_10k", runner_status=RunnerStatus.RUNNING)

    unique_actions = [
        AgentAction(
            round_num=i % 100,
            timestamp=f"2026-01-01T10:{i % 60:02d}:{(i * 7) % 60:02d}",
            platform="twitter" if i % 2 == 0 else "reddit",
            agent_id=i,
            agent_name=f"agent_{i}",
            action_type="POST",
            action_args={"content": f"msg_{i}"},
            action_id=f"act_stress_{i}",
        )
        for i in range(5000)
    ]

    start = time.perf_counter()
    # First pass: all 5,000 unique actions inserted
    first_pass_results = [state.add_action(a) for a in unique_actions]
    # Second pass: duplicate insertion of same 5,000 actions (total 10,000 insertions)
    second_pass_results = [state.add_action(a) for a in unique_actions]
    elapsed = time.perf_counter() - start

    assert elapsed < 1.0, f"Deduplication of 10,000 items took {elapsed:.3f}s (exceeded 1.0s limit)"
    assert all(first_pass_results), "Expected all unique actions to be added successfully in first pass"
    assert not any(second_pass_results), "Expected all duplicate actions to be rejected in second pass"
    assert len(state.actions) == 5000, f"Expected exactly 5000 deduplicated actions, got {len(state.actions)}"
    assert len(state.seen_action_ids) == 5000, f"Expected 5000 seen action IDs, got {len(state.seen_action_ids)}"
    assert len(state.recent_actions) == min(5000, state.max_recent_actions), (
        f"Expected recent_actions bounded to {state.max_recent_actions}, got {len(state.recent_actions)}"
    )


def test_dual_platform_collision_prevention():
    """
    Dual-platform collision prevention:
    Actions with identical timestamp, round, and action type on `twitter` vs `reddit`
    are preserved as distinct actions.
    """
    state = SimulationRunState(simulation_id="sim_platform_collision", runner_status=RunnerStatus.RUNNING)

    tw_action = AgentAction(
        round_num=1,
        timestamp="2026-01-01T12:00:00",
        platform="twitter",
        agent_id=42,
        agent_name="SameAgent",
        action_type="POST",
        action_args={"text": "same payload"},
    )
    rd_action = AgentAction(
        round_num=1,
        timestamp="2026-01-01T12:00:00",
        platform="reddit",
        agent_id=42,
        agent_name="SameAgent",
        action_type="POST",
        action_args={"text": "same payload"},
    )

    key_tw = state._get_action_key(tw_action)
    key_rd = state._get_action_key(rd_action)
    assert key_tw != key_rd, f"Expected distinct keys across platforms, but got {key_tw}"

    added_tw = state.add_action(tw_action)
    added_rd = state.add_action(rd_action)

    assert added_tw is True, "Expected twitter action to be added"
    assert added_rd is True, "Expected reddit action to be added without collision"
    assert state.twitter_actions_count == 1, f"Expected 1 twitter action, got {state.twitter_actions_count}"
    assert state.reddit_actions_count == 1, f"Expected 1 reddit action, got {state.reddit_actions_count}"
    assert len(state.actions) == 2, f"Expected 2 total actions, got {len(state.actions)}"

    # Re-adding either action must be detected as duplicate
    assert state.add_action(tw_action) is False, "Expected re-added twitter action to be rejected as duplicate"
    assert state.add_action(rd_action) is False, "Expected re-added reddit action to be rejected as duplicate"


def test_concurrent_reader_writer_stress_20_writers_10_readers():
    """
    Concurrent reader/writer stress test:
    20 concurrent threads inserting actions while 10 threads call `to_detail_dict()`
    and iterate `recent_actions`, verifying no `RuntimeError` or deadlocks.
    """
    state = SimulationRunState(simulation_id="sim_stress_rw_20_10", max_recent_actions=50)

    errors = []
    stop_event = threading.Event()
    num_writers = 20
    num_readers = 10
    actions_per_writer = 100

    def writer(tid: int):
        for idx in range(actions_per_writer):
            action = AgentAction(
                round_num=idx,
                timestamp=f"2026-01-01T10:00:{idx % 60:02d}",
                platform="twitter" if (tid + idx) % 2 == 0 else "reddit",
                agent_id=tid * 1000 + idx,
                agent_name=f"agent_{tid}",
                action_type="POST",
                action_args={"seq": idx},
                action_id=f"act_{tid}_{idx}",
            )
            try:
                state.add_action(action)
            except (RuntimeError, IndexError, ValueError, KeyError, AssertionError) as e:
                errors.append((f"writer_{tid}", e))

    def reader(rid: int):
        while not stop_event.is_set():
            try:
                detail = state.to_detail_dict()
                recents = detail.get("recent_actions", [])
                for act in recents:
                    _ = act.get("action_type")
                    _ = act.get("platform")
                with state.lock:
                    for direct_act in state.recent_actions:
                        _ = direct_act.action_type
            except (RuntimeError, IndexError, ValueError, KeyError, AssertionError) as e:
                errors.append((f"reader_{rid}", e))

    writer_threads = [threading.Thread(target=writer, args=(i,), name=f"writer-{i}") for i in range(num_writers)]
    reader_threads = [threading.Thread(target=reader, args=(i,), name=f"reader-{i}") for i in range(num_readers)]

    for t in writer_threads + reader_threads:
        t.start()

    # Wait for all writers to complete with a generous deadlock timeout
    for t in writer_threads:
        t.join(timeout=10.0)
        assert not t.is_alive(), f"Writer thread {t.name} did not terminate within timeout (possible deadlock)"

    # Signal readers to stop and join
    stop_event.set()
    for t in reader_threads:
        t.join(timeout=5.0)
        assert not t.is_alive(), f"Reader thread {t.name} did not terminate within timeout (possible deadlock)"

    assert not errors, f"Errors encountered during concurrent stress: {errors}"
    expected_actions = num_writers * actions_per_writer
    assert len(state.actions) == expected_actions, (
        f"Expected {expected_actions} actions, got {len(state.actions)}"
    )
    assert len(state.recent_actions) <= state.max_recent_actions, (
        f"recent_actions length {len(state.recent_actions)} exceeded max {state.max_recent_actions}"
    )


def test_action_id_uniqueness_fallback_deterministic_across_agents():
    """
    Action ID uniqueness fallback:
    Actions without explicit `action_id` generate deterministic composite keys
    without collisions across distinct agents.
    """
    state = SimulationRunState(simulation_id="sim_fallback_keys", runner_status=RunnerStatus.RUNNING)

    act_agent1 = AgentAction(
        round_num=2,
        timestamp="2026-01-01T15:30:00",
        platform="twitter",
        agent_id=1,
        agent_name="AgentOne",
        action_type="REPLY",
        action_args={"reply_to": "123", "text": "agree"},
        action_id=None,
    )
    act_agent2 = AgentAction(
        round_num=2,
        timestamp="2026-01-01T15:30:00",
        platform="twitter",
        agent_id=2,
        agent_name="AgentTwo",
        action_type="REPLY",
        action_args={"reply_to": "123", "text": "agree"},
        action_id=None,
    )

    key1 = state._get_action_key(act_agent1)
    key2 = state._get_action_key(act_agent2)

    # 1. Deterministic composite keys
    assert key1 == state._get_action_key(act_agent1), "Action key generation must be deterministic"
    assert key2 == state._get_action_key(act_agent2), "Action key generation must be deterministic"

    # 2. No collisions across distinct agents
    assert key1 != key2, f"Expected distinct composite keys for distinct agents, got {key1}"

    # 3. Both actions added successfully
    assert state.add_action(act_agent1) is True, "First agent action must be added successfully"
    assert state.add_action(act_agent2) is True, "Second agent action must be added without colliding with first"

    # 4. Duplicate for same agent rejected
    duplicate_act1 = AgentAction(
        round_num=2,
        timestamp="2026-01-01T15:30:00",
        platform="twitter",
        agent_id=1,
        agent_name="AgentOne",
        action_type="REPLY",
        action_args={"reply_to": "123", "text": "agree"},
        action_id=None,
    )
    assert state.add_action(duplicate_act1) is False, "Duplicate action for same agent must be rejected"

    # 5. Same agent with different action_args produces different composite key
    act_agent1_diff_args = AgentAction(
        round_num=2,
        timestamp="2026-01-01T15:30:00",
        platform="twitter",
        agent_id=1,
        agent_name="AgentOne",
        action_type="REPLY",
        action_args={"reply_to": "123", "text": "disagree"},
        action_id=None,
    )
    key3 = state._get_action_key(act_agent1_diff_args)
    assert key3 != key1, "Different action_args for same agent must produce distinct composite keys"
    assert state.add_action(act_agent1_diff_args) is True, "Action with different arguments must be added"
