"""
Tests for Issue #8: Race condition between stop_simulation/start_simulation and monitor thread.

Covers:
1. stop_simulation retains STOPPED status and does not get overwritten with FAILED by the monitor thread.
2. Monitor thread exception handler preserves STOPPED status if stopping/stopped.
3. Process identity check in monitor finally block prevents popping a replacement process,
   action queue, or closing its stdout file.
4. Monitor thread does not overwrite run_state if process_pid has changed.
5. stop_simulation joins existing monitor thread with timeout.
6. start_simulation joins existing monitor thread before starting new process.
7. cleanup_all_simulations joins monitor threads and sets status to STOPPED.
8. Real threading race test: stop and immediate restart does not corrupt process 2.
"""

import json
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.services.simulation_runner import (
    RunnerStatus,
    SimulationRunState,
    SimulationRunner,
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

    yield test_dir

    SimulationRunner._cleanup_done = False
    SimulationRunner._run_states.clear()
    SimulationRunner._processes.clear()
    SimulationRunner._action_queues.clear()
    SimulationRunner._monitor_threads.clear()
    SimulationRunner._stdout_files.clear()
    SimulationRunner._stderr_files.clear()
    SimulationRunner._graph_memory_enabled.clear()


def _create_mock_process(pid: int = 12345, returncode=None):
    """Helper to create a mock subprocess.Popen instance."""
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = returncode
    proc.returncode = returncode
    return proc


def _setup_simulation_dir(sim_dir: str, simulation_id: str):
    """Create simulation config and files needed by runner."""
    s_dir = os.path.join(sim_dir, simulation_id)
    os.makedirs(s_dir, exist_ok=True)
    config = {
        "simulation_id": simulation_id,
        "time_config": {
            "total_simulation_hours": 1,
            "minutes_per_round": 30,
        },
    }
    with open(os.path.join(s_dir, "simulation_config.json"), "w", encoding="utf-8") as f:
        json.dump(config, f)
    return s_dir


# ============================================================================
# 1. stop_simulation retains STOPPED status and is not overwritten with FAILED
# ============================================================================

def test_stop_simulation_retains_stopped_status_not_overwritten_by_monitor(clean_simulation_runner):
    """When a simulation is stopped, monitor thread must preserve STOPPED status even on non-zero exit code."""
    sim_id = "sim_stop_retains_status"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    proc = _create_mock_process(pid=5001, returncode=None)
    SimulationRunner._processes[sim_id] = proc

    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        process_pid=5001,
        started_at="2026-01-01T00:00:00",
    )
    SimulationRunner._save_run_state(state)

    def fake_terminate(process, simulation_id, timeout=10):
        # Simulate SIGTERM termination resulting in non-zero returncode (-15)
        proc.poll.return_value = -15
        proc.returncode = -15

    # Run monitor in a thread
    monitor_thread = threading.Thread(
        target=SimulationRunner._monitor_simulation,
        args=(sim_id,),
    )
    SimulationRunner._monitor_threads[sim_id] = monitor_thread

    with patch.object(SimulationRunner, "_terminate_process", side_effect=fake_terminate):
        monitor_thread.start()
        # Give monitor thread a moment to enter its loop
        time.sleep(0.05)
        res = SimulationRunner.stop_simulation(sim_id)

    assert res.runner_status == RunnerStatus.STOPPED
    assert res.completed_at is not None
    assert "Process exit code" not in (res.error or "")

    # Check state on disk
    saved_state = SimulationRunner.get_run_state(sim_id)
    assert saved_state.runner_status == RunnerStatus.STOPPED
    assert "Process exit code" not in (saved_state.error or "")


def test_monitor_simulation_preserves_stopped_when_already_stopping_or_stopped(clean_simulation_runner):
    """Directly test _monitor_simulation preserving STOPPED status after process exits with error code."""
    sim_id = "sim_monitor_stopping"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    proc = _create_mock_process(pid=5002, returncode=-9)
    SimulationRunner._processes[sim_id] = proc

    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.STOPPING,
        process_pid=5002,
    )
    SimulationRunner._save_run_state(state)

    SimulationRunner._monitor_simulation(sim_id)

    final_state = SimulationRunner.get_run_state(sim_id)
    assert final_state.runner_status == RunnerStatus.STOPPED
    assert final_state.completed_at is not None
    assert final_state.twitter_running is False
    assert final_state.reddit_running is False


