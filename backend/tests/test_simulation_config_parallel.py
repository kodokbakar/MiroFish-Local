"""
Unit tests for parallel batch agent configuration and structural parameter generation
in SimulationConfigGenerator and integration with SimulationManager / API.
"""

import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app import create_app
from app.config import Config
from app.models.project import Project, ProjectManager, ProjectStatus
from app.services.entity_reader import EntityNode, FilteredEntities
from app.services.simulation_config_generator import SimulationConfigGenerator
from app.services.simulation_manager import SimulationManager, SimulationState, SimulationStatus


def _create_sample_entities(count: int = 35):
    """Create sample entities with alternating labels."""
    types = ["Student", "Professor", "MediaOutlet", "GovernmentAgency"]
    return [
        EntityNode(
            uuid=f"uuid_{i:03d}",
            name=f"Entity_{i:03d}",
            labels=[types[i % len(types)]],
            summary=f"Summary for entity {i}",
            attributes={"role": "tester", "index": i},
        )
        for i in range(count)
    ]


def test_generate_config_parallel_deterministic_order():
    """Verify parallel generation preserves deterministic agent ordering across batches."""
    entities = _create_sample_entities(count=45)  # 3 batches of 15

    def mock_call_llm(prompt, system_prompt):
        # Introduce variable sleep based on prompt to test out-of-order completion
        if "time configuration" in prompt:
            time.sleep(0.02)
            return {
                "total_simulation_hours": 48,
                "minutes_per_round": 60,
                "agents_per_hour_min": 2,
                "agents_per_hour_max": 10,
                "reasoning": "Fast time config",
            }
        if "event configuration" in prompt:
            time.sleep(0.01)
            return {
                "hot_topics": ["news"],
                "narrative_direction": "steady",
                "initial_posts": [{"content": "hello", "poster_type": "Student"}],
                "reasoning": "Fast event config",
            }
        # In batch agent configuration, delay earlier batches more to force reverse completion
        if "Entity_000" in prompt:
            time.sleep(0.04)  # batch 0 finishes last
        elif "Entity_015" in prompt:
            time.sleep(0.02)  # batch 1 finishes middle
        else:
            time.sleep(0.005)  # batch 2 finishes first
        return {"agent_configs": []}

    with patch("app.services.simulation_config_generator.OpenAI"):
        gen = SimulationConfigGenerator(api_key="mock", model_name="mock")
        gen._call_llm_with_retry = mock_call_llm

        progress_calls = []
        progress_lock = threading.Lock()

        def on_progress(step, total, msg):
            with progress_lock:
                progress_calls.append((step, total, msg))

        params = gen.generate_config(
            simulation_id="sim_test",
            project_id="proj_1",
            graph_id="graph_1",
            simulation_requirement="Test requirement",
            document_text="Test document",
            entities=entities,
            progress_callback=on_progress,
            parallel_count=5,
        )

        # 45 entities in total
        assert len(params.agent_configs) == 45
        # Verify strict ordering from 0 to 44
        for expected_id, agent in enumerate(params.agent_configs):
            assert agent.agent_id == expected_id
            assert agent.entity_uuid == f"uuid_{expected_id:03d}"
            assert agent.entity_name == f"Entity_{expected_id:03d}"

        # Verify progress callback was safely called with expected steps
        # total_steps = 3 (time, event, platform) + 3 batches = 6
        assert len(progress_calls) >= 6
        step_numbers = [call[0] for call in progress_calls]
        assert 1 in step_numbers
        assert 2 in step_numbers
        assert 6 in step_numbers
        for step, total, _ in progress_calls:
            assert total == 6
            assert 1 <= step <= 6


def test_generate_config_zero_entities():
    """Verify parallel generate_config handles 0 entities gracefully."""
    with patch("app.services.simulation_config_generator.OpenAI"):
        gen = SimulationConfigGenerator(api_key="mock", model_name="mock")
        gen._generate_time_config = MagicMock(return_value={"reasoning": "ok"})
        gen._generate_event_config = MagicMock(return_value={"reasoning": "ok"})

        params = gen.generate_config(
            simulation_id="sim_zero",
            project_id="proj_1",
            graph_id="graph_1",
            simulation_requirement="Test",
            document_text="",
            entities=[],
            parallel_count=3,
        )

        assert len(params.agent_configs) == 0
        assert params.simulation_id == "sim_zero"


def test_generate_config_progress_callback_exception_handling():
    """Verify progress callback exceptions do not fail configuration generation."""
    entities = _create_sample_entities(count=5)

    def failing_progress(step, total, msg):
        raise RuntimeError("Callback explosion")

    with patch("app.services.simulation_config_generator.OpenAI"):
        gen = SimulationConfigGenerator(api_key="mock", model_name="mock")
        gen._generate_time_config = MagicMock(return_value={"reasoning": "ok"})
        gen._generate_event_config = MagicMock(return_value={"reasoning": "ok"})

        params = gen.generate_config(
            simulation_id="sim_safe",
            project_id="proj_1",
            graph_id="graph_1",
            simulation_requirement="Test",
            document_text="",
            entities=entities,
            progress_callback=failing_progress,
            parallel_count=2,
        )

        assert len(params.agent_configs) == 5


