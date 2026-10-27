import json
from unittest.mock import MagicMock

import pytest

from app import create_app
from app.config import Config
from app.models.project import Project, ProjectManager, ProjectStatus
from app.services import simulation_manager as simulation_manager_module
from app.services.entity_reader import FilteredEntities
from app.services.simulation_manager import (
    SimulationManager,
    SimulationState,
    SimulationStatus,
)
from app.models.task import TaskManager, TaskStatus


def _write_failed_state(root, simulation_id="sim_failed"):
    sim_dir = root / simulation_id
    sim_dir.mkdir(parents=True)
    (sim_dir / "state.json").write_text(
        json.dumps(
            {
                "status": "failed",
                "error": "no usable entities",
                "entities_count": 0,
                "profiles_generated": False,
                "config_generated": False,
            }
        ),
        encoding="utf-8",
    )
    return simulation_id


def test_realtime_endpoints_expose_terminal_failure(tmp_path, monkeypatch):
    simulation_id = _write_failed_state(tmp_path)
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(tmp_path))

    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()

    config_response = client.get(f"/api/simulation/{simulation_id}/config/realtime")
    profile_response = client.get(f"/api/simulation/{simulation_id}/profiles/realtime")

    assert config_response.status_code == 200
    expected_config_state = {
        "status": "failed",
        "error": "no usable entities",
        "generation_stage": "failed",
        "profiles_generated": False,
        "config_generated": False,
        "is_generating": False,
    }
    for key, expected in expected_config_state.items():
        assert config_response.json["data"][key] == expected
    assert profile_response.status_code == 200
    assert profile_response.json["data"]["status"] == "failed"
    assert profile_response.json["data"]["error"] == "no usable entities"
    assert profile_response.json["data"]["is_generating"] is False


def test_zero_entities_persists_failed_state_and_raises(tmp_path, monkeypatch):
    class EmptyReader:
        def __init__(self, *args, **kwargs):
            pass

        def filter_defined_entities(self, **kwargs):
            return FilteredEntities(
                entities=[],
                entity_types=set(),
                total_count=3,
                filtered_count=0,
            )

    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(simulation_manager_module, "EntityReader", EmptyReader)

    manager = SimulationManager()
    state = SimulationState(
        simulation_id="sim_empty",
        project_id="project",
        graph_id="graph",
        status=SimulationStatus.READY,
        profiles_generated=True,
        config_generated=True,
        config_reasoning="stale",
        error="stale",
    )
    manager._save_simulation_state(state)

    dummy_storage = MagicMock()
    with pytest.raises(ValueError, match="No matching entities found"):
        manager.prepare_simulation(
            simulation_id=state.simulation_id,
            simulation_requirement="requirement",
            document_text="document",
            storage=dummy_storage,
        )

    persisted = json.loads(
        (tmp_path / state.simulation_id / "state.json").read_text(encoding="utf-8")
    )
    assert persisted["status"] == "failed"
    assert persisted["profiles_generated"] is False
    assert persisted["config_generated"] is False
    assert persisted["config_reasoning"] == ""
    assert "No matching entities found" in persisted["error"]


def test_prepare_simulation_task_manager_fails_on_failed_result(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path))

    task_manager = TaskManager()
    task_manager._tasks.clear()

    # Mock ProjectManager
    project = Project(
        project_id="proj_test",
        name="Test Project",
        status=ProjectStatus.GRAPH_COMPLETED,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        simulation_requirement="Simulate discussions",
    )
    monkeypatch.setattr(ProjectManager, "get_project", lambda pid: project)
    monkeypatch.setattr(ProjectManager, "get_extracted_text", lambda pid: "Doc text")

    # Storage provided via app.extensions['neo4j_storage']

    # Create initial simulation state
    manager = SimulationManager()
    sim_state = SimulationState(
        simulation_id="sim_task_fail",
        project_id="proj_test",
        graph_id="graph_test",
        status=SimulationStatus.CREATED,
    )
    manager._save_simulation_state(sim_state)

    # Mock prepare_simulation to return failed state
    def mock_prepare_simulation(*args, **kwargs):
        failed_state = SimulationState(
            simulation_id="sim_task_fail",
            project_id="proj_test",
            graph_id="graph_test",
            status=SimulationStatus.FAILED,
            error="Preparation failed due to invalid entity graph",
        )
        manager._save_simulation_state(failed_state)
        return failed_state

    monkeypatch.setattr(SimulationManager, "prepare_simulation", mock_prepare_simulation)

    # Run thread target synchronously
    import threading
    class SyncThread:
        def __init__(self, target, daemon=True):
            self.target = target
        def start(self):
            self.target()

    monkeypatch.setattr(threading, "Thread", SyncThread)

    app = create_app()
    app.config.update(TESTING=True)
    app.extensions['neo4j_storage'] = MagicMock()
    client = app.test_client()
    response = client.post(
        "/api/simulation/prepare",
        json={"simulation_id": "sim_task_fail", "force_regenerate": True},
    )
    assert response.status_code == 200
    task_id = response.json["data"]["task_id"]

    task = task_manager.get_task(task_id)
    assert task.status == TaskStatus.FAILED
    assert "Preparation failed due to invalid entity graph" in task.error
