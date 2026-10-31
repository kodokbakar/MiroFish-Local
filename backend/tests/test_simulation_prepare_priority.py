"""
Tests for Issue #32:
1. Task priority over simulation_id check in get_prepare_status
2. Preparing status never treated as prepared in _check_simulation_prepared and never auto-mutated to ready
3. Force regenerate resets config_generated and profiles_generated
"""

import csv
import json
import os
from unittest.mock import patch

import pytest

from app import create_app
from app.api.simulation import _check_simulation_prepared
from app.config import Config
from app.models.task import TaskManager, TaskStatus
from app.models.project import Project, ProjectManager, ProjectStatus
from app.services.simulation_manager import SimulationManager, SimulationStatus
from app.services.simulation_runner import SimulationRunner


@pytest.fixture
def test_sim_dir(tmp_path, monkeypatch):
    """Set up temporary simulation and project directories."""
    data_dir = str(tmp_path / "simulations")
    os.makedirs(data_dir, exist_ok=True)
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", data_dir)
    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", data_dir)
    monkeypatch.setattr(SimulationRunner, "RUN_STATE_DIR", data_dir)
    return data_dir


@pytest.fixture
def app_client(test_sim_dir):
    """Flask test client with mocked neo4j_storage."""
    with patch("app.storage.Neo4jStorage"):
        app = create_app()
    app.config["TESTING"] = True
    return app.test_client()


