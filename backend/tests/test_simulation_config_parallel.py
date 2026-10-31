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


def test_generate_config_single_entity():
    """Verify generate_config handles a single entity correctly with 1 batch."""
    entities = _create_sample_entities(count=1)

    with patch("app.services.simulation_config_generator.OpenAI"):
        gen = SimulationConfigGenerator(api_key="mock", model_name="mock")
        gen._call_llm_with_retry = MagicMock(return_value={
            "agent_configs": [
                {
                    "agent_id": 0,
                    "activity_level": 0.8,
                    "stance": "supportive",
                    "influence_weight": 2.0,
                }
            ],
            "hot_topics": ["single_topic"],
            "narrative_direction": "focused",
            "initial_posts": [{"content": "hello single", "poster_type": "Student"}],
            "total_simulation_hours": 24,
            "minutes_per_round": 60,
            "agents_per_hour_min": 1,
            "agents_per_hour_max": 1,
            "reasoning": "Single entity test",
        })

        progress_reports = []

        def on_progress(step, total, msg):
            progress_reports.append((step, total, msg))

        params = gen.generate_config(
            simulation_id="sim_single",
            project_id="proj_single",
            graph_id="graph_single",
            simulation_requirement="Single requirement",
            document_text="Single doc",
            entities=entities,
            progress_callback=on_progress,
            parallel_count=2,
        )

        assert len(params.agent_configs) == 1
        agent = params.agent_configs[0]
        assert agent.agent_id == 0
        assert agent.entity_uuid == "uuid_000"
        assert agent.entity_name == "Entity_000"
        assert agent.stance == "supportive"

        # total_steps = 3 + 1 batch = 4
        assert len(progress_reports) >= 4
        for step, total, _ in progress_reports:
            assert total == 4
        # Verify initial post assigned to agent 0
        assert len(params.event_config.initial_posts) == 1
        assert params.event_config.initial_posts[0]["poster_agent_id"] == 0


def test_generate_config_high_concurrency_deterministic_order():
    """Verify deterministic ordering under high concurrency (65 entities, 5 batches, 10 workers)."""
    entities = _create_sample_entities(count=65)  # 5 batches (15, 15, 15, 15, 5)

    def mock_call_llm(prompt, system_prompt):
        if "time configuration" in prompt:
            time.sleep(0.01)
            return {"reasoning": "Time ok"}
        if "event configuration" in prompt:
            time.sleep(0.02)
            return {"reasoning": "Event ok"}
        # Deliberately invert batch completion times: batch 0 delayed the most
        if "Entity_000" in prompt:
            time.sleep(0.05)
        elif "Entity_015" in prompt:
            time.sleep(0.03)
        elif "Entity_030" in prompt:
            time.sleep(0.02)
        elif "Entity_045" in prompt:
            time.sleep(0.01)
        else:
            time.sleep(0.001)
        return {"agent_configs": []}

    with patch("app.services.simulation_config_generator.OpenAI"):
        gen = SimulationConfigGenerator(api_key="mock", model_name="mock")
        gen._call_llm_with_retry = mock_call_llm

        params = gen.generate_config(
            simulation_id="sim_high_concurrency",
            project_id="proj_hc",
            graph_id="graph_hc",
            simulation_requirement="High concurrency test",
            document_text="High concurrency text",
            entities=entities,
            parallel_count=10,
        )

        assert len(params.agent_configs) == 65
        for i in range(65):
            agent = params.agent_configs[i]
            assert agent.agent_id == i
            assert agent.entity_uuid == f"uuid_{i:03d}"
            assert agent.entity_name == f"Entity_{i:03d}"


@pytest.mark.parametrize("invalid_parallel_count", [0, -5, None])
def test_generate_config_parallel_count_fallback(invalid_parallel_count):
    """Verify generate_config safely falls back to default/min workers on non-positive or None values."""
    entities = _create_sample_entities(count=5)

    with patch("app.services.simulation_config_generator.OpenAI"):
        gen = SimulationConfigGenerator(api_key="mock", model_name="mock")
        gen._generate_time_config = MagicMock(return_value={"reasoning": "ok"})
        gen._generate_event_config = MagicMock(return_value={"reasoning": "ok"})

        params = gen.generate_config(
            simulation_id="sim_fallback",
            project_id="proj_1",
            graph_id="graph_1",
            simulation_requirement="Fallback test",
            document_text="",
            entities=entities,
            parallel_count=invalid_parallel_count,
        )

        assert len(params.agent_configs) == 5
        assert params.simulation_id == "sim_fallback"


