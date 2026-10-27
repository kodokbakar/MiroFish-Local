"""
Tests for Issue #4: Unkillable zombie subprocesses and IPC hijacking prevention.

Covers:
1. is_process_alive() and _is_pid_alive() checks (defunct zombie detection, PID fallback)
2. _terminate_pid() cross-platform termination
3. stop_simulation() terminating process when runner_status is COMPLETED
4. stop_simulation() using process_pid fallback when cls._processes is empty
5. stop_simulation() raising ValueError when process is dead and status not RUNNING/PAUSED
6. start_simulation() cleaning up lingering process from previous COMPLETED run
7. /api/simulation/start with force=true stopping COMPLETED alive process before cleanup and restart
8. /api/simulation/start without force rejecting restart with 400 when process is alive
"""

import json
import os
import signal
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.services.simulation_manager import (
    SimulationManager,
    SimulationState,
    SimulationStatus,
)
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
    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", test_dir)

    SimulationRunner._run_states.clear()
    SimulationRunner._processes.clear()
    SimulationRunner._action_queues.clear()
    SimulationRunner._monitor_threads.clear()
    SimulationRunner._stdout_files.clear()
    SimulationRunner._stderr_files.clear()
    SimulationRunner._graph_memory_enabled.clear()

    yield test_dir

    SimulationRunner._run_states.clear()
    SimulationRunner._processes.clear()
    SimulationRunner._action_queues.clear()
    SimulationRunner._monitor_threads.clear()
    SimulationRunner._stdout_files.clear()
    SimulationRunner._stderr_files.clear()
    SimulationRunner._graph_memory_enabled.clear()


@pytest.fixture
def app_client():
    """Create Flask test client."""
    app = create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def _create_mock_process(pid: int = 12345, returncode=None):
    """Helper to create a mock subprocess.Popen instance."""
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = returncode
    return proc


def _setup_simulation_dir(sim_dir: str, simulation_id: str):
    """Create simulation config and files needed by start_simulation."""
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
# 1. Process Liveness & PID Checks
# ============================================================================

def test_is_pid_alive_edge_cases():
    """_is_pid_alive handles None, invalid numbers, and non-existent PIDs."""
    assert SimulationRunner._is_pid_alive(None) is False
    assert SimulationRunner._is_pid_alive(0) is False
    assert SimulationRunner._is_pid_alive(-1) is False
    assert SimulationRunner._is_pid_alive("invalid") is False

    # Current process PID is alive
    assert SimulationRunner._is_pid_alive(os.getpid()) is True

    # High non-existent PID should be dead
    assert SimulationRunner._is_pid_alive(99999999) is False


def test_is_pid_alive_detects_defunct_zombie(monkeypatch):
    """_is_pid_alive returns False when /proc/{pid}/status marks process as zombie."""
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)  # os.kill succeeds
    monkeypatch.setattr(os.path, "exists", lambda p: True)

    zombie_status = "Name:\tpython\nState:\tZ (zombie)\nTgid:\t1234\n"
    with patch("builtins.open", pytest.mock_open(read_data=zombie_status) if hasattr(pytest, "mock_open") else MagicMock()):
        with patch("builtins.open", create=True) as mock_file:
            mock_file.return_value.__enter__.return_value = zombie_status.splitlines(keepends=True)
            assert SimulationRunner._is_pid_alive(1234) is False


def test_is_process_alive_in_memory_and_fallback():
    """is_process_alive checks in-memory process first, then falls back to state.process_pid."""
    sim_id = "sim_alive_test"

    # 1. In memory and alive (poll() is None)
    proc = _create_mock_process(pid=1001, returncode=None)
    SimulationRunner._processes[sim_id] = proc
    assert SimulationRunner.is_process_alive(sim_id) is True

    # 2. In memory and dead (poll() is 0)
    proc.poll.return_value = 0
    assert SimulationRunner.is_process_alive(sim_id) is False

    # 3. Not in memory, but state.process_pid is alive
    SimulationRunner._processes.pop(sim_id, None)
    state = SimulationRunState(simulation_id=sim_id, process_pid=2002)
    with patch.object(SimulationRunner, "_is_pid_alive", return_value=True) as mock_pid_alive:
        assert SimulationRunner.is_process_alive(sim_id, state) is True
        mock_pid_alive.assert_called_once_with(2002)

    # 4. Not in memory, state.process_pid is dead
    with patch.object(SimulationRunner, "_is_pid_alive", return_value=False):
        assert SimulationRunner.is_process_alive(sim_id, state) is False

    # 5. Not in memory and no process_pid
    state.process_pid = None
    assert SimulationRunner.is_process_alive(sim_id, state) is False


