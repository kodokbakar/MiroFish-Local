"""Tests for Issue #84 (Item 063): Temporal graph evolution tracking across simulation rounds."""

from unittest.mock import MagicMock, patch

from flask import Flask

from app.api.simulation import simulation_bp
from app.services.graph_memory_updater import AgentActivity, GraphMemoryUpdater
from app.storage.neo4j_records import edge_to_dict, node_to_dict
from app.storage.neo4j_storage import Neo4jStorage
from tests.test_neo4j_storage_ingestion import InMemoryNeo4jDriver


def test_records_include_round_and_simulation_id():
    """node_to_dict and edge_to_dict return created_round (default 0) and simulation_id."""
    # Node with round and sim
    node_props = {
        "uuid": "u1",
        "name": "Alice",
        "created_round": 2,
        "simulation_id": "sim_123",
        "created_at": "2026-11-14T00:00:00Z",
    }
    nd = node_to_dict(node_props, ["Entity", "Person"])
    assert nd["uuid"] == "u1"
    assert nd["name"] == "Alice"
    assert nd["created_round"] == 2
    assert nd["simulation_id"] == "sim_123"

    # Node default round 0
    nd_default = node_to_dict({"uuid": "u2", "name": "Bob"}, ["Entity"])
    assert nd_default["created_round"] == 0
    assert nd_default["simulation_id"] is None

    # Edge with round and sim
    edge_props = {
        "uuid": "e1",
        "name": "LIKED",
        "fact": "Alice liked Bob",
        "created_round": 3,
        "simulation_id": "sim_123",
    }
    ed = edge_to_dict(edge_props, "u1", "u2")
    assert ed["uuid"] == "e1"
    assert ed["created_round"] == 3
    assert ed["simulation_id"] == "sim_123"

    # Edge default round 0
    ed_default = edge_to_dict({"uuid": "e2", "name": "FOLLOWS"}, "u1", "u2")
    assert ed_default["created_round"] == 0
    assert ed_default["simulation_id"] is None


def test_graph_memory_updater_tracks_round_and_simulation():
    """GraphMemoryUpdater passes round_num and simulation_id to storage."""
    mock_storage = MagicMock()
    updater = GraphMemoryUpdater(graph_id="g1", storage=mock_storage, simulation_id="sim_abc")
    assert updater.simulation_id == "sim_abc"

    # add_activity_from_dict sets round_num and simulation_id
    updater.add_activity_from_dict({
        "agent_name": "Alice",
        "action_type": "LIKE_POST",
        "round": 4,
        "action_args": {"post_author_name": "Bob"}
    }, "twitter")

    act = updater._activity_queue.get_nowait()
    assert act.agent_name == "Alice"
    assert act.round_num == 4
    assert act.simulation_id == "sim_abc"

    # _send_batch_activities forwards simulation_id and round_num to persist_structured_activities
    updater._send_batch_activities([act], "twitter")
    mock_storage.persist_structured_activities.assert_called_once_with(
        "g1",
        [act],
        simulation_id="sim_abc",
        round_num=4,
    )


def test_neo4j_storage_persist_structured_activities_tags_round():
    """Neo4jStorage sets simulation_id and created_round in Cypher MERGE queries."""
    driver = InMemoryNeo4jDriver()
    with patch("app.storage.neo4j_storage.GraphDatabase.driver", return_value=driver):
        storage = Neo4jStorage(
            uri="bolt://localhost:7687",
            user="neo4j",
            password="password",
            embedding_service=MagicMock(),
            ner_extractor=MagicMock(),
        )

    activities = [
        AgentActivity(
            platform="twitter",
            agent_id=1,
            agent_name="Alice",
            action_type="LIKE_POST",
            action_args={"post_author_name": "Bob"},
            round_num=2,
            timestamp="2026-11-14T00:00:00Z",
            simulation_id="sim_456",
        )
    ]

    ep = storage.persist_structured_activities("g_test", activities)
    assert ep

    # Inspect query params passed to UNWIND $entities and UNWIND $relations
    entity_calls = [kwargs for q, kwargs in driver.queries if "UNWIND $entities AS e" in q]
    relation_calls = [kwargs for q, kwargs in driver.queries if "UNWIND $relations AS r" in q]

    assert len(entity_calls) == 1
    assert entity_calls[0]["simulation_id"] == "sim_456"
    assert entity_calls[0]["round_num"] == 2

    assert len(relation_calls) == 1
    assert relation_calls[0]["simulation_id"] == "sim_456"
    assert relation_calls[0]["round_num"] == 2