def test_monitor_simulation_exception_preserves_stopped_status(clean_simulation_runner):
    """Exception in monitor thread preserves STOPPED status if state is STOPPING or STOPPED."""
    sim_id = "sim_monitor_exception"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    proc = _create_mock_process(pid=5003, returncode=0)
    SimulationRunner._processes[sim_id] = proc

    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.STOPPED,
        process_pid=5003,
    )
    SimulationRunner._save_run_state(state)

    with patch.object(SimulationRunner, "_read_action_log", side_effect=RuntimeError("Disk read failure")):
        # Create dummy log file so _read_action_log is invoked
        tw_log = os.path.join(clean_simulation_runner, sim_id, "twitter", "actions.jsonl")
        os.makedirs(os.path.dirname(tw_log), exist_ok=True)
        with open(tw_log, "w") as f:
            f.write("{}\n")

        SimulationRunner._monitor_simulation(sim_id)

    final_state = SimulationRunner.get_run_state(sim_id)
    assert final_state.runner_status == RunnerStatus.STOPPED


# ============================================================================
# 2. Process identity check in finally block prevents popping replacement
# ============================================================================

def test_process_identity_check_prevents_popping_replacement_resources(clean_simulation_runner):
    """Monitor thread for process 1 must NOT pop process 2 or close process 2's stdout file."""
    sim_id = "sim_identity_check"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    proc1 = _create_mock_process(pid=6001, returncode=0)
    proc2 = _create_mock_process(pid=6002, returncode=None)

    mock_stdout_file2 = MagicMock()
    mock_action_queue2 = MagicMock()
    thread2 = MagicMock()

    # Process 1 was monitored
    SimulationRunner._processes[sim_id] = proc1

    state1 = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        process_pid=6001,
    )
    SimulationRunner._save_run_state(state1)

    # Hook into _save_run_state inside _monitor_simulation so that right before finally,
    # replacement process 2 is registered
    orig_save = SimulationRunner._save_run_state

    def simulate_restart_before_finally(saved_state):
        # When process 1 tries to save COMPLETED or STOPPED, process 2 has replaced it
        SimulationRunner._processes[sim_id] = proc2
        SimulationRunner._stdout_files[sim_id] = mock_stdout_file2
        SimulationRunner._action_queues[sim_id] = mock_action_queue2
        SimulationRunner._monitor_threads[sim_id] = thread2

        state2 = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=6002,
        )
        orig_save(state2)

    with patch.object(SimulationRunner, "_save_run_state", side_effect=simulate_restart_before_finally):
        SimulationRunner._monitor_simulation(sim_id)

    # Process 2 and its resources must still exist!
    assert SimulationRunner._processes.get(sim_id) is proc2
    assert SimulationRunner._stdout_files.get(sim_id) is mock_stdout_file2
    assert SimulationRunner._action_queues.get(sim_id) is mock_action_queue2
    assert SimulationRunner._monitor_threads.get(sim_id) is thread2
    mock_stdout_file2.close.assert_not_called()


def test_monitor_does_not_overwrite_state_if_pid_changed(clean_simulation_runner):
    """If process_pid has changed to a new run, old monitor must not overwrite run_state.json."""
    sim_id = "sim_pid_changed"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    proc1 = _create_mock_process(pid=7001, returncode=1)
    SimulationRunner._processes[sim_id] = proc1

    # Old run state
    state1 = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        process_pid=7001,
    )
    SimulationRunner._save_run_state(state1)

    # Replacement run state already committed by start_simulation
    state2 = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        process_pid=7002,
    )
    SimulationRunner._save_run_state(state2)

    # Run monitor for process 1
    SimulationRunner._monitor_simulation(sim_id)

    # Verify run_state still belongs to process 2 and is RUNNING, not FAILED from process 1
    current_state = SimulationRunner.get_run_state(sim_id)
    assert current_state.process_pid == 7002
    assert current_state.runner_status == RunnerStatus.RUNNING


# ============================================================================
# 3. Thread join synchronization in stop_simulation and start_simulation
# ============================================================================

def test_stop_simulation_joins_monitor_thread(clean_simulation_runner):
    """stop_simulation waits for monitor_thread to finish and cleans up the thread map."""
    sim_id = "sim_stop_joins_thread"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    proc = _create_mock_process(pid=8001, returncode=None)
    SimulationRunner._processes[sim_id] = proc

    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        process_pid=8001,
    )
    SimulationRunner._save_run_state(state)

    mock_thread = MagicMock()
    mock_thread.is_alive.side_effect = [True, False]
    SimulationRunner._monitor_threads[sim_id] = mock_thread

    with patch.object(SimulationRunner, "_terminate_process"):
        SimulationRunner.stop_simulation(sim_id)

    mock_thread.join.assert_called_once_with(timeout=5.0)
    assert sim_id not in SimulationRunner._monitor_threads


