"""
Tests for dynamic platform resolution in simulations (port 006).

Covers:
- SimulationState.get_default_platform()
- SimulationManager.get_profiles()
- _get_default_platform() helper function
- API endpoints:
  - GET /api/simulation/<id>/profiles
  - GET /api/simulation/<id>/profiles/realtime
  - GET /api/simulation/<id>/posts
  - GET /api/simulation/<id>/comments
"""

import csv
import json
import os
import shutil
import sqlite3
import uuid
from unittest.mock import patch

import pytest

from app import create_app
from app.api.simulation import _get_default_platform
from app.services.simulation_manager import SimulationManager, SimulationState, SimulationStatus


# ============================================================================
# 1. Unit tests for SimulationState.get_default_platform()
# ============================================================================

class TestSimulationStateDefaultPlatform:
    """Tests for SimulationState.get_default_platform()."""

    def test_both_platforms_enabled_returns_reddit(self):
        """When both Twitter and Reddit are enabled, default platform is reddit."""
        state = SimulationState(
            simulation_id="sim_both",
            project_id="proj_1",
            graph_id="graph_1",
            enable_twitter=True,
            enable_reddit=True,
        )
        assert state.get_default_platform() == "reddit"

    def test_twitter_only_enabled_returns_twitter(self):
        """When Twitter is enabled and Reddit is disabled, default platform is twitter."""
        state = SimulationState(
            simulation_id="sim_twitter",
            project_id="proj_1",
            graph_id="graph_1",
            enable_twitter=True,
            enable_reddit=False,
        )
        assert state.get_default_platform() == "twitter"

    def test_reddit_only_enabled_returns_reddit(self):
        """When Twitter is disabled and Reddit is enabled, default platform is reddit."""
        state = SimulationState(
            simulation_id="sim_reddit",
            project_id="proj_1",
            graph_id="graph_1",
            enable_twitter=False,
            enable_reddit=True,
        )
        assert state.get_default_platform() == "reddit"

    def test_neither_platform_enabled_returns_reddit(self):
        """When neither Twitter nor Reddit is enabled, fallback is reddit."""
        state = SimulationState(
            simulation_id="sim_none",
            project_id="proj_1",
            graph_id="graph_1",
            enable_twitter=False,
            enable_reddit=False,
        )
        assert state.get_default_platform() == "reddit"


# ============================================================================
# 2. Unit tests for SimulationManager.get_profiles()
# ============================================================================

class TestSimulationManagerGetProfiles:
    """Tests for SimulationManager.get_profiles()."""

    @pytest.fixture
    def manager_and_dir(self, tmp_path, monkeypatch):
        """Create SimulationManager configured to use temporary storage directory."""
        manager = SimulationManager()
        monkeypatch.setattr(manager, "SIMULATION_DATA_DIR", str(tmp_path))
        return manager, tmp_path

    def test_loads_twitter_profiles_when_twitter_only_and_platform_none(self, manager_and_dir):
        """When platform=None on Twitter-only simulation, loads twitter_profiles.json."""
        manager, base_dir = manager_and_dir
        sim_id = "sim_twitter_only"

        state = SimulationState(
            simulation_id=sim_id,
            project_id="proj_1",
            graph_id="graph_1",
            enable_twitter=True,
            enable_reddit=False,
        )
        manager._save_simulation_state(state)

        twitter_profiles = [{"id": "t1", "username": "alice", "platform": "twitter"}]
        reddit_profiles = [{"id": "r1", "username": "bob", "platform": "reddit"}]

        sim_dir = base_dir / sim_id
        with open(sim_dir / "twitter_profiles.json", "w", encoding="utf-8") as f:
            json.dump(twitter_profiles, f)
        with open(sim_dir / "reddit_profiles.json", "w", encoding="utf-8") as f:
            json.dump(reddit_profiles, f)

        # platform=None should dynamically resolve to twitter
        profiles = manager.get_profiles(sim_id, platform=None)
        assert profiles == twitter_profiles

    def test_loads_reddit_profiles_when_reddit_only_and_platform_none(self, manager_and_dir):
        """When platform=None on Reddit-only simulation, loads reddit_profiles.json."""
        manager, base_dir = manager_and_dir
        sim_id = "sim_reddit_only"

        state = SimulationState(
            simulation_id=sim_id,
            project_id="proj_1",
            graph_id="graph_1",
            enable_twitter=False,
            enable_reddit=True,
        )
        manager._save_simulation_state(state)

        twitter_profiles = [{"id": "t1", "username": "alice", "platform": "twitter"}]
        reddit_profiles = [{"id": "r1", "username": "bob", "platform": "reddit"}]

        sim_dir = base_dir / sim_id
        with open(sim_dir / "twitter_profiles.json", "w", encoding="utf-8") as f:
            json.dump(twitter_profiles, f)
        with open(sim_dir / "reddit_profiles.json", "w", encoding="utf-8") as f:
            json.dump(reddit_profiles, f)

        # platform=None should dynamically resolve to reddit
        profiles = manager.get_profiles(sim_id, platform=None)
        assert profiles == reddit_profiles

    def test_explicit_platform_override_works(self, manager_and_dir):
        """Explicit platform override still works regardless of default platform."""
        manager, base_dir = manager_and_dir
        sim_id = "sim_override"

        # Twitter-only simulation
        state = SimulationState(
            simulation_id=sim_id,
            project_id="proj_1",
            graph_id="graph_1",
            enable_twitter=True,
            enable_reddit=False,
        )
        manager._save_simulation_state(state)

        twitter_profiles = [{"id": "t1", "username": "alice"}]
        reddit_profiles = [{"id": "r1", "username": "bob"}]

        sim_dir = base_dir / sim_id
        with open(sim_dir / "twitter_profiles.json", "w", encoding="utf-8") as f:
            json.dump(twitter_profiles, f)
        with open(sim_dir / "reddit_profiles.json", "w", encoding="utf-8") as f:
            json.dump(reddit_profiles, f)

        # Explicitly request reddit even though simulation is twitter-only
        assert manager.get_profiles(sim_id, platform="reddit") == reddit_profiles
        # Explicitly request twitter
        assert manager.get_profiles(sim_id, platform="twitter") == twitter_profiles