# ============================================================================
# 2. PID Termination
# ============================================================================

def test_terminate_pid_unix():
    """_terminate_pid sends SIGTERM to process group, waits, and falls back to SIGKILL if alive."""
    pid = 4321
    sim_id = "sim_term_pid"

    calls = []

    def mock_killpg(pgid, sig):
        calls.append((pgid, sig))

    alive_counts = [True, False]

    def mock_is_alive(p):
        return alive_counts.pop(0) if alive_counts else False

    with patch("os.getpgid", return_value=pid), \
         patch("os.killpg", side_effect=mock_killpg), \
         patch.object(SimulationRunner, "_is_pid_alive", side_effect=mock_is_alive):
        SimulationRunner._terminate_pid(pid, sim_id, timeout=1)

    assert (pid, signal.SIGTERM) in calls


# ============================================================================
# 3. stop_simulation Zombie Prevention
# ============================================================================

def test_stop_simulation_terminates_process_when_runner_status_is_completed():
    """stop_simulation allows stopping and terminates process when runner_status is COMPLETED."""
    sim_id = "sim_completed_alive"
    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.COMPLETED,
        process_pid=5555,
    )
    SimulationRunner._run_states[sim_id] = state

    proc = _create_mock_process(pid=5555, returncode=None)
    SimulationRunner._processes[sim_id] = proc

    with patch.object(SimulationRunner, "_terminate_process") as mock_term:
        res = SimulationRunner.stop_simulation(sim_id)

        assert mock_term.called
        assert res.runner_status == RunnerStatus.STOPPED
        assert sim_id not in SimulationRunner._processes


def test_stop_simulation_uses_process_pid_fallback_when_processes_empty():
    """stop_simulation uses process_pid fallback when cls._processes is empty."""
    sim_id = "sim_pid_fallback"
    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.COMPLETED,
        process_pid=7777,
    )
    SimulationRunner._run_states[sim_id] = state
    assert sim_id not in SimulationRunner._processes

    with patch.object(SimulationRunner, "_is_pid_alive", return_value=True), \
         patch.object(SimulationRunner, "_terminate_pid") as mock_term_pid:
        res = SimulationRunner.stop_simulation(sim_id)

        mock_term_pid.assert_called_once_with(7777, sim_id)
        assert res.runner_status == RunnerStatus.STOPPED


def test_stop_simulation_raises_value_error_when_process_is_dead_and_status_not_running_paused():
    """stop_simulation raises ValueError when process is dead and status is COMPLETED or STOPPED."""
    sim_id = "sim_dead"
    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.COMPLETED,
        process_pid=8888,
    )
    SimulationRunner._run_states[sim_id] = state

    with patch.object(SimulationRunner, "is_process_alive", return_value=False):
        with pytest.raises(ValueError, match=f"Simulation not running: {sim_id}, status={RunnerStatus.COMPLETED}"):
            SimulationRunner.stop_simulation(sim_id)


# ============================================================================
# 4. start_simulation Lingering Process Cleanup
# ============================================================================

def test_start_simulation_cleans_up_lingering_process_from_previous_completed_run(clean_simulation_runner):
    """start_simulation terminates lingering process from previous COMPLETED run before starting."""
    sim_id = "sim_restart_lingering"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    # Previous run state was COMPLETED with lingering alive process
    old_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.COMPLETED,
        process_pid=9191,
    )
    SimulationRunner._save_run_state(old_state)

    old_proc = _create_mock_process(pid=9191, returncode=None)
    SimulationRunner._processes[sim_id] = old_proc

    new_proc = _create_mock_process(pid=9292, returncode=None)

    stopped_calls = []

    def mock_stop_simulation(sid):
        stopped_calls.append(sid)
        old_proc.poll.return_value = 0
        SimulationRunner._processes.pop(sid, None)
        old_state.runner_status = RunnerStatus.STOPPED
        return old_state

    with patch.object(SimulationRunner, "stop_simulation", side_effect=mock_stop_simulation), \
         patch("subprocess.Popen", return_value=new_proc), \
         patch("threading.Thread"):
        res = SimulationRunner.start_simulation(sim_id, platform="twitter")

        assert sim_id in stopped_calls
        assert res.runner_status == RunnerStatus.RUNNING
        assert res.process_pid == 9292
        assert SimulationRunner._processes[sim_id] == new_proc


# ============================================================================
# 5. /api/simulation/start Endpoint Zombie Prevention
# ============================================================================

