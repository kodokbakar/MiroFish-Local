"""
Regression test for Issue #33 / Item 065:
Stale env_status.json without OS process check blocks interview endpoints.

Demonstrates:
When env_status.json contains {"status": "alive"} but the simulation OS process
is dead or not running, SimulationRunner.check_env_alive(simulation_id) must verify
process liveness and return False, rather than blindly trusting the JSON file and
causing WSGI threads to hang on IPC polling.
"""

import json
import os
from unittest.mock import MagicMock, mock_open, patch

import pytest

from app import create_app
from app.services.simulation_manager import SimulationManager
from app.services.simulation_runner import (
    RunnerStatus,
    SimulationRunner,
    SimulationRunState,
)


@pytest.fixture(autouse=True)
def clean_simulation_runner(tmp_path, monkeypatch):
    """Isolate runner storage and in-memory state for every test."""
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


def _write_env_status(sim_dir: str, status: str = "alive"):
    """Helper to write env_status.json."""
    status_file = os.path.join(sim_dir, "env_status.json")
    with open(status_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "status": status,
                "twitter_available": True,
                "reddit_available": True,
                "timestamp": "2026-11-15T12:00:00",
            },
            f,
        )


def _create_mock_process(pid: int = 12345, returncode=None):
    """Helper to create a mock subprocess.Popen instance."""
    proc = MagicMock()
    proc.pid = pid
    proc.poll.return_value = returncode
    return proc


def test_check_env_alive_returns_false_when_process_exited(clean_simulation_runner):
    """
    When env_status.json says 'alive' but the managed process in _processes
    has already terminated (poll() returns exit code), check_env_alive must return False.
    """
    sim_id = "sim_dead_proc_1"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    # Mock process has exited with returncode 1
    dead_proc = _create_mock_process(pid=54321, returncode=1)
    SimulationRunner._processes[sim_id] = dead_proc

    # BUG: currently SimulationRunner.check_env_alive only checks env_status.json
    # and returns True despite the OS process being dead.
    assert SimulationRunner.check_env_alive(sim_id) is False


def test_check_env_alive_returns_false_when_process_pid_not_alive(clean_simulation_runner):
    """
    When env_status.json says 'alive' and no in-memory process handle exists,
    but run_state has a PID that is no longer alive in the OS,
    check_env_alive must return False.
    """
    sim_id = "sim_dead_pid_2"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    # Create run_state with a dead PID (PID 99999999 is nonexistent)
    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        process_pid=99999999,
    )
    SimulationRunner._save_run_state(state)

    with patch.object(SimulationRunner, "_is_pid_alive", return_value=False):
        assert SimulationRunner.check_env_alive(sim_id) is False


def test_check_env_alive_returns_false_when_no_process_tracking(clean_simulation_runner):
    """
    When env_status.json says 'alive' but there is no running process handle
    and no run_state with an active PID, check_env_alive must return False.
    """
    sim_id = "sim_no_proc_3"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    # No process in _processes, no run_state saved
    assert SimulationRunner.check_env_alive(sim_id) is False


def test_check_env_alive_returns_true_when_process_and_env_both_alive(clean_simulation_runner):
    """
    When env_status.json says 'alive' AND the process is genuinely running,
    check_env_alive must return True.
    """
    sim_id = "sim_alive_proc_4"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    live_proc = _create_mock_process(pid=11111, returncode=None)
    SimulationRunner._processes[sim_id] = live_proc

    assert SimulationRunner.check_env_alive(sim_id) is True


def test_check_env_alive_returns_false_when_env_status_stopped_even_if_process_alive(clean_simulation_runner):
    """
    When the process is alive but env_status.json says 'stopped',
    check_env_alive must return False.
    """
    sim_id = "sim_stopped_env_5"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="stopped")

    live_proc = _create_mock_process(pid=22222, returncode=None)
    SimulationRunner._processes[sim_id] = live_proc

    assert SimulationRunner.check_env_alive(sim_id) is False


def test_env_status_endpoint_reports_not_alive_when_process_dead(clean_simulation_runner, app_client):
    """
    API endpoint POST /api/simulation/env-status should report env_alive=False
    when env_status.json is stale 'alive' but process is dead.
    """
    sim_id = "sim_env_status_endpoint_6"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    dead_proc = _create_mock_process(pid=33333, returncode=1)
    SimulationRunner._processes[sim_id] = dead_proc

    response = app_client.post(
        "/api/simulation/env-status",
        json={"simulation_id": sim_id},
    )

    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    # BUG: currently data["data"]["env_alive"] is True because check_env_alive returned True
    assert data["data"]["env_alive"] is False
    assert "not running or closed" in data["data"]["message"]