# ============================================================================
# 3. Helper test for _get_default_platform(simulation_id)
# ============================================================================

class TestGetDefaultPlatformHelper:
    """Tests for _get_default_platform helper function."""

    @pytest.fixture
    def test_uploads_dir(self):
        """Ensure test simulation cleanup under uploads/simulations."""
        created_sim_ids = []
        base_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../uploads/simulations")
        )

        def _create_sim(enable_twitter: bool, enable_reddit: bool, corrupt: bool = False):
            sim_id = f"test_sim_{uuid.uuid4().hex[:8]}"
            sim_dir = os.path.join(base_dir, sim_id)
            os.makedirs(sim_dir, exist_ok=True)
            created_sim_ids.append(sim_id)

            state_file = os.path.join(sim_dir, "state.json")
            if corrupt:
                with open(state_file, "w", encoding="utf-8") as f:
                    f.write("{invalid_json: true")
            else:
                state = SimulationState(
                    simulation_id=sim_id,
                    project_id="proj_helper",
                    graph_id="graph_helper",
                    enable_twitter=enable_twitter,
                    enable_reddit=enable_reddit,
                )
                with open(state_file, "w", encoding="utf-8") as f:
                    json.dump(state.to_dict(), f, indent=2)
            return sim_id

        yield _create_sim

        for sim_id in created_sim_ids:
            sim_dir = os.path.join(base_dir, sim_id)
            if os.path.exists(sim_dir):
                shutil.rmtree(sim_dir, ignore_errors=True)

    def test_resolves_twitter_for_twitter_only_simulation(self, test_uploads_dir):
        """_get_default_platform resolves 'twitter' for Twitter-only simulation."""
        sim_id = test_uploads_dir(enable_twitter=True, enable_reddit=False)
        assert _get_default_platform(sim_id) == "twitter"

    def test_resolves_reddit_for_reddit_only_simulation(self, test_uploads_dir):
        """_get_default_platform resolves 'reddit' for Reddit-only simulation."""
        sim_id = test_uploads_dir(enable_twitter=False, enable_reddit=True)
        assert _get_default_platform(sim_id) == "reddit"

    def test_resolves_reddit_for_dual_simulation(self, test_uploads_dir):
        """_get_default_platform resolves 'reddit' when both Twitter and Reddit are enabled."""
        sim_id = test_uploads_dir(enable_twitter=True, enable_reddit=True)
        assert _get_default_platform(sim_id) == "reddit"

    def test_falls_back_to_reddit_for_non_existent_simulation(self):
        """_get_default_platform falls back safely to 'reddit' for non-existent simulation."""
        sim_id = f"sim_non_existent_{uuid.uuid4().hex[:8]}"
        base_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../uploads/simulations")
        )
        sim_dir = os.path.join(base_dir, sim_id)
        try:
            assert _get_default_platform(sim_id) == "reddit"
        finally:
            if os.path.exists(sim_dir):
                shutil.rmtree(sim_dir, ignore_errors=True)
    def test_falls_back_to_reddit_for_corrupted_state(self, test_uploads_dir):
        """_get_default_platform falls back safely to 'reddit' when state file is corrupted."""
        sim_id = test_uploads_dir(enable_twitter=True, enable_reddit=False, corrupt=True)
        assert _get_default_platform(sim_id) == "reddit"

    def test_falls_back_to_reddit_on_exception(self):
        """_get_default_platform falls back safely to 'reddit' if loader raises unexpected exception."""
        with patch.object(SimulationManager, "_load_simulation_state", side_effect=RuntimeError("disk error")):
            assert _get_default_platform("sim_any") == "reddit"


