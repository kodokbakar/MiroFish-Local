"""
Regression tests for Issue #92: Synchronize simulation start with mutex lock to eliminate TOCTOU zombie subprocesses.

Covers:
1. Concurrent invocations of SimulationRunner.start_simulation() for the same simulation_id
   suffer from a TOCTOU race condition where multiple subprocesses are spawned and process
   handles are clobbered instead of atomically locking and rejecting the duplicate invocation.
2. start_simulation() does not reject when runner_status is STARTING if process_pid is not yet alive
   due to improper liveness coupling in status check.
3. Concurrent POST requests to /api/simulation/start for the same simulation_id spawn duplicate
   processes and both return 200 OK instead of atomically serializing/rejecting the duplicate call.
"""

import json
import os
import threading
import time
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
    SimulationRunner,
    SimulationRunState,
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

    SimulationRunner._cleanup_done = False
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


def _setup_simulation_dir(sim_dir: str, simulation_id: str):
    """Create simulation config and directory needed by start_simulation."""
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


def test_concurrent_start_simulation_rejects_duplicate_and_spawns_single_process(clean_simulation_runner):
    """
    Concurrent calls to SimulationRunner.start_simulation() for the same simulation_id
    must acquire a mutex lock, spawn exactly one subprocess, and reject the duplicate
    invocation with an error (e.g. ValueError).
    """
    sim_id = "sim_concurrent_start_toctou"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    popen_instances = []
    pids = [10011, 10012]
    t1_in_popen = threading.Event()

    def fake_popen(*args, **kwargs):
        pid = pids.pop(0) if pids else 99999
        proc = MagicMock()
        proc.pid = pid
        proc.poll.return_value = None
        proc.returncode = None
        popen_instances.append(proc)
        # Signal that Thread 1 has passed initial check and reached Popen
        t1_in_popen.set()
        # Sleep to allow Thread 2 to attempt start during this window
        time.sleep(0.05)
        return proc

    results = []
    errors = []

    def target_t1():
        try:
            res = SimulationRunner.start_simulation(sim_id, platform="twitter")
            results.append(res)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    def target_t2():
        assert t1_in_popen.wait(timeout=2.0)
        try:
            res = SimulationRunner.start_simulation(sim_id, platform="twitter")
            results.append(res)
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    with patch("subprocess.Popen", side_effect=fake_popen) as mock_popen, \
         patch.object(SimulationRunner, "_monitor_simulation"):
        t1 = threading.Thread(target=target_t1)
        t2 = threading.Thread(target=target_t2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    # Under proper mutex synchronization:
    # 1. subprocess.Popen must be called exactly once
    assert mock_popen.call_count == 1, (
        f"Expected exactly 1 subprocess spawned, but got {mock_popen.call_count}. "
        "Concurrent calls spawned duplicate zombie subprocesses due to missing start mutex."
    )
    # 2. Exactly one start_simulation must succeed and one must fail with ValueError
    assert len(results) == 1, f"Expected 1 successful start, got {len(results)}"
    assert len(errors) == 1, f"Expected 1 rejected start, got {len(errors)}"
    assert isinstance(errors[0], ValueError)
    assert "already running" in str(errors[0]).lower() or "already starting" in str(errors[0]).lower()

    # 3. Exactly one process tracked in runner matching the single spawned process
    tracked_proc = SimulationRunner._processes.get(sim_id)
    assert tracked_proc is not None
    assert tracked_proc.pid == popen_instances[0].pid


def test_start_simulation_rejects_when_runner_status_is_starting_without_alive_pid(clean_simulation_runner):
    """
    If a simulation is in STARTING status (transitioning before process PID is alive/registered),
    start_simulation must reject duplicate start instead of proceeding to spawn another process.
    """
    sim_id = "sim_starting_not_yet_alive"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    # Simulate in-progress start where state is STARTING but process handle / PID not yet alive
    starting_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.STARTING,
        process_pid=None,
        started_at="2026-01-01T00:00:00",
    )
    SimulationRunner._run_states[sim_id] = starting_state
    SimulationRunner._save_run_state(starting_state)

    mock_proc = MagicMock()
    mock_proc.pid = 99988
    mock_proc.poll.return_value = None

    with patch("subprocess.Popen", return_value=mock_proc) as mock_popen, \
         patch.object(SimulationRunner, "_monitor_simulation"):
        with pytest.raises(ValueError, match="already running|already starting"):
            SimulationRunner.start_simulation(sim_id, platform="twitter")

        assert mock_popen.call_count == 0, (
            "start_simulation spawned a process even though simulation status was STARTING."
        )


def test_api_concurrent_start_rejects_duplicate_and_spawns_single_process(app_client, clean_simulation_runner):
    """
    Concurrent POST requests to /api/simulation/start for the same simulation_id
    must result in exactly 1 subprocess spawned, 1 HTTP 200, and 1 error response (400/409).
    """
    sim_id = "sim_api_concurrent_toctou"
    _setup_simulation_dir(clean_simulation_runner, sim_id)

    manager = SimulationManager()
    state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_concurrency",
        graph_id="graph_concurrency",
        status=SimulationStatus.READY,
    )
    manager._save_simulation_state(state)

    popen_instances = []
    pids = [20011, 20012]
    t1_saving_starting = threading.Event()

    orig_save_run_state = SimulationRunner._save_run_state

    def hook_save_run_state(run_state):
        if (
            run_state.simulation_id == sim_id
            and run_state.runner_status == RunnerStatus.STARTING
            and not t1_saving_starting.is_set()
        ):
            t1_saving_starting.set()
            time.sleep(0.05)
        return orig_save_run_state(run_state)

    def fake_popen(*args, **kwargs):
        pid = pids.pop(0) if pids else 99999
        proc = MagicMock()
        proc.pid = pid
        proc.poll.return_value = None
        proc.returncode = None
        popen_instances.append(proc)
        return proc

    responses = []

    def call_t1():
        resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id, "platform": "twitter"})
        responses.append(resp)

    def call_t2():
        assert t1_saving_starting.wait(timeout=2.0)
        resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id, "platform": "twitter"})
        responses.append(resp)

    with patch.object(SimulationRunner, "_save_run_state", side_effect=hook_save_run_state), \
         patch("subprocess.Popen", side_effect=fake_popen) as mock_popen, \
         patch.object(SimulationRunner, "_monitor_simulation"):
        t1 = threading.Thread(target=call_t1)
        t2 = threading.Thread(target=call_t2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

    # Subprocess.Popen must be called exactly once
    assert mock_popen.call_count == 1, (
        f"Expected exactly 1 subprocess spawned, got {mock_popen.call_count}."
    )

    status_codes = [r.status_code for r in responses]
    assert 200 in status_codes, f"Expected one 200 response, got {status_codes}"
    error_codes = [code for code in status_codes if code != 200]
    assert len(error_codes) == 1, f"Expected exactly one error response, got {status_codes}"
    assert error_codes[0] in [400, 409], f"Unexpected error status: {error_codes[0]}"
