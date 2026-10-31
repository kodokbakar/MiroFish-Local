"""
Tests for state-machine deadlock fix for single-platform simulations.

Covers Issue #7:
1. _check_simulation_prepared:
   - Dynamic profile file requirements based on enable_twitter / enable_reddit
   - missing state.json handling
   - prepared_statuses includes "paused"
   - profiles_count calculation from twitter_profiles.csv and fallback
2. /api/simulation/start:
   - Restarting single-platform simulations in paused, stopped, and completed states
   - Platform defaulting logic (twitter-only -> twitter, reddit-only -> reddit, dual -> parallel)
3. /api/simulation/prepare and /api/simulation/prepare/status:
   - Recognizing single-platform simulations as already_prepared=True
"""

import csv
import json
import os
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.api.simulation import _check_simulation_prepared
from app.config import Config
from app.services.simulation_manager import SimulationManager, SimulationState, SimulationStatus
from app.services.simulation_runner import RunnerStatus, SimulationRunState, SimulationRunner


@pytest.fixture
def test_sim_dir(tmp_path, monkeypatch):
    """Set up temporary simulation directories across all components."""
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
    enable_twitter: bool = True,
    enable_reddit: bool = True,
    status: str = "ready",
    config_generated: bool = True,
    twitter_profiles_count: int = 0,
    reddit_profiles_count: int = 0,
    write_state: bool = True,
    write_config: bool = True,
    state_profiles_count: int = 0,
) -> str:
    sim_dir = os.path.join(base_dir, sim_id)
    os.makedirs(sim_dir, exist_ok=True)

    if write_state:
        state_data = {
            "simulation_id": sim_id,
            "project_id": "proj_1",
            "graph_id": "graph_1",
            "status": status,
            "enable_twitter": enable_twitter,
            "enable_reddit": enable_reddit,
            "config_generated": config_generated,
            "profiles_count": state_profiles_count,
            "entities_count": 10,
            "entity_types": ["User"],
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
        }
        with open(os.path.join(sim_dir, "state.json"), "w", encoding="utf-8") as f:
            json.dump(state_data, f, indent=2)

    if write_config:
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
# 1. _check_simulation_prepared tests
# ============================================================================

