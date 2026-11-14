"""Regression test for Issue #75: real-time simulation streaming via SSE."""

import json
from unittest.mock import patch, MagicMock

import pytest
from app import create_app
from app.services.simulation_runner import SimulationRunState, RunnerStatus, AgentAction


@pytest.fixture
def client():
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_simulation_stream_endpoint_returns_event_stream_and_closes_when_completed(client):
    """
    GET /api/simulation/<id>/stream should return text/event-stream,
    stream status updates and actions, and close gracefully upon terminal status.
    """
    sim_id = "test_sim_sse"

    state1 = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.RUNNING,
        current_round=1,
        total_rounds=10,
        recent_actions=[
            AgentAction(round_num=1, timestamp="2026-11-13T00:00:00", platform="twitter", agent_id=1, agent_name="Alice", action_type="POST")
        ]
    )

    state2 = SimulationRunState(
        simulation_id=sim_id,
        runner_status=RunnerStatus.COMPLETED,
        current_round=10,
        total_rounds=10,
        recent_actions=[
            AgentAction(round_num=1, timestamp="2026-11-13T00:00:00", platform="twitter", agent_id=1, agent_name="Alice", action_type="POST"),
            AgentAction(round_num=10, timestamp="2026-11-13T00:10:00", platform="twitter", agent_id=2, agent_name="Bob", action_type="LIKE")
        ]
    )

    states = [state1, state2]

    def mock_get_run_state(id_):
        if states:
            return states.pop(0)
        return state2

    with patch("app.services.simulation_runner.SimulationRunner.get_run_state", side_effect=mock_get_run_state), \
         patch("time.sleep", return_value=None):
        response = client.get(f"/api/simulation/{sim_id}/stream")
        assert response.status_code == 200
        assert "text/event-stream" in response.headers["Content-Type"]

        lines = response.data.decode("utf-8").split("\n")
        data_events = [line[6:] for line in lines if line.startswith("data: ")]
        assert len(data_events) >= 2

        first_event = json.loads(data_events[0])
        assert first_event["runner_status"] == "running"
        assert first_event["current_round"] == 1

        last_event = json.loads(data_events[-1])
        assert last_event["runner_status"] == "completed"