def test_interview_endpoint_rejects_immediately_when_process_dead(clean_simulation_runner, app_client):
    """
    API endpoint POST /api/simulation/interview should immediately return 400
    without attempting IPC dispatch when env_status.json has 'alive' but OS process is dead.
    """
    sim_id = "sim_endpoint_stale_7"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    dead_proc = _create_mock_process(pid=44444, returncode=1)
    SimulationRunner._processes[sim_id] = dead_proc

    # In production, if check_env_alive returns True, interview_agent would be called
    # and hang on ipc_client.send_interview waiting for response.
    # When check_env_alive properly returns False, it rejects with 400 immediately.
    with patch.object(SimulationRunner, "interview_agent", return_value={"success": True}) as mock_interview:
        response = app_client.post(
            "/api/simulation/interview",
            json={"simulation_id": sim_id, "agent_id": 1, "prompt": "Hello?"},
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "not running or closed" in data["error"]
        mock_interview.assert_not_called()


def test_check_env_alive_updates_env_status_file_to_stopped(clean_simulation_runner):
    """When check_env_alive finds OS process is dead, env_status.json should be marked stopped."""
    sim_id = "sim_status_update_8"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    dead_proc = _create_mock_process(pid=88888, returncode=1)
    SimulationRunner._processes[sim_id] = dead_proc

    assert SimulationRunner.check_env_alive(sim_id) is False

    status_file = os.path.join(sim_dir, "env_status.json")
    with open(status_file, "r", encoding="utf-8") as f:
        status_data = json.load(f)
    assert status_data["status"] == "stopped"


def test_stop_simulation_updates_env_status_file_to_stopped(clean_simulation_runner):
    """When stop_simulation is called, env_status.json should be marked stopped."""
    sim_id = "sim_stop_sim_9"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        process_pid=99999,
    )
    SimulationRunner._save_run_state(state)

    with patch.object(SimulationRunner, "_is_pid_alive", return_value=False):
        SimulationRunner.stop_simulation(sim_id)

    status_file = os.path.join(sim_dir, "env_status.json")
    with open(status_file, "r", encoding="utf-8") as f:
        status_data = json.load(f)
    assert status_data["status"] == "stopped"


def test_interview_agent_raises_value_error_immediately_when_process_dead(clean_simulation_runner):
    """SimulationRunner.interview_agent must fail-fast with ValueError when process is dead."""
    sim_id = "sim_interview_direct_10"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    dead_proc = _create_mock_process(pid=77777, returncode=1)
    SimulationRunner._processes[sim_id] = dead_proc

    with pytest.raises(ValueError, match="not running or closed"):
        SimulationRunner.interview_agent(simulation_id=sim_id, agent_id=1, prompt="Hello")


def test_interview_agents_batch_raises_value_error_immediately_when_process_dead(clean_simulation_runner):
    """SimulationRunner.interview_agents_batch must fail-fast with ValueError when process is dead."""
    sim_id = "sim_batch_direct_11"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    dead_proc = _create_mock_process(pid=66666, returncode=1)
    SimulationRunner._processes[sim_id] = dead_proc

    with pytest.raises(ValueError, match="not running or closed"):
        SimulationRunner.interview_agents_batch(
            simulation_id=sim_id,
            interviews=[{"agent_id": 1, "prompt": "Hello"}],
        )



# ============== Edge-case tests ==============


@pytest.mark.parametrize("corrupted_content", [
    "{invalid_json: true,",
    "not even json",
    "   ",
])
def test_check_env_alive_malformed_json_returns_false(clean_simulation_runner, corrupted_content):
    """
    Malformed or corrupted JSON in env_status.json must return False gracefully
    both when process is running and when process is dead.
    """
    sim_id = "sim_malformed_json_12"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    status_file = os.path.join(sim_dir, "env_status.json")
    with open(status_file, "w", encoding="utf-8") as f:
        f.write(corrupted_content)

    # 1. With live process
    live_proc = _create_mock_process(pid=12345, returncode=None)
    SimulationRunner._processes[sim_id] = live_proc
    assert SimulationRunner.check_env_alive(sim_id) is False

    # 2. With dead process
    dead_proc = _create_mock_process(pid=12345, returncode=1)
    SimulationRunner._processes[sim_id] = dead_proc
    assert SimulationRunner.check_env_alive(sim_id) is False


def test_check_env_alive_empty_file_returns_false(clean_simulation_runner):
    """Empty env_status.json must return False gracefully."""
    sim_id = "sim_empty_file_13"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    status_file = os.path.join(sim_dir, "env_status.json")
    with open(status_file, "w", encoding="utf-8") as f:
        f.write("")

    live_proc = _create_mock_process(pid=12345, returncode=None)
    SimulationRunner._processes[sim_id] = live_proc
    assert SimulationRunner.check_env_alive(sim_id) is False


