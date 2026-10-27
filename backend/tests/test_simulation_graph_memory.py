"""
Tests for Knowledge Graph Memory Updates during simulation (Issue #3).

Covers:
- SimulationRunner.is_graph_memory_enabled()
- SimulationRunner.start_simulation() storage parameter validation & updater handling
- SimulationRunner._read_action_log() activity forwarding to graph memory updater
- POST /api/simulation/start endpoint storage injection and error handling
"""

import json
from unittest.mock import MagicMock, patch
import pytest

from app import create_app
from app.models.project import ProjectManager
from app.services.simulation_manager import (
    SimulationManager,
    SimulationState,
    SimulationStatus,
)
from app.services.simulation_runner import (
    SimulationRunner,
    SimulationRunState,
    RunnerStatus,
)
from app.services.graph_memory_updater import GraphMemoryManager


@pytest.fixture(autouse=True)
def cleanup_runner_state():
    """Reset SimulationRunner internal states between tests."""
    SimulationRunner._graph_memory_enabled.clear()
    SimulationRunner._run_states.clear()
    SimulationRunner._processes.clear()
    yield
    SimulationRunner._graph_memory_enabled.clear()
    SimulationRunner._run_states.clear()
    SimulationRunner._processes.clear()


# ============================================================================
# 1. Unit tests for SimulationRunner.is_graph_memory_enabled
# ============================================================================

def test_is_graph_memory_enabled():
    """is_graph_memory_enabled returns correct boolean based on _graph_memory_enabled dict."""
    SimulationRunner._graph_memory_enabled["sim_active"] = True
    SimulationRunner._graph_memory_enabled["sim_inactive"] = False

    assert SimulationRunner.is_graph_memory_enabled("sim_active") is True
    assert SimulationRunner.is_graph_memory_enabled("sim_inactive") is False
    assert SimulationRunner.is_graph_memory_enabled("sim_nonexistent") is False


# ============================================================================
# 2. Unit tests for SimulationRunner.start_simulation storage validation
# ============================================================================

def test_start_simulation_raises_when_storage_missing(tmp_path, monkeypatch):
    """Direct call to start_simulation raises ValueError if enable_graph_memory_update=True and storage=None."""
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))

    sim_id = "sim_no_storage"
    sim_dir = tmp_path / sim_id
    sim_dir.mkdir(parents=True)
    config_file = sim_dir / "simulation_config.json"
    config_file.write_text(json.dumps({"time_config": {"total_simulation_hours": 24}}), encoding="utf-8")

    with pytest.raises(ValueError, match="Must provide storage \\(GraphStorage\\) when enabling graph memory update"):
        SimulationRunner.start_simulation(
            simulation_id=sim_id,
            enable_graph_memory_update=True,
            graph_id="graph_test",
            storage=None,
        )

    assert SimulationRunner.is_graph_memory_enabled(sim_id) is False


def test_start_simulation_raises_when_graph_id_missing(tmp_path, monkeypatch):
    """Direct call to start_simulation raises ValueError if enable_graph_memory_update=True and graph_id is None."""
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))

    sim_id = "sim_no_graph_id"
    sim_dir = tmp_path / sim_id
    sim_dir.mkdir(parents=True)
    config_file = sim_dir / "simulation_config.json"
    config_file.write_text(json.dumps({"time_config": {"total_simulation_hours": 24}}), encoding="utf-8")

    with pytest.raises(ValueError, match="Must provide graph_id when enabling graph memory update"):
        SimulationRunner.start_simulation(
            simulation_id=sim_id,
            enable_graph_memory_update=True,
            graph_id=None,
            storage=MagicMock(),
        )


def test_start_simulation_handles_updater_exception_gracefully(tmp_path, monkeypatch):
    """If create_updater raises exception, simulation continues but graph memory update is disabled."""
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))

    sim_id = "sim_updater_fail"
    sim_dir = tmp_path / sim_id
    sim_dir.mkdir(parents=True)
    config_file = sim_dir / "simulation_config.json"
    config_file.write_text(json.dumps({"time_config": {"total_simulation_hours": 24}}), encoding="utf-8")

    mock_process = MagicMock()
    mock_process.pid = 99999
    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: mock_process)
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    monkeypatch.setattr(
        GraphMemoryManager,
        "create_updater",
        MagicMock(side_effect=RuntimeError("Neo4j connection dropped")),
    )

    state = SimulationRunner.start_simulation(
        simulation_id=sim_id,
        enable_graph_memory_update=True,
        graph_id="graph_test",
        storage=MagicMock(),
    )

    assert state.runner_status == RunnerStatus.RUNNING
    assert SimulationRunner.is_graph_memory_enabled(sim_id) is False