@pytest.mark.parametrize("bad_val", [0, -1, "invalid_str", None])
def test_api_prepare_simulation_invalid_parallel_count_types(tmp_path, monkeypatch, bad_val):
    """Verify API gracefully coerces invalid or non-positive parallel_profile_count to 5."""
    monkeypatch.setattr(Config, "OASIS_SIMULATION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path))

    sim_id = f"sim_api_bad_{bad_val}"
    sim_dir = tmp_path / sim_id
    sim_dir.mkdir(parents=True)
    state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_api_bad",
        graph_id="graph_api_bad",
        status=SimulationStatus.CREATED,
    )
    (sim_dir / "state.json").write_text(json.dumps(state.to_dict()), encoding="utf-8")

    mock_project = Project(
        project_id="proj_api_bad",
        name="Test API Bad",
        status=ProjectStatus.GRAPH_COMPLETED,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        simulation_requirement="Requirement",
    )
    monkeypatch.setattr(ProjectManager, "get_project", lambda pid: mock_project)
    monkeypatch.setattr(ProjectManager, "get_extracted_text", lambda pid: "Doc")

    captured_parallel_count = []

    def mock_prepare(self, *args, **kwargs):
        captured_parallel_count.append(kwargs.get("parallel_profile_count"))
        state.status = SimulationStatus.READY
        return state

    monkeypatch.setattr(SimulationManager, "prepare_simulation", mock_prepare)

    class MockEntityReader:
        def __init__(self, *args, **kwargs):
            pass

        def filter_defined_entities(self, **kwargs):
            return FilteredEntities(entities=[], entity_types=set(), total_count=0, filtered_count=0)

    from app.api import simulation as sim_api_mod
    monkeypatch.setattr(sim_api_mod, "EntityReader", MockEntityReader)

    class SyncThread:
        def __init__(self, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(threading, "Thread", SyncThread)

    app = create_app()
    app.config.update(TESTING=True)
    app.extensions["neo4j_storage"] = MagicMock()
    client = app.test_client()

    payload = {
        "simulation_id": sim_id,
        "force_regenerate": True,
    }
    if bad_val is not None:
        payload["parallel_profile_count"] = bad_val

    response = client.post("/api/simulation/prepare", json=payload)
    assert response.status_code == 200
    assert response.json["success"] is True
    assert captured_parallel_count == [5]


def test_generate_config_structural_exception_propagates():
    """Verify unhandled exception during concurrent structural step propagates without deadlock."""
    entities = _create_sample_entities(count=10)

    with patch("app.services.simulation_config_generator.OpenAI"):
        gen = SimulationConfigGenerator(api_key="mock", model_name="mock")
        gen._generate_time_config = MagicMock(side_effect=RuntimeError("Structural time failed"))
        gen._generate_event_config = MagicMock(return_value={"reasoning": "ok"})

        with pytest.raises(RuntimeError, match="Structural time failed"):
            gen.generate_config(
                simulation_id="sim_err",
                project_id="proj_err",
                graph_id="graph_err",
                simulation_requirement="Err",
                document_text="",
                entities=entities,
                parallel_count=2,
            )


def test_generate_config_batch_exception_propagates():
    """Verify unhandled exception during concurrent batch generation propagates cleanly."""
    entities = _create_sample_entities(count=30)  # 2 batches

    with patch("app.services.simulation_config_generator.OpenAI"):
        gen = SimulationConfigGenerator(api_key="mock", model_name="mock")
        gen._generate_time_config = MagicMock(return_value={"reasoning": "ok"})
        gen._generate_event_config = MagicMock(return_value={"reasoning": "ok"})

        def mock_batch(context, entities, start_idx, simulation_requirement):
            if start_idx == 0:
                raise RuntimeError("Batch 0 exploded")
            return []

        gen._generate_agent_configs_batch = mock_batch

        with pytest.raises(RuntimeError, match="Batch 0 exploded"):
            gen.generate_config(
                simulation_id="sim_batch_err",
                project_id="proj_batch_err",
                graph_id="graph_batch_err",
                simulation_requirement="Err",
                document_text="",
                entities=entities,
                parallel_count=2,
            )


def test_simulation_manager_offline_prepare_flow_complete(tmp_path, monkeypatch):
    """Verify full offline simulation preparation flow with real SimulationConfigGenerator and mock LLM calls."""
    sim_id = "sim_offline_full"
    sim_dir = tmp_path / sim_id
    sim_dir.mkdir(parents=True)

    state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_offline",
        graph_id="graph_offline",
        status=SimulationStatus.CREATED,
    )
    (sim_dir / "state.json").write_text(json.dumps(state.to_dict()), encoding="utf-8")

    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(Config, "LLM_API_KEY", "mock-key")
    monkeypatch.setattr(Config, "LLM_BASE_URL", "http://mock-llm")
    monkeypatch.setattr(Config, "LLM_MODEL_NAME", "mock-model")

    mock_project = Project(
        project_id="proj_offline",
        name="Offline Project",
        status=ProjectStatus.GRAPH_COMPLETED,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        simulation_requirement="Simulate offline discussions",
    )
    monkeypatch.setattr(ProjectManager, "get_project", lambda pid: mock_project)
    monkeypatch.setattr(ProjectManager, "get_extracted_text", lambda pid: "Full offline doc text")

    entities = _create_sample_entities(count=20)  # 2 batches: 15 and 5

    class MockEntityReader:
        def __init__(self, *args, **kwargs):
            pass

        def filter_defined_entities(self, **kwargs):
            return FilteredEntities(
                entities=entities,
                entity_types={"Student", "Professor", "MediaOutlet", "GovernmentAgency"},
                total_count=20,
                filtered_count=20,
            )

    from app.services import simulation_manager as sim_mgr_mod
    monkeypatch.setattr(sim_mgr_mod, "EntityReader", MockEntityReader)

    # Mock OasisProfileGenerator to return 20 profiles
    mock_profile_gen = MagicMock()
    mock_profile_gen.generate_profiles_from_entities.return_value = [
        {"user_id": i, "name": f"User_{i}"} for i in range(20)
    ]
    monkeypatch.setattr(sim_mgr_mod, "OasisProfileGenerator", lambda *args, **kwargs: mock_profile_gen)

    # Mock OpenAI client used by SimulationConfigGenerator
    def mock_llm_call(self, prompt, system_prompt):
        if "time simulation configuration" in prompt or "time configuration" in prompt:
            return {
                "total_simulation_hours": 48,
                "minutes_per_round": 60,
                "agents_per_hour_min": 2,
                "agents_per_hour_max": 8,
                "reasoning": "Offline time reasoning",
            }
        if "event configuration" in prompt:
            return {
                "hot_topics": ["offline_topic"],
                "narrative_direction": "calm",
                "initial_posts": [{"content": "Offline initial post", "poster_type": "Student"}],
                "reasoning": "Offline event reasoning",
            }
        return {"agent_configs": []}

    with patch.object(SimulationConfigGenerator, "_call_llm_with_retry", mock_llm_call), \
         patch("app.services.simulation_config_generator.OpenAI"):
        manager = SimulationManager()
        result_state = manager.prepare_simulation(
            simulation_id=sim_id,
            simulation_requirement="Offline requirement",
            document_text="Offline text",
            parallel_profile_count=3,
            storage=MagicMock(),
        )

        assert result_state.status == SimulationStatus.READY
        assert result_state.config_generated is True
        assert "Time config:" in result_state.config_reasoning
        assert "Event config:" in result_state.config_reasoning
        assert "Agent config:" in result_state.config_reasoning

        config_file = sim_dir / "simulation_config.json"
        assert config_file.exists()
        saved_config = json.loads(config_file.read_text(encoding="utf-8"))
        assert saved_config["simulation_id"] == sim_id
        assert len(saved_config["agent_configs"]) == 20
        assert saved_config["time_config"]["total_simulation_hours"] == 48
        assert saved_config["event_config"]["hot_topics"] == ["offline_topic"]
        assert len(saved_config["event_config"]["initial_posts"]) == 1
        assert saved_config["event_config"]["initial_posts"][0]["poster_agent_id"] == 0