def test_api_start_with_force_true_stops_completed_alive_process_before_cleanup_and_restart(app_client, clean_simulation_runner):
    """/api/simulation/start with force=true stops COMPLETED alive process before cleanup and restarting."""
    sim_id = "sim_api_force"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    # Manager state
    manager = SimulationManager()
    mgr_state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_test",
        graph_id="graph_test",
        status=SimulationStatus.COMPLETED,
    )
    manager._save_simulation_state(mgr_state)

    # Runner state (COMPLETED)
    run_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.COMPLETED,
        process_pid=3333,
    )
    SimulationRunner._save_run_state(run_state)

    call_order = []

    def mock_stop(sid):
        call_order.append("stop_simulation")
        return run_state

    def mock_cleanup(sid):
        call_order.append("cleanup_simulation_logs")
        return {"success": True, "cleaned_files": []}

    def mock_start(**kwargs):
        call_order.append("start_simulation")
        return run_state

    with patch.object(SimulationRunner, "is_process_alive", return_value=True), \
         patch.object(SimulationRunner, "stop_simulation", side_effect=mock_stop), \
         patch.object(SimulationRunner, "cleanup_simulation_logs", side_effect=mock_cleanup), \
         patch.object(SimulationRunner, "start_simulation", side_effect=mock_start), \
         patch("app.api.simulation._check_simulation_prepared", return_value=(True, {})):

        resp = app_client.post("/api/simulation/start", json={
            "simulation_id": sim_id,
            "force": True,
        })

        assert resp.status_code == 200
        assert resp.json["success"] is True
        assert resp.json["data"]["force_restarted"] is True
        assert call_order == ["stop_simulation", "cleanup_simulation_logs", "start_simulation"]


def test_api_start_without_force_rejects_restart_with_400_when_process_is_alive(app_client, clean_simulation_runner):
    """/api/simulation/start without force rejects restart with 400 when process is still alive."""
    sim_id = "sim_api_reject"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    # Manager state
    manager = SimulationManager()
    mgr_state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_test",
        graph_id="graph_test",
        status=SimulationStatus.COMPLETED,
    )
    manager._save_simulation_state(mgr_state)

    # Runner state is COMPLETED but process is still alive
    run_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.COMPLETED,
        process_pid=4444,
    )
    SimulationRunner._save_run_state(run_state)

    with patch.object(SimulationRunner, "is_process_alive", return_value=True), \
         patch.object(SimulationRunner, "stop_simulation") as mock_stop, \
         patch.object(SimulationRunner, "cleanup_simulation_logs") as mock_cleanup, \
         patch.object(SimulationRunner, "start_simulation") as mock_start:

        resp = app_client.post("/api/simulation/start", json={
            "simulation_id": sim_id,
            "force": False,
        })

        assert resp.status_code == 400
        assert resp.json["success"] is False
        assert "Simulation is running. Please call /stop first or use force=true to force restart." in resp.json["error"]
        assert not mock_stop.called
        assert not mock_cleanup.called
        assert not mock_start.called


# ============================================================================
# 6. Additional Edge Cases: Dead PID, Invalid/Negative PID, Missing State File
# ============================================================================

def test_is_pid_alive_negative_and_non_numeric_variations():
    """_is_pid_alive returns False for various non-numeric, negative, and malformed inputs."""
    # Negative integers and string representations
    assert SimulationRunner._is_pid_alive(-1) is False
    assert SimulationRunner._is_pid_alive(-9999) is False
    assert SimulationRunner._is_pid_alive("-1") is False
    assert SimulationRunner._is_pid_alive("-500") is False
    assert SimulationRunner._is_pid_alive(0) is False
    assert SimulationRunner._is_pid_alive("0") is False

    # Non-numeric strings and invalid types
    assert SimulationRunner._is_pid_alive("") is False
    assert SimulationRunner._is_pid_alive("   ") is False
    assert SimulationRunner._is_pid_alive("abc") is False
    assert SimulationRunner._is_pid_alive("3.14") is False
    assert SimulationRunner._is_pid_alive([]) is False
    assert SimulationRunner._is_pid_alive({}) is False
    assert SimulationRunner._is_pid_alive(()) is False
    assert SimulationRunner._is_pid_alive(object()) is False

    # Valid numeric string of alive process should return True
    assert SimulationRunner._is_pid_alive(str(os.getpid())) is True