def test_simulation_manager_forwards_parallel_count(tmp_path, monkeypatch):
    """Verify prepare_simulation passes parallel_count=parallel_profile_count to generate_config."""
    sim_id = "sim_forward_test"
    sim_dir = tmp_path / sim_id
    sim_dir.mkdir(parents=True)

    state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_forward",
        graph_id="graph_forward",
        status=SimulationStatus.CREATED,
    )
    (sim_dir / "state.json").write_text(json.dumps(state.to_dict()), encoding="utf-8")

    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path))

    # Mock ProjectManager
    mock_project = Project(
        project_id="proj_forward",
        name="Test Project",
        status=ProjectStatus.GRAPH_COMPLETED,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        simulation_requirement="Requirement",
    )
    monkeypatch.setattr(ProjectManager, "get_project", lambda pid: mock_project)
    monkeypatch.setattr(ProjectManager, "get_extracted_text", lambda pid: "Sample doc")

    # Mock EntityReader
    class MockEntityReader:
        def __init__(self, *args, **kwargs):
            pass

        def filter_defined_entities(self, **kwargs):
            return FilteredEntities(
                entities=_create_sample_entities(count=4),
                entity_types={"Student"},
                total_count=4,
                filtered_count=4,
            )

    from app.services import simulation_manager as sim_mgr_mod
    monkeypatch.setattr(sim_mgr_mod, "EntityReader", MockEntityReader)

    # Mock OasisProfileGenerator
    mock_profile_gen = MagicMock()
    mock_profile_gen.generate_profiles_from_entities.return_value = []
    monkeypatch.setattr(sim_mgr_mod, "OasisProfileGenerator", lambda *args, **kwargs: mock_profile_gen)

    # Mock SimulationConfigGenerator
    mock_config_gen = MagicMock()
    mock_params = MagicMock()
    mock_params.to_json.return_value = "{}"
    mock_params.generation_reasoning = "Test reasoning"
    mock_config_gen.generate_config.return_value = mock_params
    monkeypatch.setattr(sim_mgr_mod, "SimulationConfigGenerator", lambda *args, **kwargs: mock_config_gen)

    manager = SimulationManager()
    mock_storage = MagicMock()

    manager.prepare_simulation(
        simulation_id=sim_id,
        simulation_requirement="Requirement",
        document_text="Sample doc",
        parallel_profile_count=7,
        storage=mock_storage,
    )

    # Verify generate_config was called with parallel_count=7
    assert mock_config_gen.generate_config.called
    kwargs = mock_config_gen.generate_config.call_args.kwargs
    assert kwargs.get("parallel_count") == 7


def test_api_prepare_simulation_forwards_parallel_profile_count(tmp_path, monkeypatch):
    """Verify /api/simulation/prepare extracts and forwards parallel_profile_count."""
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path))

    sim_id = "sim_api_test"
    sim_dir = tmp_path / sim_id
    sim_dir.mkdir(parents=True)
    state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_api",
        graph_id="graph_api",
        status=SimulationStatus.CREATED,
    )
    (sim_dir / "state.json").write_text(json.dumps(state.to_dict()), encoding="utf-8")

    # Mock ProjectManager
    mock_project = Project(
        project_id="proj_api",
        name="Test API",
        status=ProjectStatus.GRAPH_COMPLETED,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        simulation_requirement="Test requirement",
    )
    monkeypatch.setattr(ProjectManager, "get_project", lambda pid: mock_project)
    monkeypatch.setattr(ProjectManager, "get_extracted_text", lambda pid: "Doc")

    captured_parallel_count = []

    def mock_prepare(self, *args, **kwargs):
        captured_parallel_count.append(kwargs.get("parallel_profile_count"))
        state.status = SimulationStatus.READY
        return state

    monkeypatch.setattr(SimulationManager, "prepare_simulation", mock_prepare)

    # Mock EntityReader for preview
    class MockEntityReader:
        def __init__(self, *args, **kwargs):
            pass

        def filter_defined_entities(self, **kwargs):
            return FilteredEntities(entities=[], entity_types=set(), total_count=0, filtered_count=0)

    from app.api import simulation as sim_api_mod
    monkeypatch.setattr(sim_api_mod, "EntityReader", MockEntityReader)

    # Mock threading.Thread to run synchronously for test
    class SyncThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    import threading
    monkeypatch.setattr(threading, "Thread", SyncThread)

    app = create_app()
    app.config.update(TESTING=True)
    mock_storage = MagicMock()
    app.extensions["neo4j_storage"] = mock_storage
    client = app.test_client()

    response = client.post(
        "/api/simulation/prepare",
        json={
            "simulation_id": sim_id,
            "force_regenerate": True,
            "parallel_profile_count": 8,
        },
    )

    assert response.status_code == 200
    assert response.json["success"] is True
    assert captured_parallel_count == [8]