def test_start_simulation_success_enables_graph_memory(tmp_path, monkeypatch):
    """When storage is provided and updater created, graph memory is enabled."""
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", str(tmp_path))

    sim_id = "sim_success"
    sim_dir = tmp_path / sim_id
    sim_dir.mkdir(parents=True)
    config_file = sim_dir / "simulation_config.json"
    config_file.write_text(json.dumps({"time_config": {"total_simulation_hours": 24}}), encoding="utf-8")

    mock_process = MagicMock()
    mock_process.pid = 99999
    mock_storage = MagicMock()
    mock_create = MagicMock()

    monkeypatch.setattr("subprocess.Popen", lambda *args, **kwargs: mock_process)
    monkeypatch.setattr("threading.Thread.start", lambda self: None)
    monkeypatch.setattr(GraphMemoryManager, "create_updater", mock_create)

    state = SimulationRunner.start_simulation(
        simulation_id=sim_id,
        enable_graph_memory_update=True,
        graph_id="graph_test",
        storage=mock_storage,
    )

    assert state.runner_status == RunnerStatus.RUNNING
    mock_create.assert_called_once_with(sim_id, "graph_test", mock_storage)
    assert SimulationRunner.is_graph_memory_enabled(sim_id) is True


# ============================================================================
# 3. Unit tests for SimulationRunner._read_action_log
# ============================================================================

def test_read_action_log_forwards_to_updater_when_enabled(tmp_path, monkeypatch):
    """When graph memory is enabled, _read_action_log forwards action dicts to graph_updater.add_activity_from_dict."""
    sim_id = "sim_action_log_enabled"
    log_file = tmp_path / "actions.jsonl"

    action_1 = {
        "round": 1,
        "agent_id": 10,
        "agent_name": "agent_alpha",
        "action_type": "post",
        "action_args": {"content": "First post"},
        "result": "created",
        "success": True,
    }
    action_2 = {
        "round": 2,
        "agent_id": 20,
        "agent_name": "agent_beta",
        "action_type": "like",
        "action_args": {"post_id": 123},
        "result": "liked",
        "success": True,
    }
    lines = [
        json.dumps({"event_type": "round_end", "round": 1}),
        json.dumps(action_1),
        "invalid json line",
        json.dumps(action_2),
        json.dumps({"event_type": "simulation_end", "total_rounds": 2, "total_actions": 2}),
    ]
    log_file.write_text("\n".join(lines) + "\n", encoding="utf-8")

    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.RUNNING)
    SimulationRunner._graph_memory_enabled[sim_id] = True

    mock_updater = MagicMock()
    monkeypatch.setattr(GraphMemoryManager, "get_updater", lambda sid: mock_updater if sid == sim_id else None)

    new_pos = SimulationRunner._read_action_log(str(log_file), 0, state, "twitter")

    assert new_pos > 0
    assert mock_updater.add_activity_from_dict.call_count == 2
    mock_updater.add_activity_from_dict.assert_any_call(action_1, "twitter")
    mock_updater.add_activity_from_dict.assert_any_call(action_2, "twitter")
    assert len(state.recent_actions) == 2
    assert state.current_round == 2


def test_read_action_log_does_not_forward_when_disabled(tmp_path, monkeypatch):
    """When graph memory is disabled, _read_action_log does NOT forward actions to graph updater."""
    sim_id = "sim_action_log_disabled"
    log_file = tmp_path / "actions.jsonl"

    action_data = {
        "round": 1,
        "agent_id": 15,
        "agent_name": "agent_gamma",
        "action_type": "comment",
        "action_args": {"content": "Nice post"},
        "result": "commented",
        "success": True,
    }
    log_file.write_text(json.dumps(action_data) + "\n", encoding="utf-8")

    state = SimulationRunState(simulation_id=sim_id, runner_status=RunnerStatus.RUNNING)
    SimulationRunner._graph_memory_enabled[sim_id] = False

    mock_get_updater = MagicMock()
    monkeypatch.setattr(GraphMemoryManager, "get_updater", mock_get_updater)

    new_pos = SimulationRunner._read_action_log(str(log_file), 0, state, "reddit")

    assert new_pos > 0
    mock_get_updater.assert_not_called()
    assert len(state.recent_actions) == 1


# ============================================================================
# 4. API endpoint tests for POST /api/simulation/start
# ============================================================================

def _setup_flask_app_with_sim(monkeypatch, sim_id="sim_api_test", graph_id="graph_api_test", status=SimulationStatus.READY):
    """Helper to initialize Flask test app and register simulation state."""
    app = create_app()
    app.config.update(TESTING=True)

    sim_state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_api_test",
        graph_id=graph_id,
        status=status,
    )
    monkeypatch.setattr(SimulationManager, "get_simulation", lambda self, sid: sim_state if sid == sim_id else None)
    monkeypatch.setattr(SimulationManager, "_save_simulation_state", lambda self, s: None)

    return app, sim_state


