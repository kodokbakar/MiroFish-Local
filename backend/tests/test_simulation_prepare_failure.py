import json
from pathlib import Path
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


def test_simulation_state_profiles_generated_serialization(tmp_path, monkeypatch):
    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path))

    state_default = SimulationState(
        simulation_id="sim_default",
        project_id="proj",
        graph_id="graph",
    )
    assert state_default.profiles_generated is False
    assert state_default.to_dict()["profiles_generated"] is False
    assert state_default.to_simple_dict()["profiles_generated"] is False

    state_generated = SimulationState(
        simulation_id="sim_gen",
        project_id="proj",
        graph_id="graph",
        profiles_generated=True,
    )
    assert state_generated.profiles_generated is True
    assert state_generated.to_dict()["profiles_generated"] is True
    assert state_generated.to_simple_dict()["profiles_generated"] is True

    manager = SimulationManager()

    # Test loading state with profiles_generated=True
    dir_true = tmp_path / "sim_load_true"
    dir_true.mkdir(parents=True)
    (dir_true / "state.json").write_text(json.dumps({"profiles_generated": True}), encoding="utf-8")
    loaded_true = manager._load_simulation_state("sim_load_true")
    assert loaded_true.profiles_generated is True

    # Test loading state with profiles_generated=False
    dir_false = tmp_path / "sim_load_false"
    dir_false.mkdir(parents=True)
    (dir_false / "state.json").write_text(json.dumps({"profiles_generated": False}), encoding="utf-8")
    loaded_false = manager._load_simulation_state("sim_load_false")
    assert loaded_false.profiles_generated is False

    # Test loading state where profiles_generated is missing (backward compatibility)
    dir_missing = tmp_path / "sim_load_missing"
    dir_missing.mkdir(parents=True)
    (dir_missing / "state.json").write_text(json.dumps({}), encoding="utf-8")
    loaded_missing = manager._load_simulation_state("sim_load_missing")
    assert loaded_missing.profiles_generated is False

def test_realtime_endpoints_handle_empty_and_corrupt_state(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(tmp_path))

    # 1. Empty state file
    sim_empty_dir = tmp_path / "sim_empty"
    sim_empty_dir.mkdir(parents=True)
    (sim_empty_dir / "state.json").write_text("{}", encoding="utf-8")

    # 2. Corrupted JSON state file
    sim_corrupt_dir = tmp_path / "sim_corrupt"
    sim_corrupt_dir.mkdir(parents=True)
    (sim_corrupt_dir / "state.json").write_text("{broken json", encoding="utf-8")

    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()

    # Verify empty state handling
    config_res = client.get("/api/simulation/sim_empty/config/realtime")
    assert config_res.status_code == 200
    assert config_res.json["data"]["profiles_generated"] is False
    assert config_res.json["data"]["is_generating"] is False
    assert config_res.json["data"]["generation_stage"] is None

    profiles_res = client.get("/api/simulation/sim_empty/profiles/realtime")
    assert profiles_res.status_code == 200
    assert profiles_res.json["data"]["is_generating"] is False
    assert profiles_res.json["data"]["profiles"] == []

    # Verify corrupted state handling (graceful fallback, no 500)
    config_corrupt_res = client.get("/api/simulation/sim_corrupt/config/realtime")
    assert config_corrupt_res.status_code == 200
    assert config_corrupt_res.json["data"]["profiles_generated"] is False
    assert config_corrupt_res.json["data"]["is_generating"] is False

    profiles_corrupt_res = client.get("/api/simulation/sim_corrupt/profiles/realtime")
    assert profiles_corrupt_res.status_code == 200
    assert profiles_corrupt_res.json["data"]["is_generating"] is False

    # Verify non-existent simulation 404
    not_found_res = client.get("/api/simulation/non_existent/config/realtime")
    assert not_found_res.status_code == 404


