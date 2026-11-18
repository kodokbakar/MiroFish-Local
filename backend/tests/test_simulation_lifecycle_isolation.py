"""
Regression tests for Issue #103 (Item 080):
1. stop_simulation() in backend/app/api/simulation.py sets status = SimulationStatus.PAUSED
   in state.json instead of SimulationStatus.STOPPED.
2. Restarting a stopped simulation opens actions.jsonl in append mode ('a') without isolating
   or rotating residual events from previous runs. When _read_action_log() encounters a residual
   simulation_end event from the prior run, it marks the runner status as COMPLETED immediately at round 0.
"""

import json
import os

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
from scripts.action_logger import PlatformActionLogger


@pytest.fixture
def test_env(tmp_path, monkeypatch):
    """Isolate simulation manager and runner storage directories."""
    sim_data_dir = str(tmp_path / "simulations_data")
    runner_dir = str(tmp_path / "simulations_runner")
    os.makedirs(sim_data_dir, exist_ok=True)
    os.makedirs(runner_dir, exist_ok=True)

    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", sim_data_dir)
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", runner_dir)

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

    app = create_app()
    app.config.update(TESTING=True)

    with app.test_client() as client:
        yield client, sim_data_dir, runner_dir

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


def test_stop_simulation_sets_stopped_status_in_state_json(test_env):
    """
    Demonstrate that stopping a simulation must set SimulationStatus.STOPPED in state.json,
    but currently sets SimulationStatus.PAUSED in backend/app/api/simulation.py:1812.
    """
    client, sim_data_dir, _runner_dir = test_env
    sim_id = "sim_test_stop_lifecycle_state"

    # Set up initial running simulation state in SimulationManager
    manager = SimulationManager()
    state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_103",
        graph_id="graph_103",
        status=SimulationStatus.RUNNING,
    )
    manager._save_simulation_state(state)

    # Set up matching running state in SimulationRunner
    run_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        current_round=3,
        total_rounds=10,
    )
    SimulationRunner._save_run_state(run_state)

    # Call /api/simulation/stop
    response = client.post("/api/simulation/stop", json={"simulation_id": sim_id})
    assert response.status_code == 200
    res_data = response.get_json()
    assert res_data.get("success") is True

    # Check 1: Persisted state.json on disk
    state_file = os.path.join(sim_data_dir, sim_id, "state.json")
    assert os.path.exists(state_file), "state.json must exist"
    with open(state_file, "r", encoding="utf-8") as f:
        persisted_data = json.load(f)

    # Expected: STOPPED ("stopped")
    # Current bug: PAUSED ("paused")
    assert persisted_data.get("status") == SimulationStatus.STOPPED.value, (
        f"state.json status must be '{SimulationStatus.STOPPED.value}', "
        f"got '{persisted_data.get('status')}'"
    )

    # Check 2: Reloaded state through SimulationManager
    fresh_manager = SimulationManager()
    reloaded_state = fresh_manager._load_simulation_state(sim_id)
    assert reloaded_state.status == SimulationStatus.STOPPED, (
        f"SimulationManager state.status must be SimulationStatus.STOPPED, "
        f"got {reloaded_state.status}"
    )

    # Check 3: GET /api/simulation/<simulation_id>
    get_res = client.get(f"/api/simulation/{sim_id}")
    assert get_res.status_code == 200
    assert get_res.get_json()["data"]["status"] == SimulationStatus.STOPPED.value


def test_restarted_simulation_isolates_residual_action_logs_and_prevents_premature_completed_status(test_env):
    """
    Demonstrate that restarting a stopped simulation opens actions.jsonl in append mode ('a')
    without isolating/rotating residual events from previous runs. When _read_action_log() encounters
    the residual simulation_end event from the prior run, it marks the runner status as COMPLETED
    prematurely at round 0.
    """
    _client, _sim_data_dir, runner_dir = test_env
    sim_id = "sim_test_action_log_residual"
    sim_dir = os.path.join(runner_dir, sim_id)
    twitter_dir = os.path.join(sim_dir, "twitter")
    os.makedirs(twitter_dir, exist_ok=True)
    actions_file = os.path.join(twitter_dir, "actions.jsonl")

    # --- Run 1: Prior run logged actions and terminated with simulation_end ---
    logger_run1 = PlatformActionLogger("twitter", sim_dir)
    logger_run1.log_action(
        round_num=1,
        agent_id=1,
        agent_name="Agent_Old",
        action_type="POST",
        action_args={"content": "prior run action"},
        success=True,
    )
    logger_run1.log_simulation_end(total_rounds=1, total_actions=1)

    with open(actions_file, "r", encoding="utf-8") as f:
        run1_lines = f.readlines()
    assert len(run1_lines) == 2
    assert "simulation_end" in run1_lines[1]

    # --- Run 2: Simulation is restarted (starting anew at round 0) ---
    new_run_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        current_round=0,
        total_rounds=10,
        twitter_running=True,
        twitter_completed=False,
    )
    SimulationRunner._save_run_state(new_run_state)

    # Logger for restarted run opens actions.jsonl and logs round 0 start
    logger_run2 = PlatformActionLogger("twitter", sim_dir)
    logger_run2.log_round_start(round_num=0, simulated_hour=0)

    # Runner reads action logs for the new run starting from offset 0
    # BUG: actions.jsonl was not isolated/rotated across runs, so it still contains Run 1's simulation_end.
    # _read_action_log encounters the residual simulation_end and prematurely marks
    # runner_status = COMPLETED and twitter_completed = True at round 0.
    SimulationRunner._read_action_log(actions_file, 0, new_run_state, "twitter")

    assert not new_run_state.twitter_completed, (
        "twitter_completed must not be True at round 0 of a restarted simulation "
        "due to residual events from prior runs"
    )
    assert new_run_state.runner_status == RunnerStatus.RUNNING, (
        f"Restarted simulation runner_status must remain RUNNING at round 0, "
        f"but got premature completion: {new_run_state.runner_status}"
    )