def test_simulation_manager_prepare_error_recovery_persists_failed_state(tmp_path, monkeypatch):
    """Verify prepare_simulation records FAILED state to state.json when config generation errors out."""
    sim_id = "sim_fail_recovery"
    sim_dir = tmp_path / sim_id
    sim_dir.mkdir(parents=True)

    state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_fail",
        graph_id="graph_fail",
        status=SimulationStatus.CREATED,
    )
    (sim_dir / "state.json").write_text(json.dumps(state.to_dict()), encoding="utf-8")

    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path))

    mock_project = Project(
        project_id="proj_fail",
        name="Fail Project",
        status=ProjectStatus.GRAPH_COMPLETED,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        simulation_requirement="Fail requirement",
    )
    monkeypatch.setattr(ProjectManager, "get_project", lambda pid: mock_project)
    monkeypatch.setattr(ProjectManager, "get_extracted_text", lambda pid: "Doc")

    class MockEntityReader:
        def __init__(self, *args, **kwargs):
            pass

        def filter_defined_entities(self, **kwargs):
            return FilteredEntities(
                entities=_create_sample_entities(count=5),
                entity_types={"Student"},
                total_count=5,
                filtered_count=5,
            )

    from app.services import simulation_manager as sim_mgr_mod
    monkeypatch.setattr(sim_mgr_mod, "EntityReader", MockEntityReader)

    mock_profile_gen = MagicMock()
    mock_profile_gen.generate_profiles_from_entities.return_value = []
    monkeypatch.setattr(sim_mgr_mod, "OasisProfileGenerator", lambda *args, **kwargs: mock_profile_gen)

    mock_config_gen = MagicMock()
    mock_config_gen.generate_config.side_effect = RuntimeError("OpenAI rate limit exhausted")
    monkeypatch.setattr(sim_mgr_mod, "SimulationConfigGenerator", lambda *args, **kwargs: mock_config_gen)

    manager = SimulationManager()
    with pytest.raises(RuntimeError, match="OpenAI rate limit exhausted"):
        manager.prepare_simulation(
            simulation_id=sim_id,
            simulation_requirement="Fail req",
            document_text="Doc",
            storage=MagicMock(),
        )

    persisted = json.loads((sim_dir / "state.json").read_text(encoding="utf-8"))
    assert persisted["status"] == "failed"
    assert "OpenAI rate limit exhausted" in persisted["error"]