def test_start_simulation_joins_existing_monitor_thread(clean_simulation_runner):
    """start_simulation waits for existing monitor_thread to exit before starting."""
    sim_id = "sim_start_joins_thread"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    # Previous process was stopped but monitor thread is still alive
    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.STOPPED,
        process_pid=8002,
    )
    SimulationRunner._save_run_state(state)

    mock_thread = MagicMock()
    mock_thread.is_alive.side_effect = [True, False]
    SimulationRunner._monitor_threads[sim_id] = mock_thread

    new_proc = _create_mock_process(pid=8003, returncode=None)

    with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
         patch("subprocess.Popen", return_value=new_proc), \
         patch("threading.Thread"):
        SimulationRunner.start_simulation(sim_id, platform="twitter")

    mock_thread.join.assert_called_with(timeout=5.0)


# ============================================================================
# 4. cleanup_all_simulations thread handling
# ============================================================================

def test_cleanup_all_simulations_joins_threads_and_sets_stopped(clean_simulation_runner):
    """cleanup_all_simulations terminates processes, joins threads, and marks states STOPPED."""
    sim_id = "sim_cleanup_all"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    proc = _create_mock_process(pid=9001, returncode=None)
    SimulationRunner._processes[sim_id] = proc

    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        process_pid=9001,
    )
    SimulationRunner._save_run_state(state)
    mock_thread = MagicMock()
    mock_thread.is_alive.return_value = True
    def fake_join(timeout=None):
        mock_thread.is_alive.return_value = False
    mock_thread.join.side_effect = fake_join
    SimulationRunner._monitor_threads[sim_id] = mock_thread

    with patch.object(SimulationRunner, "_terminate_process"):
        SimulationRunner.cleanup_all_simulations()

    mock_thread.join.assert_called_once_with(timeout=5.0)
    assert len(SimulationRunner._monitor_threads) == 0

    saved_state = SimulationRunner.get_run_state(sim_id)
    assert saved_state.runner_status == RunnerStatus.STOPPED


# ============================================================================
# 5. Full concurrent stop & restart race condition integration
# ============================================================================

def test_concurrent_stop_and_restart_race(clean_simulation_runner):
    """
    End-to-end race condition test:
    Simulates Process 1 being stopped while its monitor thread is active.
    Immediately starts Process 2.
    Ensures Process 2 is not orphaned, its log file is not closed, and state remains RUNNING.
    """
    sim_id = "sim_concurrent_race"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    proc1 = _create_mock_process(pid=9501, returncode=None)
    proc2 = _create_mock_process(pid=9502, returncode=None)

    SimulationRunner._processes[sim_id] = proc1
    state1 = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        process_pid=9501,
    )
    SimulationRunner._save_run_state(state1)

    # Monitor thread 1 running
    thread1_done = threading.Event()

    def run_monitor_1():
        # Will loop until proc1.poll() returns non-zero
        SimulationRunner._monitor_simulation(sim_id)
        thread1_done.set()

    t1 = threading.Thread(target=run_monitor_1)
    SimulationRunner._monitor_threads[sim_id] = t1
    t1.start()

    time.sleep(0.05)

    def terminate_proc1(p, sid, timeout=10):
        proc1.poll.return_value = -15
        proc1.returncode = -15

    with patch.object(SimulationRunner, "_terminate_process", side_effect=terminate_proc1):
        # Stop simulation 1
        SimulationRunner.stop_simulation(sim_id)

    assert thread1_done.is_set()

    # Now start simulation 2
    with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
         patch("subprocess.Popen", return_value=proc2), \
         patch("threading.Thread") as mock_thread_cls:
        mock_t2 = MagicMock()
        mock_thread_cls.return_value = mock_t2
        state2 = SimulationRunner.start_simulation(sim_id, platform="twitter")

    assert state2.runner_status == RunnerStatus.RUNNING
    assert state2.process_pid == 9502
    assert SimulationRunner._processes[sim_id] is proc2
    assert SimulationRunner.get_run_state(sim_id).runner_status == RunnerStatus.RUNNING
    assert SimulationRunner.get_run_state(sim_id).process_pid == 9502