class TestCheckSimulationPrepared:
    """Tests for _check_simulation_prepared logic."""

    def test_missing_state_file_returns_clean_missing_files_error(self, test_sim_dir):
        """Missing state.json returns missing_files: ['state.json'] with existing_files: []."""
        sim_id = "sim_no_state"
        sim_dir = os.path.join(test_sim_dir, sim_id)
        os.makedirs(sim_dir, exist_ok=True)

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is False
        assert info["reason"] == "Missing required files"
        assert info["missing_files"] == ["state.json"]
        assert info["existing_files"] == []

    def test_nonexistent_directory_returns_false(self, test_sim_dir):
        """Nonexistent simulation directory returns False."""
        is_prepared, info = _check_simulation_prepared("sim_nonexistent")
        assert is_prepared is False
        assert "does not exist" in info["reason"]

    def test_twitter_only_simulation_prepared_without_reddit_profiles(self, test_sim_dir):
        """Twitter-only simulation does not require reddit_profiles.json."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_tw_only",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=3,
        )

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is True
        assert info["status"] == "ready"
        assert info["profiles_count"] == 3
        assert "reddit_profiles.json" not in info["existing_files"]
        assert "twitter_profiles.csv" in info["existing_files"]

    def test_reddit_only_simulation_prepared_without_twitter_profiles(self, test_sim_dir):
        """Reddit-only simulation does not require twitter_profiles.csv."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_rd_only",
            enable_twitter=False,
            enable_reddit=True,
            reddit_profiles_count=4,
        )

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is True
        assert info["status"] == "ready"
        assert info["profiles_count"] == 4
        assert "twitter_profiles.csv" not in info["existing_files"]
        assert "reddit_profiles.json" in info["existing_files"]

    def test_twitter_only_fails_when_twitter_profiles_csv_missing(self, test_sim_dir):
        """Twitter-only simulation fails when twitter_profiles.csv is missing."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_tw_missing_csv",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=0,  # file not created
        )

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is False
        assert "twitter_profiles.csv" in info["missing_files"]
        assert "reddit_profiles.json" not in info["missing_files"]

    def test_reddit_only_fails_when_reddit_profiles_json_missing(self, test_sim_dir):
        """Reddit-only simulation fails when reddit_profiles.json is missing."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_rd_missing_json",
            enable_twitter=False,
            enable_reddit=True,
            reddit_profiles_count=0,  # file not created
        )

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is False
        assert "reddit_profiles.json" in info["missing_files"]
        assert "twitter_profiles.csv" not in info["missing_files"]

    def test_dual_platform_requires_both_profile_files(self, test_sim_dir):
        """Dual-platform simulation requires both profile files."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_dual_missing_rd",
            enable_twitter=True,
            enable_reddit=True,
            twitter_profiles_count=2,
            reddit_profiles_count=0,  # missing reddit
        )

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is False
        assert "reddit_profiles.json" in info["missing_files"]

    @pytest.mark.parametrize("status", ["ready", "running", "paused", "stopped", "completed", "failed"])
    def test_prepared_statuses_include_all_valid_states(self, test_sim_dir, status):
        """All valid prepared statuses evaluate as prepared when config_generated=True."""
        sim_id = _create_sim(
            test_sim_dir,
            f"sim_status_{status}",
            enable_twitter=True,
            enable_reddit=False,
            status=status,
            twitter_profiles_count=2,
        )

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is True
        assert info["status"] == status

    def test_preparing_status_never_auto_updates_to_ready(self, test_sim_dir):
        """Status 'preparing' must return (False, 'preparing') and never auto-update to 'ready'."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_preparing_no_autoupdate",
            enable_twitter=True,
            enable_reddit=False,
            status="preparing",
            twitter_profiles_count=2,
        )

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is False
        assert info == "preparing"

        # Check persisted file not updated to ready
        state_file = os.path.join(test_sim_dir, sim_id, "state.json")
        with open(state_file, "r", encoding="utf-8") as f:
            persisted = json.load(f)
        assert persisted["status"] == "preparing"

    def test_unprepared_status_created_returns_false(self, test_sim_dir):
        """Status 'created' returns False even if files exist."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_created",
            enable_twitter=True,
            enable_reddit=False,
            status="created",
            twitter_profiles_count=2,
        )

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is False
        assert "Status not in prepared list" in info["reason"]

    def test_twitter_profiles_count_from_csv(self, test_sim_dir):
        """Profiles count accurately counts rows in twitter_profiles.csv."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_count_tw",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=6,
        )

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is True
        assert info["profiles_count"] == 6

    def test_profiles_count_falls_back_to_state_data_when_zero(self, test_sim_dir):
        """When profile file rows are 0, fall back to state_data profiles_count."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_fallback_count",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=0,
            state_profiles_count=9,
        )
        # Touch empty twitter_profiles.csv with only header
        csv_path = os.path.join(test_sim_dir, sim_id, "twitter_profiles.csv")
        with open(csv_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["user_id", "username", "name", "bio"])
            writer.writeheader()

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is True
        assert info["profiles_count"] == 9

    def test_corrupted_state_json_returns_error(self, test_sim_dir):
        """Corrupted state.json returns error reason."""
        sim_id = "sim_corrupted_state"
        sim_dir = os.path.join(test_sim_dir, sim_id)
        os.makedirs(sim_dir, exist_ok=True)
        with open(os.path.join(sim_dir, "state.json"), "w", encoding="utf-8") as f:
            f.write("{invalid json content")

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is False
        assert "Failed to read state file" in info["reason"]

    def test_zero_byte_empty_twitter_profiles_csv_falls_back_to_state_count(self, test_sim_dir):
        """Completely empty (0-byte) twitter_profiles.csv falls back to state_data profiles_count."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_zero_byte_tw",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=0,
            state_profiles_count=7,
        )
        csv_path = os.path.join(test_sim_dir, sim_id, "twitter_profiles.csv")
        with open(csv_path, "w", encoding="utf-8") as f:
            f.write("")

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is True
        assert info["profiles_count"] == 7

    def test_corrupted_twitter_profiles_csv_falls_back_gracefully(self, test_sim_dir):
        """Corrupted twitter_profiles.csv with invalid byte sequence falls back to state_data profiles_count."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_corrupted_tw_csv",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=0,
            state_profiles_count=11,
        )
        csv_path = os.path.join(test_sim_dir, sim_id, "twitter_profiles.csv")
        with open(csv_path, "wb") as f:
            f.write(b"\xff\xfe\x80\x80")

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is True
        assert info["profiles_count"] == 11

    def test_corrupted_reddit_profiles_json_falls_back_gracefully(self, test_sim_dir):
        """Corrupted reddit_profiles.json falls back to state_data profiles_count."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_corrupted_rd_json",
            enable_twitter=False,
            enable_reddit=True,
            reddit_profiles_count=0,
            state_profiles_count=14,
        )
        json_path = os.path.join(test_sim_dir, sim_id, "reddit_profiles.json")
        with open(json_path, "w", encoding="utf-8") as f:
            f.write("{corrupted json[")

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is True
        assert info["profiles_count"] == 14

    def test_both_platforms_disabled_requires_only_base_files(self, test_sim_dir):
        """When both platforms are disabled, neither profile file is required."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_both_disabled",
            enable_twitter=False,
            enable_reddit=False,
            twitter_profiles_count=0,
            reddit_profiles_count=0,
        )

        is_prepared, info = _check_simulation_prepared(sim_id)
        assert is_prepared is True
        assert "reddit_profiles.json" not in info["existing_files"]
        assert "twitter_profiles.csv" not in info["existing_files"]
        assert "state.json" in info["existing_files"]
        assert "simulation_config.json" in info["existing_files"]


# ============================================================================
# 2. Restart single-platform simulation from paused, stopped, completed
# ============================================================================

class TestSinglePlatformSimulationRestart:
    """Tests for restarting single-platform simulations via POST /api/simulation/start."""

    @pytest.mark.parametrize("initial_status", ["paused", "stopped", "completed", "failed"])
    def test_restart_twitter_only_from_inactive_states(self, app_client, test_sim_dir, initial_status):
        """Twitter-only simulation in paused/stopped/completed state successfully restarts."""
        sim_id = _create_sim(
            test_sim_dir,
            f"sim_tw_restart_{initial_status}",
            enable_twitter=True,
            enable_reddit=False,
            status=initial_status,
            twitter_profiles_count=3,
        )

        dummy_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=9999,
            twitter_running=True,
            reddit_running=False,
            started_at="2026-01-01T00:00:00",
        )

        with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
             patch.object(SimulationRunner, "get_run_state", return_value=None), \
             patch.object(SimulationRunner, "start_simulation", return_value=dummy_run_state) as mock_start:

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id})
            assert resp.status_code == 200
            assert resp.json["success"] is True
            assert resp.json["data"]["runner_status"] == "running"

            # Check runner was invoked with platform='twitter'
            mock_start.assert_called_once()
            call_kwargs = mock_start.call_args[1]
            assert call_kwargs["platform"] == "twitter"

        # Check simulation state in manager is RUNNING
        manager = SimulationManager()
        state = manager.get_simulation(sim_id)
        assert state.status == SimulationStatus.RUNNING

    @pytest.mark.parametrize("initial_status", ["paused", "stopped", "completed", "failed"])
    def test_restart_reddit_only_from_inactive_states(self, app_client, test_sim_dir, initial_status):
        """Reddit-only simulation in paused/stopped/completed state successfully restarts."""
        sim_id = _create_sim(
            test_sim_dir,
            f"sim_rd_restart_{initial_status}",
            enable_twitter=False,
            enable_reddit=True,
            status=initial_status,
            reddit_profiles_count=3,
        )

        dummy_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=9999,
            twitter_running=False,
            reddit_running=True,
            started_at="2026-01-01T00:00:00",
        )

        with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
             patch.object(SimulationRunner, "get_run_state", return_value=None), \
             patch.object(SimulationRunner, "start_simulation", return_value=dummy_run_state) as mock_start:

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id})
            assert resp.status_code == 200
            assert resp.json["success"] is True

            mock_start.assert_called_once()
            call_kwargs = mock_start.call_args[1]
            assert call_kwargs["platform"] == "reddit"

        manager = SimulationManager()
        state = manager.get_simulation(sim_id)
        assert state.status == SimulationStatus.RUNNING

    def test_restart_unprepared_simulation_rejected_with_400(self, app_client, test_sim_dir):
        """Unprepared simulation rejected with 400 when attempting restart."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_tw_unprepared",
            enable_twitter=True,
            enable_reddit=False,
            status="stopped",
            twitter_profiles_count=0,  # missing required profiles file
        )

        with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
             patch.object(SimulationRunner, "get_run_state", return_value=None):

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id})
            assert resp.status_code == 400
            assert resp.json["success"] is False
            assert "Please call /prepare first" in resp.json["error"]

    def test_restart_with_force_true_on_paused_simulation(self, app_client, test_sim_dir):
        """Force restart on paused single-platform simulation cleans logs and resets status."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_force_paused_tw",
            enable_twitter=True,
            enable_reddit=False,
            status="paused",
            twitter_profiles_count=3,
        )

        dummy_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=9998,
            twitter_running=True,
            reddit_running=False,
            started_at="2026-01-01T00:00:00",
        )

        with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
             patch.object(SimulationRunner, "get_run_state", return_value=None), \
             patch.object(SimulationRunner, "cleanup_simulation_logs", return_value={"success": True}) as mock_cleanup, \
             patch.object(SimulationRunner, "start_simulation", return_value=dummy_run_state):

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id, "force": True})
            assert resp.status_code == 200
            assert resp.json["success"] is True
            assert resp.json["data"]["force_restarted"] is True
            mock_cleanup.assert_called_once_with(sim_id)

        manager = SimulationManager()
        state = manager.get_simulation(sim_id)
        assert state.status == SimulationStatus.RUNNING

    def test_force_restart_when_process_is_alive(self, app_client, test_sim_dir):
        """Restarting while process is alive requires force=True to stop running process."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_alive_tw",
            enable_twitter=True,
            enable_reddit=False,
            status="running",
            twitter_profiles_count=3,
        )

        alive_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=8888,
            twitter_running=True,
            reddit_running=False,
            started_at="2026-01-01T00:00:00",
        )

        # Without force -> 400 error
        with patch.object(SimulationRunner, "is_process_alive", return_value=True), \
             patch.object(SimulationRunner, "get_run_state", return_value=alive_run_state):

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id, "force": False})
            assert resp.status_code == 400
            assert "Simulation is running" in resp.json["error"]

        new_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=8889,
            twitter_running=True,
            reddit_running=False,
            started_at="2026-01-01T00:05:00",
        )

        # With force=True -> stops simulation, cleans logs, resets status, restarts
        with patch.object(SimulationRunner, "is_process_alive", return_value=True), \
             patch.object(SimulationRunner, "get_run_state", return_value=alive_run_state), \
             patch.object(SimulationRunner, "stop_simulation") as mock_stop, \
             patch.object(SimulationRunner, "cleanup_simulation_logs", return_value={"success": True}) as mock_cleanup, \
             patch.object(SimulationRunner, "start_simulation", return_value=new_run_state) as mock_start:

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id, "force": True})
            assert resp.status_code == 200
            assert resp.json["success"] is True
            assert resp.json["data"]["force_restarted"] is True
            mock_stop.assert_called_once_with(sim_id)
            mock_cleanup.assert_called_once_with(sim_id)
            assert mock_start.call_args[1]["platform"] == "twitter"