def _create_sim(
    base_dir: str,
    sim_id: str,
    project_id: str = "proj_1",
    enable_twitter: bool = True,
    enable_reddit: bool = True,
    status: str = "ready",
    config_generated: bool = True,
    profiles_generated: bool = True,
    twitter_profiles_count: int = 2,
    reddit_profiles_count: int = 2,
) -> str:
    sim_dir = os.path.join(base_dir, sim_id)
    os.makedirs(sim_dir, exist_ok=True)

    state_data = {
        "simulation_id": sim_id,
        "project_id": project_id,
        "graph_id": "graph_1",
        "status": status,
        "enable_twitter": enable_twitter,
        "enable_reddit": enable_reddit,
        "config_generated": config_generated,
        "profiles_generated": profiles_generated,
        "profiles_count": twitter_profiles_count + reddit_profiles_count,
        "entities_count": 10,
        "entity_types": ["User"],
        "created_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    with open(os.path.join(sim_dir, "state.json"), "w", encoding="utf-8") as f:
        json.dump(state_data, f, indent=2)

    config_data = {
        "simulation_id": sim_id,
        "time_config": {"total_simulation_hours": 24, "minutes_per_round": 30},
    }
    with open(os.path.join(sim_dir, "simulation_config.json"), "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)

    if enable_reddit and reddit_profiles_count > 0:
        reddit_profiles = [{"id": f"r_{i}", "name": f"RedditUser_{i}"} for i in range(reddit_profiles_count)]
        with open(os.path.join(sim_dir, "reddit_profiles.json"), "w", encoding="utf-8") as f:
            json.dump(reddit_profiles, f, indent=2)

    if enable_twitter and twitter_profiles_count > 0:
        csv_path = os.path.join(sim_dir, "twitter_profiles.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["user_id", "username", "name", "bio"])
            writer.writeheader()
            for i in range(twitter_profiles_count):
                writer.writerow({
                    "user_id": str(i + 1),
                    "username": f"tw_user_{i+1}",
                    "name": f"Twitter User {i+1}",
                    "bio": f"Bio of user {i+1}",
                })

    return sim_id


# ============================================================================
# 1. Task priority over simulation_id check in get_prepare_status
# ============================================================================

class TestPrepareStatusTaskPriority:
    """Verifies that an active task_id takes priority over simulation_id in /prepare/status."""

    def test_task_priority_over_simulation_id_json_body(self, app_client, test_sim_dir):
        """When task_id is present and task exists, return task status even if simulation is prepared."""
        sim_id = _create_sim(test_sim_dir, "sim_priority_json")

        task_manager = TaskManager()
        task_id = task_manager.create_task(
            task_type="simulation_prepare",
            metadata={"simulation_id": sim_id},
        )
        task_manager.update_task(
            task_id,
            status=TaskStatus.PROCESSING,
            progress=35,
            message="Generating agent profiles...",
            progress_detail={
                "current_stage": "generating_profiles",
                "current_stage_name": "Generate Agent persona",
            },
        )

        resp = app_client.post(
            "/api/simulation/prepare/status",
            json={"task_id": task_id, "simulation_id": sim_id},
        )
        assert resp.status_code == 200
        assert resp.json["success"] is True
        data = resp.json["data"]
        assert data["task_id"] == task_id
        assert data["status"] == "processing"
        assert data["progress"] == 35
        assert data["already_prepared"] is False
        assert data["message"] == "Generating agent profiles..."
        assert data["current_step"] == "Generate Agent persona"

    def test_task_priority_over_simulation_id_query_params_get(self, app_client, test_sim_dir):
        """GET request with query params prioritizes active task over prepared simulation_id."""
        sim_id = _create_sim(test_sim_dir, "sim_priority_get")

        task_manager = TaskManager()
        task_id = task_manager.create_task(
            task_type="simulation_prepare",
            metadata={"simulation_id": sim_id},
        )
        task_manager.update_task(
            task_id,
            status=TaskStatus.PROCESSING,
            progress=50,
            message="Generating simulation config...",
            progress_detail={
                "current_stage": "generating_config",
                "current_stage_name": "Generate simulation configuration",
            },
        )

        resp = app_client.get(f"/api/simulation/prepare/status?task_id={task_id}&simulation_id={sim_id}")
        assert resp.status_code == 200
        assert resp.json["success"] is True
        data = resp.json["data"]
        assert data["task_id"] == task_id
        assert data["status"] == "processing"
        assert data["progress"] == 50
        assert data["already_prepared"] is False
        assert data["current_step"] == "Generate simulation configuration"

    def test_task_priority_over_simulation_id_query_params_post(self, app_client, test_sim_dir):
        """POST request with query params prioritizes active task over prepared simulation_id."""
        sim_id = _create_sim(test_sim_dir, "sim_priority_post_query")

        task_manager = TaskManager()
        task_id = task_manager.create_task(
            task_type="simulation_prepare",
            metadata={"simulation_id": sim_id},
        )
        task_manager.update_task(
            task_id,
            status=TaskStatus.PROCESSING,
            progress=20,
            message="Reading knowledge graph entities...",
            progress_detail={
                "current_stage": "reading",
                "current_stage_name": "Read knowledge graph entities",
            },
        )

        resp = app_client.post(f"/api/simulation/prepare/status?task_id={task_id}&simulation_id={sim_id}")
        assert resp.status_code == 200
        assert resp.json["success"] is True
        data = resp.json["data"]
        assert data["task_id"] == task_id
        assert data["status"] == "processing"
        assert data["progress"] == 20
        assert data["already_prepared"] is False

    def test_fallback_to_simulation_id_when_task_id_omitted(self, app_client, test_sim_dir):
        """When task_id is omitted, falls back to checking simulation_id preparation."""
        sim_id = _create_sim(test_sim_dir, "sim_fallback_no_task")

        resp = app_client.post(
            "/api/simulation/prepare/status",
            json={"simulation_id": sim_id},
        )
        assert resp.status_code == 200
        assert resp.json["success"] is True
        data = resp.json["data"]
        assert data["already_prepared"] is True
        assert data["status"] == "ready"
        assert data["progress"] == 100

    def test_fallback_to_simulation_id_when_task_is_none_and_sim_prepared(self, app_client, test_sim_dir):
        """When task_id is supplied but task does not exist, fall back to simulation_id if prepared."""
        sim_id = _create_sim(test_sim_dir, "sim_fallback_task_none")

        resp = app_client.post(
            "/api/simulation/prepare/status",
            json={"task_id": "nonexistent_task_123", "simulation_id": sim_id},
        )
        assert resp.status_code == 200
        assert resp.json["success"] is True
        data = resp.json["data"]
        assert data["already_prepared"] is True
        assert data["status"] == "ready"
        assert data["progress"] == 100

    def test_task_not_found_and_sim_not_prepared_returns_404(self, app_client, test_sim_dir):
        """When task_id is unknown and simulation is not prepared, returns 404."""
        resp = app_client.post(
            "/api/simulation/prepare/status",
            json={"task_id": "nonexistent_task_404", "simulation_id": "sim_not_found"},
        )
        assert resp.status_code == 404
        assert resp.json["success"] is False
        assert "Task does not exist" in resp.json["error"]

    def test_neither_task_nor_simulation_returns_400(self, app_client):
        """Missing both task_id and simulation_id returns 400."""
        resp = app_client.post("/api/simulation/prepare/status", json={})
        assert resp.status_code == 400
        assert resp.json["success"] is False
        assert "Please provide task_id Or simulation_id" in resp.json["error"]


# ============================================================================
# 2. Preparing status never treated as prepared in _check_simulation_prepared
# ============================================================================

class TestCheckSimulationPreparingStatus:
    """Verifies that status 'preparing' returns (False, 'preparing') and never auto-mutates."""

    def test_preparing_status_never_treated_as_prepared(self, test_sim_dir):
        """_check_simulation_prepared returns (False, 'preparing') when state.json status is 'preparing'."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_preparing_check",
            status="preparing",
            config_generated=True,
            profiles_generated=True,
        )

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is False
        assert info == "preparing"

        # Ensure state.json status is NOT mutated to ready
        state_file = os.path.join(test_sim_dir, sim_id, "state.json")
        with open(state_file, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        assert persisted["status"] == "preparing"

    def test_prepare_status_endpoint_returns_preparing_when_no_task_id(self, app_client, test_sim_dir):
        """POST /api/simulation/prepare/status returns status='preparing' and already_prepared=False."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_prep_endpoint",
            status="preparing",
            config_generated=True,
            profiles_generated=True,
        )

        resp = app_client.post(
            "/api/simulation/prepare/status",
            json={"simulation_id": sim_id},
        )
        assert resp.status_code == 200
        assert resp.json["success"] is True
        data = resp.json["data"]
        assert data["already_prepared"] is False
        assert data["status"] == "preparing"
        assert data["progress"] == 0


# ============================================================================
# 3. Force regenerate resets config_generated and profiles_generated
# ============================================================================

class TestForceRegenerateResetsFlags:
    """Verifies that force_regenerate=True resets state flags synchronously."""

    def test_force_regenerate_resets_config_and_profiles_generated(self, app_client, test_sim_dir):
        """Calling /prepare with force_regenerate=True sets config_generated and profiles_generated to False."""
        project_id = "proj_test_force"
        sim_id = _create_sim(
            test_sim_dir,
            "sim_force_regen",
            project_id=project_id,
            status="ready",
            config_generated=True,
            profiles_generated=True,
        )

        # Mock project retrieval so prepare_simulation doesn't fail on project check
        mock_project = Project(
            project_id=project_id,
            name="Test Project",
            status=ProjectStatus.GRAPH_COMPLETED,
            created_at="2026-01-01T00:00:00",
            updated_at="2026-01-01T00:00:00",
            simulation_requirement="Test simulation requirements description",
        )

        with patch.object(ProjectManager, "get_project", return_value=mock_project), \
             patch.object(ProjectManager, "get_extracted_text", return_value="Sample doc text"), \
             patch("threading.Thread") as mock_thread, \
             patch("app.api.simulation.EntityReader") as mock_reader:

            mock_reader.return_value.filter_defined_entities.return_value.filtered_count = 5
            mock_reader.return_value.filter_defined_entities.return_value.entity_types = ["User"]

            resp = app_client.post(
                "/api/simulation/prepare",
                json={"simulation_id": sim_id, "force_regenerate": True},
            )
            assert resp.status_code == 200
            assert resp.json["success"] is True
            assert resp.json["data"]["status"] == "preparing"

        # Check that simulation state flags were persisted as False
        manager = SimulationManager()
        state = manager.get_simulation(sim_id)
        assert state is not None
        assert state.config_generated is False
        assert state.profiles_generated is False
        assert state.status == SimulationStatus.PREPARING

        # Check persisted file on disk
        state_file = os.path.join(test_sim_dir, sim_id, "state.json")
        with open(state_file, "r", encoding="utf-8") as f:
            disk_data = json.load(f)
        assert disk_data["config_generated"] is False
        assert disk_data["profiles_generated"] is False
        assert disk_data["status"] == "preparing"

    def test_non_forced_prepare_skips_when_already_prepared(self, app_client, test_sim_dir):
        """Calling /prepare with force_regenerate=False on prepared sim returns already_prepared=True."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_skip_regen",
            status="ready",
            config_generated=True,
            profiles_generated=True,
        )

        with patch.object(SimulationManager, "prepare_simulation") as mock_prep:
            resp = app_client.post(
                "/api/simulation/prepare",
                json={"simulation_id": sim_id, "force_regenerate": False},
            )
            assert resp.status_code == 200
            assert resp.json["success"] is True
            assert resp.json["data"]["already_prepared"] is True
            assert resp.json["data"]["status"] == "ready"
            mock_prep.assert_not_called()