def test_start_endpoint_passes_storage_when_graph_memory_enabled(monkeypatch):
    """POST /api/simulation/start with enable_graph_memory_update=True retrieves neo4j_storage and passes to runner."""
    sim_id = "sim_enable_graph"
    app, _ = _setup_flask_app_with_sim(monkeypatch, sim_id=sim_id)

    mock_storage = MagicMock()
    app.extensions["neo4j_storage"] = mock_storage

    dummy_run_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
    )
    mock_start_simulation = MagicMock(return_value=dummy_run_state)
    monkeypatch.setattr(SimulationRunner, "start_simulation", mock_start_simulation)
    monkeypatch.setattr(SimulationRunner, "is_graph_memory_enabled", lambda sid: True)

    client = app.test_client()
    response = client.post(
        "/api/simulation/start",
        json={
            "simulation_id": sim_id,
            "enable_graph_memory_update": True,
        },
    )

    assert response.status_code == 200
    data = response.json["data"]
    assert data["graph_memory_update_enabled"] is True
    assert data["graph_id"] == "graph_api_test"

    mock_start_simulation.assert_called_once_with(
        simulation_id=sim_id,
        platform="parallel",
        max_rounds=None,
        enable_graph_memory_update=True,
        graph_id="graph_api_test",
        storage=mock_storage,
    )


def test_start_endpoint_returns_500_when_storage_missing(monkeypatch):
    """POST /api/simulation/start with enable_graph_memory_update=True returns 500 when neo4j_storage is missing/None."""
    sim_id = "sim_missing_storage"
    app, _ = _setup_flask_app_with_sim(monkeypatch, sim_id=sim_id)
    app.extensions["neo4j_storage"] = None

    client = app.test_client()
    response = client.post(
        "/api/simulation/start",
        json={
            "simulation_id": sim_id,
            "enable_graph_memory_update": True,
        },
    )

    assert response.status_code == 500
    assert response.json["success"] is False
    assert response.json["error"] == "GraphStorage not initialized — check Neo4j connection"


def test_start_endpoint_storage_none_when_graph_memory_disabled(monkeypatch):
    """POST /api/simulation/start with enable_graph_memory_update=False passes storage=None and starts normally."""
    sim_id = "sim_disabled_graph"
    app, _ = _setup_flask_app_with_sim(monkeypatch, sim_id=sim_id)
    app.extensions["neo4j_storage"] = None

    dummy_run_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
    )
    mock_start_simulation = MagicMock(return_value=dummy_run_state)
    monkeypatch.setattr(SimulationRunner, "start_simulation", mock_start_simulation)

    client = app.test_client()
    response = client.post(
        "/api/simulation/start",
        json={
            "simulation_id": sim_id,
            "enable_graph_memory_update": False,
        },
    )

    assert response.status_code == 200
    data = response.json["data"]
    assert data["graph_memory_update_enabled"] is False

    mock_start_simulation.assert_called_once_with(
        simulation_id=sim_id,
        platform="parallel",
        max_rounds=None,
        enable_graph_memory_update=False,
        graph_id=None,
        storage=None,
    )


def test_start_endpoint_reflects_actual_runner_state_when_updater_fails(monkeypatch):
    """If updater creation fails in runner, endpoint reflects False in graph_memory_update_enabled."""
    sim_id = "sim_updater_failed_endpoint"
    app, _ = _setup_flask_app_with_sim(monkeypatch, sim_id=sim_id)

    mock_storage = MagicMock()
    app.extensions["neo4j_storage"] = mock_storage

    dummy_run_state = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
    )
    mock_start_simulation = MagicMock(return_value=dummy_run_state)
    monkeypatch.setattr(SimulationRunner, "start_simulation", mock_start_simulation)
    # Runner reports disabled because updater creation failed
    monkeypatch.setattr(SimulationRunner, "is_graph_memory_enabled", lambda sid: False)

    client = app.test_client()
    response = client.post(
        "/api/simulation/start",
        json={
            "simulation_id": sim_id,
            "enable_graph_memory_update": True,
        },
    )

    assert response.status_code == 200
    data = response.json["data"]
    assert data["graph_memory_update_enabled"] is False


def test_start_endpoint_returns_400_when_graph_id_missing(monkeypatch):
    """POST /api/simulation/start with enable_graph_memory_update=True returns 400 when graph_id is missing."""
    sim_id = "sim_no_graph_endpoint"
    app, _ = _setup_flask_app_with_sim(monkeypatch, sim_id=sim_id, graph_id=None)
    monkeypatch.setattr(ProjectManager, "get_project", lambda pid: None)
    app.extensions["neo4j_storage"] = MagicMock()

    client = app.test_client()
    response = client.post(
        "/api/simulation/start",
        json={
            "simulation_id": sim_id,
            "enable_graph_memory_update": True,
        },
    )

    assert response.status_code == 400
    assert response.json["success"] is False
    assert "valid graph_id" in response.json["error"]
