import csv
import json

import pytest

from app.services.simulation_manager import SimulationManager, SimulationState


def _manager(tmp_path, *, twitter, reddit):
    manager = SimulationManager()
    manager.SIMULATION_DATA_DIR = str(tmp_path)
    state = SimulationState(
        simulation_id="sim_test",
        project_id="proj_test",
        graph_id="graph_test",
        enable_twitter=twitter,
        enable_reddit=reddit,
    )
    manager._save_simulation_state(state)
    return manager, tmp_path / "sim_test"


def test_twitter_default_reads_generated_csv(tmp_path):
    manager, sim_dir = _manager(tmp_path, twitter=True, reddit=False)
    with (sim_dir / "twitter_profiles.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["agent_id", "name"])
        writer.writeheader()
        writer.writerow({"agent_id": "0", "name": "Alice"})

    assert manager.get_profiles("sim_test") == [{"agent_id": "0", "name": "Alice"}]


def test_reddit_default_preserves_json_behavior(tmp_path):
    manager, sim_dir = _manager(tmp_path, twitter=False, reddit=True)
    profiles = [{"user_id": 0, "username": "alice"}]
    (sim_dir / "reddit_profiles.json").write_text(json.dumps(profiles), encoding="utf-8")

    assert manager.get_profiles("sim_test") == profiles


def test_explicit_platform_overrides_dual_platform_default(tmp_path):
    manager, sim_dir = _manager(tmp_path, twitter=True, reddit=True)
    with (sim_dir / "twitter_profiles.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["agent_id", "name"])
        writer.writeheader()
        writer.writerow({"agent_id": "1", "name": "Bob"})

    assert manager.get_profiles("sim_test", platform="twitter")[0]["name"] == "Bob"


def test_invalid_platform_is_rejected(tmp_path):
    manager, _ = _manager(tmp_path, twitter=True, reddit=True)

    with pytest.raises(ValueError, match="不支持的平台"):
        manager.get_profiles("sim_test", platform="../state")


def test_missing_profile_file_returns_empty_list(tmp_path):
    manager, _ = _manager(tmp_path, twitter=True, reddit=False)
    assert manager.get_profiles("sim_test") == []


def test_empty_csv_file_returns_empty_list(tmp_path):
    manager, sim_dir = _manager(tmp_path, twitter=True, reddit=False)
    (sim_dir / "twitter_profiles.csv").write_text("", encoding="utf-8")
    assert manager.get_profiles("sim_test") == []

    with (sim_dir / "twitter_profiles.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["agent_id", "name"])
        writer.writeheader()
    assert manager.get_profiles("sim_test") == []


def test_csv_with_utf8_and_special_characters(tmp_path):
    manager, sim_dir = _manager(tmp_path, twitter=True, reddit=False)
    with (sim_dir / "twitter_profiles.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["agent_id", "name", "bio"])
        writer.writeheader()
        writer.writerow({
            "agent_id": "42",
            "name": "张三 / Alice & Bob 🚀",
            "bio": 'Quote: "hello, world!", with comma',
        })

    profiles = manager.get_profiles("sim_test")
    assert len(profiles) == 1
    assert profiles[0]["agent_id"] == "42"
    assert profiles[0]["name"] == "张三 / Alice & Bob 🚀"
    assert profiles[0]["bio"] == 'Quote: "hello, world!", with comma'


@pytest.mark.parametrize("invalid_casing", ["Twitter", "TWITTER", "Reddit", "REDDIT"])
def test_platform_casing_is_rejected(tmp_path, invalid_casing):
    manager, _ = _manager(tmp_path, twitter=True, reddit=True)
    with pytest.raises(ValueError, match="不支持的平台"):
        manager.get_profiles("sim_test", platform=invalid_casing)