def test_get_graph_timeline_cumulative_aggregation():
    """get_graph_timeline aggregates nodes and edges by round and calculates cumulative deltas."""
    storage = Neo4jStorage.__new__(Neo4jStorage)
    mock_driver = MagicMock()
    mock_session = MagicMock()
    mock_driver.session.return_value.__enter__.return_value = mock_session
    storage._driver = mock_driver
    storage._call_with_retry = lambda fn, *args, **kwargs: fn(*args, **kwargs)
    storage._node_to_dict = Neo4jStorage._node_to_dict
    storage._edge_to_dict = Neo4jStorage._edge_to_dict

    def fake_execute_read(tx_fn):
        mock_tx = MagicMock()
        # Mock tx.run responses
        def mock_run(query, **kwargs):
            if "RETURN n, labels(n)" in query:
                return [
                    {
                        "n": {"uuid": "n0", "name": "BaseDoc", "created_round": 0, "simulation_id": None},
                        "labels": ["Entity", "Document"]
                    },
                    {
                        "n": {"uuid": "n1", "name": "Alice", "created_round": 1, "simulation_id": "sim_test"},
                        "labels": ["Entity", "Person"]
                    },
                    {
                        "n": {"uuid": "n2", "name": "Bob", "created_round": 2, "simulation_id": "sim_test"},
                        "labels": ["Entity", "Person"]
                    },
                ]
            else:
                return [
                    {
                        "r": {"uuid": "e1", "name": "POSTED", "created_round": 1, "simulation_id": "sim_test"},
                        "src_uuid": "n1",
                        "tgt_uuid": "n0",
                        "src_name": "Alice",
                        "tgt_name": "BaseDoc",
                    },
                    {
                        "r": {"uuid": "e2", "name": "LIKED", "created_round": 2, "simulation_id": "sim_test"},
                        "src_uuid": "n2",
                        "tgt_uuid": "n1",
                        "src_name": "Bob",
                        "tgt_name": "Alice",
                    },
                ]
        mock_tx.run = mock_run
        return tx_fn(mock_tx)

    mock_session.execute_read = fake_execute_read

    timeline = storage.get_graph_timeline("sim_test", graph_id="g_test")
    assert timeline["simulation_id"] == "sim_test"
    rounds = timeline["rounds"]
    assert len(rounds) == 3  # round 0, round 1, round 2

    # Round 0 (baseline)
    assert rounds[0]["round"] == 0
    assert len(rounds[0]["nodes_added"]) == 1
    assert len(rounds[0]["edges_added"]) == 0
    assert rounds[0]["cumulative_nodes"] == 1
    assert rounds[0]["cumulative_edges"] == 0

    # Round 1
    assert rounds[1]["round"] == 1
    assert len(rounds[1]["nodes_added"]) == 1
    assert len(rounds[1]["edges_added"]) == 1
    assert rounds[1]["cumulative_nodes"] == 2
    assert rounds[1]["cumulative_edges"] == 1

    # Round 2
    assert rounds[2]["round"] == 2
    assert len(rounds[2]["nodes_added"]) == 1
    assert len(rounds[2]["edges_added"]) == 1
    assert rounds[2]["cumulative_nodes"] == 3
    assert rounds[2]["cumulative_edges"] == 2


def test_simulation_graph_timeline_api_endpoint():
    """GET /api/simulation/<simulation_id>/graph-timeline returns 200 with code 0 and timeline."""
    app = Flask(__name__)
    app.register_blueprint(simulation_bp, url_prefix="/api/simulation")

    mock_storage = MagicMock()
    mock_storage.get_graph_timeline.return_value = {
        "simulation_id": "sim_xyz",
        "rounds": [
            {
                "round": 0,
                "nodes_added": [{"uuid": "n0"}],
                "edges_added": [],
                "cumulative_nodes": 1,
                "cumulative_edges": 0,
            },
            {
                "round": 1,
                "nodes_added": [{"uuid": "n1"}],
                "edges_added": [{"uuid": "e1"}],
                "cumulative_nodes": 2,
                "cumulative_edges": 1,
            }
        ]
    }
    app.extensions["neo4j_storage"] = mock_storage

    client = app.test_client()
    resp = client.get("/api/simulation/sim_xyz/graph-timeline?graph_id=g123")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["code"] == 0
    assert data["message"] == "success"
    assert data["success"] is True
    assert data["data"]["simulation_id"] == "sim_xyz"
    assert len(data["data"]["rounds"]) == 2
    mock_storage.get_graph_timeline.assert_called_once_with(simulation_id="sim_xyz", graph_id="g123")