# ============================================================================
# 4. API endpoint tests using Flask test client
# ============================================================================

class TestSimulationApiPlatformEndpoints:
    """Tests for simulation API endpoints dynamically resolving platform."""

    @pytest.fixture
    def app_client(self):
        """Create Flask test client with mocked Neo4jStorage."""
        with patch("app.storage.Neo4jStorage"):
            app = create_app()
        app.config["TESTING"] = True
        return app.test_client()

    @pytest.fixture
    def populated_sim(self):
        """
        Create a fully populated simulation directory with state, profile files,
        and SQLite databases for both Twitter and Reddit.
        """
        sim_dirs = []
        base_dir = os.path.abspath(
            os.path.join(os.path.dirname(__file__), "../uploads/simulations")
        )

        def _create(enable_twitter: bool, enable_reddit: bool):
            sim_id = f"test_sim_{uuid.uuid4().hex[:8]}"
            sim_dir = os.path.join(base_dir, sim_id)
            os.makedirs(sim_dir, exist_ok=True)
            sim_dirs.append(sim_dir)

            # 1. State
            state = SimulationState(
                simulation_id=sim_id,
                project_id="proj_api_test",
                graph_id="graph_api_test",
                enable_twitter=enable_twitter,
                enable_reddit=enable_reddit,
                status=SimulationStatus.READY,
                entities_count=10,
            )
            with open(os.path.join(sim_dir, "state.json"), "w", encoding="utf-8") as f:
                json.dump(state.to_dict(), f, indent=2)

            # 2. Profiles for SimulationManager.get_profiles
            tw_profiles = [{"id": "tw_agent_1", "name": "Twitter User"}]
            rd_profiles = [{"id": "rd_agent_1", "name": "Reddit User"}]
            with open(os.path.join(sim_dir, "twitter_profiles.json"), "w", encoding="utf-8") as f:
                json.dump(tw_profiles, f)
            with open(os.path.join(sim_dir, "reddit_profiles.json"), "w", encoding="utf-8") as f:
                json.dump(rd_profiles, f)

            # 3. Profiles for realtime endpoint (twitter_profiles.csv / reddit_profiles.json)
            with open(os.path.join(sim_dir, "twitter_profiles.csv"), "w", encoding="utf-8", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=["id", "name"])
                writer.writeheader()
                writer.writerow({"id": "tw_csv_1", "name": "Twitter Realtime User"})

            # 4. SQLite databases for posts and comments
            for platform, post_content, comment_content in [
                ("twitter", "Post from Twitter", "Comment from Twitter"),
                ("reddit", "Post from Reddit", "Comment from Reddit"),
            ]:
                db_path = os.path.join(sim_dir, f"{platform}_simulation.db")
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                cursor.execute(
                    "CREATE TABLE post (id TEXT PRIMARY KEY, content TEXT, created_at TEXT)"
                )
                cursor.execute(
                    "INSERT INTO post VALUES (?, ?, ?)",
                    (f"{platform}_post_1", post_content, "2026-10-23T12:00:00"),
                )
                cursor.execute(
                    "CREATE TABLE comment (id TEXT PRIMARY KEY, post_id TEXT, content TEXT, created_at TEXT)"
                )
                cursor.execute(
                    "INSERT INTO comment VALUES (?, ?, ?, ?)",
                    (f"{platform}_comment_1", f"{platform}_post_1", comment_content, "2026-10-23T12:05:00"),
                )
                conn.commit()
                conn.close()

            return sim_id

        yield _create

        for sim_dir in sim_dirs:
            if os.path.exists(sim_dir):
                shutil.rmtree(sim_dir, ignore_errors=True)

    def test_get_profiles_endpoint_uses_dynamic_default(self, app_client, populated_sim):
        """GET /api/simulation/<id>/profiles without platform param uses dynamic default platform."""
        tw_sim_id = populated_sim(enable_twitter=True, enable_reddit=False)
        rd_sim_id = populated_sim(enable_twitter=False, enable_reddit=True)

        # Twitter-only without platform param -> defaults to twitter
        tw_resp = app_client.get(f"/api/simulation/{tw_sim_id}/profiles")
        assert tw_resp.status_code == 200
        tw_data = tw_resp.get_json()
        assert tw_data["success"] is True
        assert tw_data["data"]["platform"] == "twitter"
        assert tw_data["data"]["profiles"][0]["name"] == "Twitter User"

        # Reddit-only without platform param -> defaults to reddit
        rd_resp = app_client.get(f"/api/simulation/{rd_sim_id}/profiles")
        assert rd_resp.status_code == 200
        rd_data = rd_resp.get_json()
        assert rd_data["success"] is True
        assert rd_data["data"]["platform"] == "reddit"
        assert rd_data["data"]["profiles"][0]["name"] == "Reddit User"

    def test_get_profiles_realtime_endpoint_uses_dynamic_default(self, app_client, populated_sim):
        """GET /api/simulation/<id>/profiles/realtime without platform param uses dynamic default platform."""
        tw_sim_id = populated_sim(enable_twitter=True, enable_reddit=False)
        rd_sim_id = populated_sim(enable_twitter=False, enable_reddit=True)

        # Twitter-only without platform param -> loads twitter_profiles.csv
        tw_resp = app_client.get(f"/api/simulation/{tw_sim_id}/profiles/realtime")
        assert tw_resp.status_code == 200
        tw_data = tw_resp.get_json()
        assert tw_data["success"] is True
        assert tw_data["data"]["platform"] == "twitter"
        assert tw_data["data"]["file_exists"] is True
        assert tw_data["data"]["profiles"][0]["name"] == "Twitter Realtime User"

        # Reddit-only without platform param -> loads reddit_profiles.json
        rd_resp = app_client.get(f"/api/simulation/{rd_sim_id}/profiles/realtime")
        assert rd_resp.status_code == 200
        rd_data = rd_resp.get_json()
        assert rd_data["success"] is True
        assert rd_data["data"]["platform"] == "reddit"
        assert rd_data["data"]["file_exists"] is True
        assert rd_data["data"]["profiles"][0]["name"] == "Reddit User"

    def test_get_posts_endpoint_uses_dynamic_default_db(self, app_client, populated_sim):
        """GET /api/simulation/<id>/posts without platform param queries {default_platform}_simulation.db."""
        tw_sim_id = populated_sim(enable_twitter=True, enable_reddit=False)
        rd_sim_id = populated_sim(enable_twitter=False, enable_reddit=True)

        # Twitter-only without platform param -> queries twitter_simulation.db
        tw_resp = app_client.get(f"/api/simulation/{tw_sim_id}/posts")
        assert tw_resp.status_code == 200
        tw_data = tw_resp.get_json()
        assert tw_data["success"] is True
        assert tw_data["data"]["platform"] == "twitter"
        assert tw_data["data"]["count"] == 1
        assert tw_data["data"]["posts"][0]["content"] == "Post from Twitter"

        # Reddit-only without platform param -> queries reddit_simulation.db
        rd_resp = app_client.get(f"/api/simulation/{rd_sim_id}/posts")
        assert rd_resp.status_code == 200
        rd_data = rd_resp.get_json()
        assert rd_data["success"] is True
        assert rd_data["data"]["platform"] == "reddit"
        assert rd_data["data"]["count"] == 1
        assert rd_data["data"]["posts"][0]["content"] == "Post from Reddit"

    def test_get_comments_endpoint_uses_dynamic_default_db(self, app_client, populated_sim):
        """GET /api/simulation/<id>/comments without platform param queries {default_platform}_simulation.db."""
        tw_sim_id = populated_sim(enable_twitter=True, enable_reddit=False)
        rd_sim_id = populated_sim(enable_twitter=False, enable_reddit=True)

        # Twitter-only without platform param -> queries twitter_simulation.db
        tw_resp = app_client.get(f"/api/simulation/{tw_sim_id}/comments")
        assert tw_resp.status_code == 200
        tw_data = tw_resp.get_json()
        assert tw_data["success"] is True
        assert tw_data["data"]["count"] == 1
        assert tw_data["data"]["comments"][0]["content"] == "Comment from Twitter"

        # Reddit-only without platform param -> queries reddit_simulation.db
        rd_resp = app_client.get(f"/api/simulation/{rd_sim_id}/comments")
        assert rd_resp.status_code == 200
        rd_data = rd_resp.get_json()
        assert rd_data["success"] is True
        assert rd_data["data"]["count"] == 1
        assert rd_data["data"]["comments"][0]["content"] == "Comment from Reddit"