def test_is_process_alive_when_state_file_does_not_exist(clean_simulation_runner):
    """is_process_alive returns False when run_state.json does not exist on disk."""
    sim_id = "sim_non_existent"
    # Ensure completely absent from memory and disk
    assert sim_id not in SimulationRunner._processes
    assert sim_id not in SimulationRunner._run_states

    assert SimulationRunner.is_process_alive(sim_id) is False

    # Test loading state from disk on-demand if run_state.json exists
    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        process_pid=65432,
    )
    SimulationRunner._save_run_state(state)
    SimulationRunner._run_states.clear()  # Evict from in-memory cache

    with patch.object(SimulationRunner, "_is_pid_alive", return_value=True) as mock_alive:
        assert SimulationRunner.is_process_alive(sim_id) is True
        mock_alive.assert_called_once_with(65432)


def test_terminate_pid_when_process_already_dead_unix():
    """_terminate_pid returns immediately without calling SIGKILL when process is already dead."""
    pid = 98765
    sim_id = "sim_already_dead"

    killpg_calls = []

    def mock_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))
        # Raise ProcessLookupError as process no longer exists
        raise ProcessLookupError("No such process")

    with patch("os.getpgid", side_effect=ProcessLookupError), \
         patch("os.killpg", side_effect=mock_killpg), \
         patch.object(SimulationRunner, "_is_pid_alive", return_value=False):
        SimulationRunner._terminate_pid(pid, sim_id, timeout=10)

    # Only initial SIGTERM attempted; returns immediately without escalating to SIGKILL
    assert killpg_calls == [(pid, signal.SIGTERM)]


def test_terminate_pid_when_process_dies_after_first_check():
    """_terminate_pid exits immediately when _is_pid_alive returns False on initial loop check."""
    pid = 98766
    sim_id = "sim_dies_fast"

    killpg_calls = []

    def mock_killpg(pgid, sig):
        killpg_calls.append((pgid, sig))

    with patch("os.getpgid", return_value=pid), \
         patch("os.killpg", side_effect=mock_killpg), \
         patch.object(SimulationRunner, "_is_pid_alive", return_value=False):
        SimulationRunner._terminate_pid(pid, sim_id, timeout=10)

    # SIGTERM was sent, process was already dead on first while check, so no SIGKILL sent
    assert killpg_calls == [(pid, signal.SIGTERM)]


def test_terminate_pid_windows_error_handled():
    """_terminate_pid catches taskkill exceptions on Windows without raising."""
    from app.services import simulation_runner

    with patch.object(simulation_runner, "IS_WINDOWS", True), \
         patch("subprocess.run", side_effect=Exception("Process not found")):
        # Should catch exception and not raise
        SimulationRunner._terminate_pid(1234, "sim_win_err", timeout=5)


def test_api_stop_completed_simulation_with_alive_process(app_client, clean_simulation_runner):
    """/api/simulation/stop stops a COMPLETED simulation if its process is still alive."""
    sim_id = "sim_api_stop_completed"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    manager = SimulationManager()
    mgr_state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_test",
        graph_id="graph_test",
        status=SimulationStatus.COMPLETED,
    )
    manager._save_simulation_state(mgr_state)

    run_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.COMPLETED,
        process_pid=54321,
    )
    SimulationRunner._save_run_state(run_state)

    with patch.object(SimulationRunner, "is_process_alive", return_value=True), \
         patch.object(SimulationRunner, "_is_pid_alive", return_value=True), \
         patch.object(SimulationRunner, "_terminate_pid") as mock_term_pid:
        resp = app_client.post("/api/simulation/stop", json={"simulation_id": sim_id})

        assert resp.status_code == 200
        assert resp.json["success"] is True
        assert resp.json["data"]["runner_status"] == "stopped"
        mock_term_pid.assert_called_once_with(54321, sim_id)


def test_api_stop_completed_simulation_with_dead_process_fails(app_client, clean_simulation_runner):
    """/api/simulation/stop rejects stopping a COMPLETED simulation if process is dead."""
    sim_id = "sim_api_stop_dead"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    manager = SimulationManager()
    mgr_state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_test",
        graph_id="graph_test",
        status=SimulationStatus.COMPLETED,
    )
    manager._save_simulation_state(mgr_state)

    run_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.COMPLETED,
        process_pid=54322,
    )
    SimulationRunner._save_run_state(run_state)

    with patch.object(SimulationRunner, "is_process_alive", return_value=False):
        resp = app_client.post("/api/simulation/stop", json={"simulation_id": sim_id})

        assert resp.status_code == 400
        assert resp.json["success"] is False
        assert "Simulation not running" in resp.json["error"]
