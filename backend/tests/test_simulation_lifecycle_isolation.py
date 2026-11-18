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
import re

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


def test_repeated_stop_calls_maintain_stopped_status_idempotently(test_env):
    """
    Verify repeated stop calls on a simulation:
    - First stop transitions status to SimulationStatus.STOPPED.
    - Subsequent stop attempts maintain SimulationStatus.STOPPED idempotently without
      corrupting state or raising unhandled 500 server errors.
    - Persisted state on disk and in SimulationManager remains STOPPED.
    """
    client, sim_data_dir, _runner_dir = test_env
    sim_id = "sim_test_repeated_stop"

    manager = SimulationManager()
    state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_103",
        graph_id="graph_103",
        status=SimulationStatus.RUNNING,
    )
    manager._save_simulation_state(state)

    run_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        current_round=2,
        total_rounds=10,
    )
    SimulationRunner._save_run_state(run_state)

    # First stop call -> 200 OK, transitions status to STOPPED
    res1 = client.post("/api/simulation/stop", json={"simulation_id": sim_id})
    assert res1.status_code == 200
    assert res1.get_json()["success"] is True

    # State on disk and in a fresh SimulationManager is STOPPED
    fresh_manager1 = SimulationManager()
    state1 = fresh_manager1._load_simulation_state(sim_id)
    assert state1.status == SimulationStatus.STOPPED

    # Repeated stop call on already stopped simulation
    res2 = client.post("/api/simulation/stop", json={"simulation_id": sim_id})
    # Handled without unhandled 500 server crash
    assert res2.status_code in (200, 400)

    # SimulationStatus.STOPPED remains intact and idempotent
    fresh_manager2 = SimulationManager()
    state2 = fresh_manager2._load_simulation_state(sim_id)
    assert state2.status == SimulationStatus.STOPPED

    state_file = os.path.join(sim_data_dir, sim_id, "state.json")
    with open(state_file, "r", encoding="utf-8") as f:
        persisted = json.load(f)
    assert persisted.get("status") == SimulationStatus.STOPPED.value

    # GET /api/simulation/<id> confirms status is still stopped
    get_res = client.get(f"/api/simulation/{sim_id}")
    assert get_res.status_code == 200
    assert get_res.get_json()["data"]["status"] == SimulationStatus.STOPPED.value


def test_dual_platform_log_isolation_on_restart(test_env):
    """
    Verify both twitter and reddit actions.jsonl files are properly rotated
    when restarting a simulation from round 0, preventing premature completion
    on either platform.
    """
    _client, _sim_data_dir, runner_dir = test_env
    sim_id = "sim_test_dual_platform_isolation"
    sim_dir = os.path.join(runner_dir, sim_id)
    twitter_dir = os.path.join(sim_dir, "twitter")
    reddit_dir = os.path.join(sim_dir, "reddit")
    os.makedirs(twitter_dir, exist_ok=True)
    os.makedirs(reddit_dir, exist_ok=True)

    twitter_actions = os.path.join(twitter_dir, "actions.jsonl")
    reddit_actions = os.path.join(reddit_dir, "actions.jsonl")

    # --- Run 1: Prior run logs actions and terminates with simulation_end on BOTH platforms ---
    twitter_logger1 = PlatformActionLogger("twitter", sim_dir)
    twitter_logger1.log_action(
        round_num=1,
        agent_id=1,
        agent_name="Agent_Twitter",
        action_type="POST",
        action_args={"content": "prior twitter post"},
    )
    twitter_logger1.log_simulation_end(total_rounds=1, total_actions=1)

    reddit_logger1 = PlatformActionLogger("reddit", sim_dir)
    reddit_logger1.log_action(
        round_num=1,
        agent_id=2,
        agent_name="Agent_Reddit",
        action_type="COMMENT",
        action_args={"content": "prior reddit comment"},
    )
    reddit_logger1.log_simulation_end(total_rounds=1, total_actions=1)

    assert os.path.exists(twitter_actions)
    assert os.path.exists(reddit_actions)

    # --- Run 2: Simulation is restarted from round 0 on dual platforms ---
    new_run_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        current_round=0,
        total_rounds=10,
        twitter_running=True,
        twitter_completed=False,
        reddit_running=True,
        reddit_completed=False,
    )
    SimulationRunner._save_run_state(new_run_state)

    # Loggers for restarted run start at round 0
    twitter_logger2 = PlatformActionLogger("twitter", sim_dir)
    twitter_logger2.log_round_start(round_num=0, simulated_hour=0)

    reddit_logger2 = PlatformActionLogger("reddit", sim_dir)
    reddit_logger2.log_round_start(round_num=0, simulated_hour=0)

    # Read action logs from offset 0 for both platforms
    SimulationRunner._read_action_log(twitter_actions, 0, new_run_state, "twitter")
    SimulationRunner._read_action_log(reddit_actions, 0, new_run_state, "reddit")

    # Verify neither platform prematurely completed
    assert not new_run_state.twitter_completed, (
        "twitter_completed must not be prematurely True after rotation"
    )
    assert not new_run_state.reddit_completed, (
        "reddit_completed must not be prematurely True after rotation"
    )
    assert new_run_state.runner_status == RunnerStatus.RUNNING

    # Verify rotation archives were generated in both directories
    twitter_archives = [f for f in os.listdir(twitter_dir) if f.startswith("actions_") and f.endswith(".jsonl")]
    reddit_archives = [f for f in os.listdir(reddit_dir) if f.startswith("actions_") and f.endswith(".jsonl")]
    assert len(twitter_archives) == 1, f"Expected 1 twitter archive, found {twitter_archives}"
    assert len(reddit_archives) == 1, f"Expected 1 reddit archive, found {reddit_archives}"