# ============================================================================
# 3. /prepare and /prepare/status endpoint tests
# ============================================================================

class TestPrepareEndpointsSinglePlatform:
    """Tests for prepare and prepare status endpoints with single-platform simulations."""

    def test_prepare_status_twitter_only_returns_already_prepared(self, app_client, test_sim_dir):
        """POST /api/simulation/prepare/status returns already_prepared=True for Twitter-only."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_status_tw",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=5,
        )

        resp = app_client.post("/api/simulation/prepare/status", json={"simulation_id": sim_id})
        assert resp.status_code == 200
        assert resp.json["success"] is True
        data = resp.json["data"]
        assert data["already_prepared"] is True
        assert data["status"] == "ready"
        assert data["progress"] == 100
        assert data["prepare_info"]["profiles_count"] == 5

    def test_prepare_status_reddit_only_returns_already_prepared(self, app_client, test_sim_dir):
        """POST /api/simulation/prepare/status returns already_prepared=True for Reddit-only."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_status_rd",
            enable_twitter=False,
            enable_reddit=True,
            reddit_profiles_count=4,
        )

        resp = app_client.post("/api/simulation/prepare/status", json={"simulation_id": sim_id})
        assert resp.status_code == 200
        assert resp.json["success"] is True
        data = resp.json["data"]
        assert data["already_prepared"] is True
        assert data["status"] == "ready"
        assert data["prepare_info"]["profiles_count"] == 4

    def test_prepare_twitter_only_already_prepared_skips_regeneration(self, app_client, test_sim_dir):
        """POST /api/simulation/prepare returns already_prepared=True without re-running preparation."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_prepare_skip_tw",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=3,
        )

        with patch.object(SimulationManager, "prepare_simulation") as mock_prep:
            resp = app_client.post("/api/simulation/prepare", json={"simulation_id": sim_id})
            assert resp.status_code == 200
            assert resp.json["success"] is True
            data = resp.json["data"]
            assert data["already_prepared"] is True
            assert data["status"] == "ready"
            assert "No need to repeat" in data["message"] or "already completed" in data["message"].lower()

            mock_prep.assert_not_called()

    def test_prepare_reddit_only_already_prepared_skips_regeneration(self, app_client, test_sim_dir):
        """POST /api/simulation/prepare returns already_prepared=True for Reddit-only without regeneration."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_prepare_skip_rd",
            enable_twitter=False,
            enable_reddit=True,
            reddit_profiles_count=3,
        )

        with patch.object(SimulationManager, "prepare_simulation") as mock_prep:
            resp = app_client.post("/api/simulation/prepare", json={"simulation_id": sim_id})
            assert resp.status_code == 200
            assert resp.json["success"] is True
            data = resp.json["data"]
            assert data["already_prepared"] is True
            assert data["status"] == "ready"

            mock_prep.assert_not_called()

    def test_prepare_status_unprepared_single_platform_returns_not_started(self, app_client, test_sim_dir):
        """POST /api/simulation/prepare/status returns already_prepared=False when profile is missing."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_status_unprep_tw",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=0,  # profile file missing
        )

        resp = app_client.post("/api/simulation/prepare/status", json={"simulation_id": sim_id})
        assert resp.status_code == 200
        assert resp.json["success"] is True
        data = resp.json["data"]
        assert data["already_prepared"] is False
        assert data["status"] == "not_started"
        assert data["progress"] == 0



# ============================================================================
# 4. POST /api/simulation/start default platform resolution
# ============================================================================

class TestStartSimulationPlatformResolution:
    """Tests for platform parameter defaulting in POST /api/simulation/start."""

    def test_default_platform_twitter_only_defaults_to_twitter(self, app_client, test_sim_dir):
        """Omitted platform parameter on Twitter-only simulation defaults to 'twitter'."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_def_tw",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=2,
        )

        dummy_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=1001,
        )

        with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
             patch.object(SimulationRunner, "get_run_state", return_value=None), \
             patch.object(SimulationRunner, "start_simulation", return_value=dummy_run_state) as mock_start:

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id})
            assert resp.status_code == 200
            assert mock_start.call_args[1]["platform"] == "twitter"

    def test_empty_string_platform_twitter_only_defaults_to_twitter(self, app_client, test_sim_dir):
        """Empty string platform on Twitter-only simulation defaults to 'twitter'."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_empty_tw",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=2,
        )

        dummy_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=1002,
        )

        with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
             patch.object(SimulationRunner, "get_run_state", return_value=None), \
             patch.object(SimulationRunner, "start_simulation", return_value=dummy_run_state) as mock_start:

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id, "platform": ""})
            assert resp.status_code == 200
            assert mock_start.call_args[1]["platform"] == "twitter"

    def test_default_platform_reddit_only_defaults_to_reddit(self, app_client, test_sim_dir):
        """Omitted platform parameter on Reddit-only simulation defaults to 'reddit'."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_def_rd",
            enable_twitter=False,
            enable_reddit=True,
            reddit_profiles_count=2,
        )

        dummy_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=1003,
        )

        with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
             patch.object(SimulationRunner, "get_run_state", return_value=None), \
             patch.object(SimulationRunner, "start_simulation", return_value=dummy_run_state) as mock_start:

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id})
            assert resp.status_code == 200
            assert mock_start.call_args[1]["platform"] == "reddit"

    def test_default_platform_dual_platform_defaults_to_parallel(self, app_client, test_sim_dir):
        """Omitted platform parameter on dual-platform simulation defaults to 'parallel'."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_def_parallel",
            enable_twitter=True,
            enable_reddit=True,
            twitter_profiles_count=2,
            reddit_profiles_count=2,
        )

        dummy_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=1004,
        )

        with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
             patch.object(SimulationRunner, "get_run_state", return_value=None), \
             patch.object(SimulationRunner, "start_simulation", return_value=dummy_run_state) as mock_start:

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id})
            assert resp.status_code == 200
            assert mock_start.call_args[1]["platform"] == "parallel"

    def test_explicit_platform_honored_for_dual_platform(self, app_client, test_sim_dir):
        """Explicit platform on dual-platform simulation is honored."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_explicit_tw",
            enable_twitter=True,
            enable_reddit=True,
            twitter_profiles_count=2,
            reddit_profiles_count=2,
        )

        dummy_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=1005,
        )

        with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
             patch.object(SimulationRunner, "get_run_state", return_value=None), \
             patch.object(SimulationRunner, "start_simulation", return_value=dummy_run_state) as mock_start:

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id, "platform": "twitter"})
            assert resp.status_code == 200
            assert mock_start.call_args[1]["platform"] == "twitter"

    def test_explicit_platform_none_twitter_only_defaults_to_twitter(self, app_client, test_sim_dir):
        """Explicit platform=None on Twitter-only simulation defaults to 'twitter'."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_none_tw",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=2,
        )

        dummy_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=1006,
        )

        with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
             patch.object(SimulationRunner, "get_run_state", return_value=None), \
             patch.object(SimulationRunner, "start_simulation", return_value=dummy_run_state) as mock_start:

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id, "platform": None})
            assert resp.status_code == 200
            assert mock_start.call_args[1]["platform"] == "twitter"

    def test_explicit_platform_none_dual_platform_defaults_to_parallel(self, app_client, test_sim_dir):
        """Explicit platform=None on dual-platform simulation defaults to 'parallel'."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_none_dual",
            enable_twitter=True,
            enable_reddit=True,
            twitter_profiles_count=2,
            reddit_profiles_count=2,
        )

        dummy_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=1007,
        )

        with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
             patch.object(SimulationRunner, "get_run_state", return_value=None), \
             patch.object(SimulationRunner, "start_simulation", return_value=dummy_run_state) as mock_start:

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id, "platform": None})
            assert resp.status_code == 200
            assert mock_start.call_args[1]["platform"] == "parallel"

    def test_both_platforms_disabled_defaults_to_reddit(self, app_client, test_sim_dir):
        """When both platforms are disabled, omitted platform defaults to 'reddit'."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_both_disabled_start",
            enable_twitter=False,
            enable_reddit=False,
            twitter_profiles_count=0,
            reddit_profiles_count=0,
        )

        dummy_run_state = SimulationRunState(
            simulation_id=sim_id,
            runner_status=RunnerStatus.RUNNING,
            process_pid=1008,
        )

        with patch.object(SimulationRunner, "is_process_alive", return_value=False), \
             patch.object(SimulationRunner, "get_run_state", return_value=None), \
             patch.object(SimulationRunner, "start_simulation", return_value=dummy_run_state) as mock_start:

            resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id})
            assert resp.status_code == 200
            assert mock_start.call_args[1]["platform"] == "reddit"

    def test_invalid_platform_type_returns_400(self, app_client, test_sim_dir):
        """Unsupported platform string returns 400 error."""
        sim_id = _create_sim(
            test_sim_dir,
            "sim_invalid_platform",
            enable_twitter=True,
            enable_reddit=False,
            twitter_profiles_count=2,
        )

        resp = app_client.post("/api/simulation/start", json={"simulation_id": sim_id, "platform": "mastodon"})
        assert resp.status_code == 400
        assert resp.json["success"] is False
        assert "Invalid platform type" in resp.json["error"]