def test_simulation_manager_config_progress_callback_payload_assertions(tmp_path, monkeypatch):
    """Verify exact payload structure and percentage assertions of config progress reporting."""
    sim_id = "sim_progress_payload"
    sim_dir = tmp_path / sim_id
    sim_dir.mkdir(parents=True)

    state = SimulationState(
        simulation_id=sim_id,
        project_id="proj_prog",
        graph_id="graph_prog",
        status=SimulationStatus.CREATED,
    )
    (sim_dir / "state.json").write_text(json.dumps(state.to_dict()), encoding="utf-8")

    monkeypatch.setattr(SimulationManager, "SIMULATION_DATA_DIR", str(tmp_path))

    mock_project = Project(
        project_id="proj_prog",
        name="Prog Project",
        status=ProjectStatus.GRAPH_COMPLETED,
        created_at="2026-01-01T00:00:00",
        updated_at="2026-01-01T00:00:00",
        simulation_requirement="Prog req",
    )
    monkeypatch.setattr(ProjectManager, "get_project", lambda pid: mock_project)
    monkeypatch.setattr(ProjectManager, "get_extracted_text", lambda pid: "Doc")

    entities = _create_sample_entities(count=30)  # 2 batches

    class MockEntityReader:
        def __init__(self, *args, **kwargs):
            pass

        def filter_defined_entities(self, **kwargs):
            return FilteredEntities(
                entities=entities,
                entity_types={"Student"},
                total_count=30,
                filtered_count=30,
            )

    from app.services import simulation_manager as sim_mgr_mod
    monkeypatch.setattr(sim_mgr_mod, "EntityReader", MockEntityReader)

    mock_profile_gen = MagicMock()
    mock_profile_gen.generate_profiles_from_entities.return_value = []
    monkeypatch.setattr(sim_mgr_mod, "OasisProfileGenerator", lambda *args, **kwargs: mock_profile_gen)

    # Simulate SimulationConfigGenerator calling the progress callback
    def mock_generate_config(*args, **kwargs):
        cb = kwargs.get("progress_callback")
        assert cb is not None
        # Total steps for 30 entities = 3 + 2 = 5
        cb(1, 5, "Completed time configuration generation")
        cb(2, 5, "Completed event configuration and hot topics generation")
        cb(3, 5, "Generated agent configuration batch 1/2")
        cb(4, 5, "Generated agent configuration batch 2/2")
        cb(5, 5, "Generating platform configuration...")
        mock_params = MagicMock()
        mock_params.to_json.return_value = "{}"
        mock_params.generation_reasoning = "Ok"
        return mock_params

    mock_config_gen = MagicMock()
    mock_config_gen.generate_config.side_effect = mock_generate_config
    monkeypatch.setattr(sim_mgr_mod, "SimulationConfigGenerator", lambda *args, **kwargs: mock_config_gen)

    captured_callbacks = []

    def outer_progress_callback(stage, percentage, message, **kwargs):
        captured_callbacks.append({
            "stage": stage,
            "percentage": percentage,
            "message": message,
            "kwargs": kwargs,
        })

    manager = SimulationManager()
    manager.prepare_simulation(
        simulation_id=sim_id,
        simulation_requirement="Prog req",
        document_text="Doc",
        progress_callback=outer_progress_callback,
        storage=MagicMock(),
    )

    config_callbacks = [c for c in captured_callbacks if c["stage"] == "generating_config"]
    assert len(config_callbacks) >= 5

    # Check the callbacks generated by config_progress_cb
    dynamic_config_calls = [
        c for c in config_callbacks
        if c["kwargs"].get("total") == 5
    ]
    assert len(dynamic_config_calls) == 5

    for expected_step, call in enumerate(dynamic_config_calls, start=1):
        assert call["kwargs"]["current"] == expected_step
        assert call["kwargs"]["total"] == 5
        expected_pct = int(expected_step / 5 * 100)
        assert call["percentage"] == expected_pct
        assert call["kwargs"]["item_name"] == call["message"]