@pytest.mark.parametrize("non_dict_json", [
    "[]",
    "[\"alive\"]",
    "\"alive\"",
    "123",
    "null",
    "true",
])
def test_check_env_alive_non_dict_json_returns_false(clean_simulation_runner, non_dict_json):
    """
    When env_status.json contains valid JSON that is not a dict (list, string, int, null, bool),
    check_env_alive must return False gracefully without raising AttributeError or TypeError.
    """
    sim_id = "sim_non_dict_14"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    status_file = os.path.join(sim_dir, "env_status.json")
    with open(status_file, "w", encoding="utf-8") as f:
        f.write(non_dict_json)

    live_proc = _create_mock_process(pid=12345, returncode=None)
    SimulationRunner._processes[sim_id] = live_proc
    assert SimulationRunner.check_env_alive(sim_id) is False


def test_check_env_alive_nonexistent_simulation_id_returns_false(clean_simulation_runner):
    """Non-existent simulation_id returns False gracefully without raising exception."""
    assert SimulationRunner.check_env_alive("completely_nonexistent_sim_999") is False


def test_env_status_endpoint_nonexistent_simulation_returns_gracefully(app_client):
    """API endpoint POST /api/simulation/env-status with nonexistent simulation_id returns 200 with env_alive=False."""
    response = app_client.post(
        "/api/simulation/env-status",
        json={"simulation_id": "sim_nonexistent_999"},
    )
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["data"]["env_alive"] is False


def test_check_env_alive_returns_false_for_zombie_process(clean_simulation_runner):
    """
    When PID exists in OS process table but is a dead zombie process (/proc check State: Z),
    check_env_alive must return False.
    """
    sim_id = "sim_zombie_15"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        process_pid=77889,
    )
    SimulationRunner._save_run_state(state)

    zombie_proc_status = "Name:\tpython\nState:\tZ (zombie)\nTgid:\t77889\nPid:\t77889\n"
    with (
        patch("os.kill", return_value=None),
        patch("os.path.exists", side_effect=lambda p: p in {"/proc/77889/status", sim_dir, os.path.join(sim_dir, "env_status.json")}),
        patch("builtins.open", mock_open(read_data=zombie_proc_status)),
    ):
        assert SimulationRunner.check_env_alive(sim_id) is False


def test_check_env_alive_returns_false_for_unrelated_process_permission_error(clean_simulation_runner):
    """
    When PID exists in OS but is owned by another user/unrelated process
    causing os.kill(pid, 0) to raise PermissionError (EPERM), check_env_alive must return False.
    """
    sim_id = "sim_unrelated_proc_16"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        process_pid=1,
    )
    SimulationRunner._save_run_state(state)

    with patch("os.kill", side_effect=PermissionError(1, "Operation not permitted")):
        assert SimulationRunner.check_env_alive(sim_id) is False


def test_interview_agent_process_dies_during_call_raises_timeout(clean_simulation_runner):
    """
    Race condition: process is alive during check_env_alive check, but dies right after.
    The IPC call must raise TimeoutError when timeout expires rather than hanging indefinitely.
    """
    sim_id = "sim_race_timeout_17"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    live_proc = _create_mock_process(pid=88990, returncode=None)
    SimulationRunner._processes[sim_id] = live_proc

    with pytest.raises(TimeoutError, match="Timeout waiting for command response"):
        SimulationRunner.interview_agent(
            simulation_id=sim_id,
            agent_id=1,
            prompt="Hello after process died",
            timeout=0.1,
        )


def test_interview_endpoint_race_timeout_returns_504(clean_simulation_runner, app_client):
    """
    Race condition via API: process dies right after check_env_alive check,
    causing interview call to time out; endpoint returns HTTP 504.
    """
    sim_id = "sim_race_api_504_18"
    sim_dir = os.path.join(clean_simulation_runner, sim_id)
    os.makedirs(sim_dir, exist_ok=True)
    _write_env_status(sim_dir, status="alive")

    live_proc = _create_mock_process(pid=88991, returncode=None)
    SimulationRunner._processes[sim_id] = live_proc

    response = app_client.post(
        "/api/simulation/interview",
        json={
            "simulation_id": sim_id,
            "agent_id": 1,
            "prompt": "Hello race condition",
            "timeout": 0.1,
        },
    )
    assert response.status_code == 504
    data = response.get_json()
    assert data["success"] is False
    assert "timeout" in data["error"].lower()