def test_cold_start_simulation_no_prior_logs(test_env):
    """
    Verify starting a simulation when no prior actions.jsonl exists starts
    cleanly without rotation error or unnecessary archive files.
    """
    _client, _sim_data_dir, runner_dir = test_env
    sim_id = "sim_test_cold_start"
    sim_dir = os.path.join(runner_dir, sim_id)

    # Cold start: no existing simulation directory or action logs
    assert not os.path.exists(sim_dir)

    logger = PlatformActionLogger("twitter", sim_dir)
    # log_simulation_start and log_round_start should execute without error
    logger.log_simulation_start({"simulation_id": sim_id, "mode": "cold_start"})
    logger.log_round_start(round_num=0, simulated_hour=0)
    logger.log_action(
        round_num=0,
        agent_id=1,
        agent_name="Agent_Cold",
        action_type="POST",
        action_args={"content": "cold start post"},
    )

    twitter_dir = os.path.join(sim_dir, "twitter")
    actions_file = os.path.join(twitter_dir, "actions.jsonl")
    assert os.path.exists(actions_file), "actions.jsonl must exist for cold start"

    # Verify no archive files were generated
    archives = [f for f in os.listdir(twitter_dir) if f.startswith("actions_") and f.endswith(".jsonl")]
    assert len(archives) == 0, f"Cold start must not create rotated archive files, found {archives}"

    # Verify actions.jsonl contains exactly 3 entries
    with open(actions_file, "r", encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == 3
    assert lines[0].get("event_type") == "simulation_start"
    assert lines[1].get("event_type") == "round_start"
    assert lines[2].get("action_type") == "POST"


def test_preserved_archive_integrity_on_rotation(test_env):
    """
    Verify rotated log files retain their full original JSONL content with the
    timestamp suffix and correct event structure.
    """
    _client, _sim_data_dir, runner_dir = test_env
    sim_id = "sim_test_archive_integrity"
    sim_dir = os.path.join(runner_dir, sim_id)
    twitter_dir = os.path.join(sim_dir, "twitter")

    # Run 1: Log specific identifiable records
    logger_run1 = PlatformActionLogger("twitter", sim_dir)
    logger_run1.log_simulation_start({"agent_configs": [{"id": 1}], "time_config": {"total_simulation_hours": 12}})
    logger_run1.log_round_start(round_num=0, simulated_hour=0)
    logger_run1.log_action(
        round_num=0,
        agent_id=42,
        agent_name="Agent_42",
        action_type="BROADCAST",
        action_args={"msg": "important message", "priority": 1},
        result="success",
    )
    logger_run1.log_simulation_end(total_rounds=1, total_actions=1)

    # Capture original file content
    original_actions_file = os.path.join(twitter_dir, "actions.jsonl")
    with open(original_actions_file, "r", encoding="utf-8") as f:
        original_content = f.read()
    original_lines = original_content.strip().splitlines()
    assert len(original_lines) == 4

    # Run 2: Restart from round 0 triggers rotation
    logger_run2 = PlatformActionLogger("twitter", sim_dir)
    logger_run2.log_simulation_start({"agent_configs": [{"id": 1}, {"id": 2}, {"id": 3}], "time_config": {"total_simulation_hours": 36}})

    # Inspect archives in twitter directory
    archives = [f for f in os.listdir(twitter_dir) if f.startswith("actions_") and f.endswith(".jsonl")]
    assert len(archives) == 1, f"Expected exactly 1 archive file, found {archives}"
    archive_name = archives[0]

    # Verify archive filename timestamp suffix format: actions_YYYYMMDD_HHMMSS_ffffff.jsonl
    pattern = r"^actions_\d{8}_\d{6}_\d{6}\.jsonl$"
    assert re.match(pattern, archive_name), f"Archive filename '{archive_name}' does not match pattern {pattern}"

    # Verify archive content matches original Run 1 content line-for-line
    archive_path = os.path.join(twitter_dir, archive_name)
    with open(archive_path, "r", encoding="utf-8") as f:
        archived_content = f.read()
    assert archived_content == original_content, (
        "Rotated log file must preserve the exact full JSONL content of the prior run"
    )

    # Verify active actions.jsonl only contains Run 2 entries
    with open(original_actions_file, "r", encoding="utf-8") as f:
        active_lines = [json.loads(line) for line in f if line.strip()]
    assert len(active_lines) == 1
    assert active_lines[0].get("agents_count") == 3
    assert active_lines[0].get("total_rounds") == 72