def test_prepare_simulation_task_manager_fails_on_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path))

    task_manager = TaskManager()
    task_manager._tasks.clear()

    project = Project(
        project_id="proj_exc",
        name="Test Project Exc",
        status=ProjectStatus.GRAPH_COMPLETED,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        simulation_requirement="Simulate discussions",
    )
    monkeypatch.setattr(ProjectManager, "get_project", lambda pid: project)
    monkeypatch.setattr(ProjectManager, "get_extracted_text", lambda pid: "Doc text")

    manager = SimulationManager()
    sim_state = SimulationState(
        simulation_id="sim_task_exc",
        project_id="proj_exc",
        graph_id="graph_exc",
        status=SimulationStatus.CREATED,
    )
    manager._save_simulation_state(sim_state)

    def mock_prepare_raise(*args, **kwargs):
        raise RuntimeError("LLM service unavailable during preparation")

    monkeypatch.setattr(SimulationManager, "prepare_simulation", mock_prepare_raise)

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
        json={"simulation_id": "sim_task_exc", "force_regenerate": True},
    )
    assert response.status_code == 200
    task_id = response.json["data"]["task_id"]

    task = task_manager.get_task(task_id)
    assert task.status == TaskStatus.FAILED
    assert "LLM service unavailable during preparation" in task.error

    persisted = json.loads(
        (tmp_path / "sim_task_exc" / "state.json").read_text(encoding="utf-8")
    )
    assert persisted["status"] == "failed"
    assert "LLM service unavailable during preparation" in persisted["error"]


class TestStep2EnvSetupVueInvariants:
    """Verify Step2EnvSetup.vue structural invariants for failure propagation and polling stop."""

    @pytest.fixture(autouse=True)
    def setup_vue_content(self):
        repo_root = Path(__file__).resolve().parents[2]
        vue_path = repo_root / "frontend" / "src" / "components" / "Step2EnvSetup.vue"
        assert vue_path.is_file(), f"Missing {vue_path}"
        self.content = vue_path.read_text(encoding="utf-8")

    def test_handle_prepare_failure_stops_all_timers_and_emits_error(self):
        fn_start = self.content.find("const handlePrepareFailure =")
        assert fn_start != -1, "handlePrepareFailure function not found in Step2EnvSetup.vue"

        fn_end = self.content.find("const handleStartSimulation =", fn_start)
        assert fn_end != -1
        fn_body = self.content[fn_start:fn_end]

        assert "stopPolling()" in fn_body
        assert "stopProfilesPolling()" in fn_body
        assert "stopConfigPolling()" in fn_body
        assert "emit('update-status', 'error')" in fn_body

    def test_poll_prepare_status_handles_failure(self):
        poll_start = self.content.find("const pollPrepareStatus = async () => {")
        assert poll_start != -1, "pollPrepareStatus not found"
        poll_end = self.content.find("const fetchProfilesRealtime = async () => {", poll_start)
        assert poll_end != -1
        poll_body = self.content[poll_start:poll_end]

        assert "else if (data.status === 'failed')" in poll_body
        assert "handlePrepareFailure(data.error)" in poll_body

    def test_fetch_config_realtime_handles_failure(self):
        fetch_start = self.content.find("const fetchConfigRealtime = async () => {")
        assert fetch_start != -1, "fetchConfigRealtime not found"
        fetch_end = self.content.find("const loadPreparedData = async () => {", fetch_start)
        assert fetch_end != -1
        fetch_body = self.content[fetch_start:fetch_end]

        assert "if (data.status === 'failed' || data.error)" in fetch_body
        assert "handlePrepareFailure(data.error)" in fetch_body

    def test_load_prepared_data_handles_terminal_failure_and_missing_config(self):
        load_start = self.content.find("const loadPreparedData = async () => {")
        assert load_start != -1, "loadPreparedData not found"
        load_end = self.content.find("const logContent = ref(null)", load_start)
        assert load_end != -1
        load_body = self.content[load_start:load_end]

        assert "if (configState.status === 'failed' || configState.error)" in load_body
        assert "handlePrepareFailure(configState.error)" in load_body
        assert "handlePrepareFailure('Simulation config is missing and the backend is no longer generating it')" in load_body
        assert "catch (err)" in load_body
        assert "handlePrepareFailure(`Failed to load configuration: ${err.message}`)" in load_body
