"""
Unit and regression tests for Item 038 (Issue #15) performance optimizations:
1. Unified `scope="both"` search in `OasisProfileGenerator._search_graph_for_entity()`
2. Throttled / linearized real-time profile persistence and final flush in `save_profiles_realtime`
3. Projected Cypher record unpacking (nodes, relationships, missing attributes fallback, score)
4. Persistent `requests.Session` with `HTTPAdapter` connection pooling and reuse in `EmbeddingService`
"""

from unittest.mock import MagicMock, Mock, patch

import pytest
import requests
from requests.adapters import HTTPAdapter

from app.config import Config
from app.services.entity_reader import EntityNode
from app.services.oasis_profile_generator import (
    OasisAgentProfile,
    OasisProfileGenerator,
)
from app.storage.embedding_service import EmbeddingService
from app.storage.search_service import (
    SearchService,
    _unpack_node,
    _unpack_relationship,
)


# =====================================================================
# 1. Unified `scope="both"` search in _search_graph_for_entity()
# =====================================================================

class TestUnifiedGraphSearch:
    """Tests for unified graph search in OasisProfileGenerator._search_graph_for_entity()."""

    @pytest.fixture
    def generator(self):
        gen = object.__new__(OasisProfileGenerator)
        gen.storage = Mock()
        gen.graph_id = "test_graph_123"
        return gen

    def test_search_graph_calls_unified_scope_both_single_query(self, generator):
        """Verify _search_graph_for_entity issues exactly ONE unified query with scope='both'."""
        generator.storage.search.return_value = {
            "edges": [
                {"fact": "Alice is a software architect"},
                {"fact": "Alice collaborates with Bob"},
                {"fact": ""},  # empty fact should be ignored
            ],
            "nodes": [
                {"summary": "Alice specializes in distributed systems", "name": "Alice"},
                {"summary": "Bob is a security researcher", "name": "Bob"},
            ],
        }

        entity = EntityNode(
            uuid="e1",
            name="Alice",
            labels=["Person"],
            summary="A senior engineer",
            attributes={},
        )
        result = generator._search_graph_for_entity(entity)

        assert generator.storage.search.call_count == 1
        call_kwargs = generator.storage.search.call_args.kwargs
        assert call_kwargs["graph_id"] == "test_graph_123"
        assert "Alice" in call_kwargs["query"]
        assert call_kwargs["limit"] == 30
        assert call_kwargs["scope"] == "both"

        # Check facts
        assert "Alice is a software architect" in result["facts"]
        assert "Alice collaborates with Bob" in result["facts"]
        assert "" not in result["facts"]
        assert len(result["facts"]) == 2

        # Check node summaries
        assert "Alice specializes in distributed systems" in result["node_summaries"]
        assert "Bob is a security researcher" in result["node_summaries"]
        # Related entity name != entity_name should be included
        assert "Related Entity: Bob" in result["node_summaries"]
        # entity_name itself should NOT create "Related Entity: Alice"
        assert "Related Entity: Alice" not in result["node_summaries"]

        # Check context string formatting
        assert "Fact Information:" in result["context"]
        assert "- Alice is a software architect" in result["context"]
        assert "Related Entities:" in result["context"]
        assert "- Bob is a security researcher" in result["context"]

    def test_search_graph_caps_facts_and_summaries_in_context(self, generator):
        """Verify context caps facts to 20 and node summaries to 10."""
        many_edges = [{"fact": f"Fact number {i}"} for i in range(35)]
        many_nodes = [{"summary": f"Summary number {i}", "name": f"Other_{i}"} for i in range(25)]

        generator.storage.search.return_value = {
            "edges": many_edges,
            "nodes": many_nodes,
        }

        entity = EntityNode(
            uuid="e_main",
            name="MainEntity",
            labels=["Entity"],
            summary="",
            attributes={},
        )
        result = generator._search_graph_for_entity(entity)

        assert len(result["facts"]) == 35
        # 25 summaries + 25 related entities = 50 items in node_summaries set
        assert len(result["node_summaries"]) == 50

        # Context string caps
        facts_section = result["context"].split("Related Entities:")[0]
        summaries_section = result["context"].split("Related Entities:")[1]

        # Max 20 facts in context
        fact_lines = [line for line in facts_section.strip().split("\n") if line.startswith("- ")]
        assert len(fact_lines) == 20

        # Max 10 summaries in context
        summary_lines = [line for line in summaries_section.strip().split("\n") if line.startswith("- ")]
        assert len(summary_lines) == 10

    def test_search_graph_handles_none_storage_or_graph_id(self):
        """Verify returns empty results when storage or graph_id is None without error."""
        entity = EntityNode(uuid="e1", name="Alice", labels=["Person"], summary="", attributes={})

        gen = object.__new__(OasisProfileGenerator)
        gen.storage = None
        gen.graph_id = "test_graph"
        res = gen._search_graph_for_entity(entity)
        assert res == {"facts": [], "node_summaries": [], "context": ""}

        gen.storage = Mock()
        gen.graph_id = None
        res = gen._search_graph_for_entity(entity)
        assert res == {"facts": [], "node_summaries": [], "context": ""}
        gen.storage.search.assert_not_called()

    def test_search_graph_handles_storage_exception_gracefully(self, generator):
        """Verify storage exceptions are caught and return safe empty dict."""
        generator.storage.search.side_effect = RuntimeError("Neo4j connection error")
        entity = EntityNode(uuid="e1", name="Alice", labels=["Person"], summary="", attributes={})

        result = generator._search_graph_for_entity(entity)

        assert result == {"facts": [], "node_summaries": [], "context": ""}

    def test_search_graph_handles_non_dict_or_empty_search_results(self, generator):
        """Verify non-dict returns or missing keys default to empty structures."""
        entity = EntityNode(uuid="e1", name="Alice", labels=["Person"], summary="", attributes={})

        generator.storage.search.return_value = None
        result = generator._search_graph_for_entity(entity)
        assert result == {"facts": [], "node_summaries": [], "context": ""}

        generator.storage.search.return_value = []
        result = generator._search_graph_for_entity(entity)
        assert result == {"facts": [], "node_summaries": [], "context": ""}

        generator.storage.search.return_value = {"edges": None, "nodes": None}
        result = generator._search_graph_for_entity(entity)
        assert result == {"facts": [], "node_summaries": [], "context": ""}


# =====================================================================
# 2. Throttled Realtime Profile Persistence & Final Flush
# =====================================================================

class TestThrottledRealtimePersistence:
    """Tests for save_profiles_realtime throttling, threshold triggers, and final flush."""

    @pytest.fixture
    def generator(self):
        gen = object.__new__(OasisProfileGenerator)
        gen.storage = None
        gen.graph_id = None
        return gen

    def test_realtime_save_throttled_under_10_completions(self, generator, tmp_path):
        """Verify 5 rapid profile generations (< 3.0s, < 10 count) throttle intermediate writes."""
        output_file = str(tmp_path / "profiles.csv")
        entities = [
            EntityNode(uuid=f"e{i}", name=f"Agent {i}", labels=["Person"], summary="", attributes={})
            for i in range(5)
        ]

        def mock_generate(entity, user_id, use_llm):
            return OasisAgentProfile(
                user_id=user_id,
                user_name=f"agent_{user_id}",
                name=f"Agent {user_id}",
                bio=f"Bio {user_id}",
                persona=f"Persona {user_id}",
            )

        generator.generate_profile_from_entity = mock_generate
        save_mock = Mock(wraps=generator.save_profiles)
        generator.save_profiles = save_mock

        # Run with 1 worker to execute linearly and rapidly
        generator.generate_profiles_from_entities(
            entities=entities,
            use_llm=False,
            parallel_count=1,
            realtime_output_path=output_file,
            output_platform="twitter",
        )

        # 1st completion saves (initial delta from 0.0 > 3.0s).
        # Completions 2, 3, 4, 5 are throttled and skipped (< 10 delta, < 3.0s).
        # Finally block save_profiles_realtime(force=True) guarantees all 5 are saved.
        # Total writes = 2 (instead of 5+ without throttling).
        assert save_mock.call_count == 2
        saved_profiles = save_mock.call_args[0][0]
        assert len(saved_profiles) == 5

    def test_realtime_save_triggers_at_10_completions_threshold(self, generator, tmp_path):
        """Verify completing 10 items triggers an intermediate save, followed by the final flush."""
        output_file = str(tmp_path / "profiles.csv")
        entities = [
            EntityNode(uuid=f"e{i}", name=f"Agent {i}", labels=["Person"], summary="", attributes={})
            for i in range(15)
        ]

        def mock_generate(entity, user_id, use_llm):
            return OasisAgentProfile(
                user_id=user_id,
                user_name=f"agent_{user_id}",
                name=f"Agent {user_id}",
                bio=f"Bio {user_id}",
                persona=f"Persona {user_id}",
            )

        generator.generate_profile_from_entity = mock_generate
        save_mock = Mock(wraps=generator.save_profiles)
        generator.save_profiles = save_mock

        generator.generate_profiles_from_entities(
            entities=entities,
            use_llm=False,
            parallel_count=1,
            realtime_output_path=output_file,
            output_platform="twitter",
        )

        # Completion #1 saves (init delta).
        # Completion #11 saves (11 - 1 = 10 items since last save).
        # Final flush (force=True) saves all 15.
        # Total = 3 writes (instead of 15 without batching).
        assert save_mock.call_count == 3
        first_save = save_mock.call_args_list[0][0][0]
        second_save = save_mock.call_args_list[1][0][0]
        final_save = save_mock.call_args_list[2][0][0]
        assert len(first_save) == 1
        assert len(second_save) == 11
        assert len(final_save) == 15

    def test_realtime_save_triggers_when_time_exceeds_3_seconds(self, generator, tmp_path):
        """Verify intermediate save triggers when time since last save exceeds 3.0s even with < 10 items."""
        output_file = str(tmp_path / "profiles.csv")
        entities = [
            EntityNode(uuid=f"e{i}", name=f"Agent {i}", labels=["Person"], summary="", attributes={})
            for i in range(3)
        ]

        def mock_generate(entity, user_id, use_llm):
            return OasisAgentProfile(
                user_id=user_id,
                user_name=f"agent_{user_id}",
                name=f"Agent {user_id}",
                bio=f"Bio {user_id}",
                persona=f"Persona {user_id}",
            )

        generator.generate_profile_from_entity = mock_generate
        save_mock = Mock(wraps=generator.save_profiles)
        generator.save_profiles = save_mock

        # Simulate time passage: 100.0s for init, 104.5s on 2nd completion
        fake_times = [100.0, 100.5, 104.5, 104.6, 104.7, 105.0]
        time_iter = iter(fake_times)

        def mock_time():
            try:
                return next(time_iter)
            except StopIteration:
                return 110.0

        with patch("time.time", side_effect=mock_time):
            generator.generate_profiles_from_entities(
                entities=entities,
                use_llm=False,
                parallel_count=1,
                realtime_output_path=output_file,
                output_platform="twitter",
            )

        # At least one intermediate save triggered by elapsed time >= 3.0s, plus final flush
        assert save_mock.call_count >= 2

    def test_final_flush_guaranteed_even_on_worker_exception(self, generator, tmp_path):
        """Verify finally block flushes valid and fallback profiles even if an error occurs."""
        output_file = str(tmp_path / "profiles.json")
        entities = [
            EntityNode(uuid="e0", name="Good Agent", labels=["Person"], summary="", attributes={}),
            EntityNode(uuid="e1", name="Failing Agent", labels=["Person"], summary="", attributes={}),
        ]

        def mock_generate(entity, user_id, use_llm):
            if user_id == 1:
                raise RuntimeError("LLM API exploded")
            return OasisAgentProfile(
                user_id=user_id,
                user_name=f"agent_{user_id}",
                name=entity.name,
                bio=f"Bio {user_id}",
                persona=f"Persona {user_id}",
            )

        generator.generate_profile_from_entity = mock_generate
        save_mock = Mock(wraps=generator.save_profiles)
        generator.save_profiles = save_mock

        generator.generate_profiles_from_entities(
            entities=entities,
            use_llm=False,
            parallel_count=1,
            realtime_output_path=output_file,
            output_platform="reddit",
        )

        assert save_mock.call_count >= 1
        final_profiles = save_mock.call_args[0][0]
        assert len(final_profiles) == 2
        # Profile 0 is generated, profile 1 is fallback
        assert final_profiles[0].name == "Good Agent"
        assert final_profiles[1].name == "Failing Agent"


# =====================================================================
# 3. Projected Cypher Record Unpacking (_unpack_relationship & _unpack_node)
# =====================================================================

class TestProjectedRecordUnpacking:
    """Tests for projected Cypher record unpacking in SearchService."""

    def test_unpack_relationship_projected_flat_record(self):
        """Verify projected flat relationship record is unpacked with all standard fields."""
        record = {
            "uuid": "rel-999",
            "name": "COLLABORATES_WITH",
            "fact": "Eve collaborates with Frank",
            "src_uuid": "user_eve",
            "tgt_uuid": "user_frank",
            "score": 0.94,
            "extra_field": "preserved",
        }

        result = _unpack_relationship(record)

        assert result["uuid"] == "rel-999"
        assert result["name"] == "COLLABORATES_WITH"
        assert result["fact"] == "Eve collaborates with Frank"
        assert result["source_node_uuid"] == "user_eve"
        assert result["target_node_uuid"] == "user_frank"
        assert result["attributes"] == {}
        assert result["_score"] == 0.94
        assert result["extra_field"] == "preserved"
        assert "score" not in result

    def test_unpack_relationship_filters_embeddings_and_json(self):
        """Verify fact_embedding and attributes_json are excluded from projected relationship."""
        record = {
            "uuid": "r1",
            "name": "TEST",
            "fact": "Some fact",
            "src_uuid": "s1",
            "tgt_uuid": "t1",
            "score": 0.88,
            "fact_embedding": [0.1, 0.2, 0.3],
            "attributes_json": '{"weight": 5}',
        }

        result = _unpack_relationship(record)

        assert "fact_embedding" not in result
        assert "attributes_json" not in result
        assert result["_score"] == 0.88

    def test_unpack_relationship_missing_fields_defaults(self):
        """Verify empty projected record defaults safely without KeyError."""
        result = _unpack_relationship({})

        assert result["uuid"] == ""
        assert result["name"] == ""
        assert result["fact"] == ""
        assert result["source_node_uuid"] == ""
        assert result["target_node_uuid"] == ""
        assert result["attributes"] == {}
        assert result["_score"] == 0.0

    def test_unpack_relationship_legacy_r_record_backwards_compatible(self):
        """Verify legacy record with 'r' key is still parsed via edge_to_dict."""
        record = {
            "r": {
                "uuid": "r_old",
                "name": "FOLLOWS",
                "fact": "Old fact",
                "graph_id": "g1",
                "fact_embedding": [0.1],
                "attributes_json": '{"strength": 10}',
            },
            "src_uuid": "u1",
            "tgt_uuid": "u2",
            "score": 0.75,
        }

        result = _unpack_relationship(record)

        assert result["uuid"] == "r_old"
        assert result["source_node_uuid"] == "u1"
        assert result["target_node_uuid"] == "u2"
        assert result["attributes"] == {"strength": 10}
        assert result["_score"] == 0.75
        assert "fact_embedding" not in result
        assert "attributes_json" not in result

    def test_unpack_node_projected_flat_record(self):
        """Verify projected flat node record is unpacked and 'Entity' label is filtered out."""
        record = {
            "uuid": "node-123",
            "name": "Dr. Sarah",
            "summary": "Biologist researching genetics",
            "labels": ["Entity", "Person", "Scientist"],
            "score": 0.91,
            "custom_metadata": "data",
        }

        result = _unpack_node(record)

        assert result["uuid"] == "node-123"
        assert result["name"] == "Dr. Sarah"
        assert result["summary"] == "Biologist researching genetics"
        assert result["labels"] == ["Person", "Scientist"]
        assert "Entity" not in result["labels"]
        assert result["attributes"] == {}
        assert result["_score"] == 0.91
        assert result["custom_metadata"] == "data"
        assert "score" not in result

    def test_unpack_node_filters_embeddings_and_name_lower(self):
        """Verify embedding, name_lower, and attributes_json are stripped from projected node."""
        record = {
            "uuid": "n1",
            "name": "Alice",
            "summary": "Engineer",
            "labels": ["Person"],
            "score": 0.85,
            "embedding": [0.5, 0.6],
            "name_lower": "alice",
            "attributes_json": '{"team": "infra"}',
        }

        result = _unpack_node(record)

        assert "embedding" not in result
        assert "name_lower" not in result
        assert "attributes_json" not in result
        assert result["_score"] == 0.85

    def test_unpack_node_missing_fields_defaults(self):
        """Verify empty projected node record defaults safely."""
        result = _unpack_node({})

        assert result["uuid"] == ""
        assert result["name"] == ""
        assert result["summary"] == ""
        assert result["labels"] == []
        assert result["attributes"] == {}
        assert result["_score"] == 0.0

    def test_unpack_node_legacy_n_record_backwards_compatible(self):
        """Verify legacy record with 'n' key is still parsed via node_to_dict."""
        record = {
            "n": {
                "uuid": "n_old",
                "name": "Bob",
                "summary": "Old summary",
                "graph_id": "g1",
                "embedding": [0.1],
                "name_lower": "bob",
                "attributes_json": '{"dept": "finance"}',
            },
            "labels": ["Entity", "Staff"],
            "score": 0.65,
        }

        result = _unpack_node(record)

        assert result["uuid"] == "n_old"
        assert result["name"] == "Bob"
        assert result["summary"] == "Old summary"
        assert result["labels"] == ["Staff"]
        assert result["attributes"] == {"dept": "finance"}
        assert result["_score"] == 0.65
        assert not {"embedding", "name_lower", "attributes_json"} & result.keys()

    def test_search_service_unpacks_projected_cypher_records(self):
        """Verify SearchService.search_edges and search_nodes work seamlessly with projected records."""
        embedding = Mock()
        embedding.embed.return_value = [0.1]
        service = SearchService(embedding)
        session = Mock()

        # Mock projected edges returned by neo4j
        projected_edge = {
            "uuid": "proj_e1",
            "name": "MENTIONS",
            "fact": "Report mentions Project X",
            "src_uuid": "rep_1",
            "tgt_uuid": "proj_x",
            "score": 0.95,
        }
        session.run.return_value = [projected_edge]

        edges = service.search_edges(session, "g1", "Project X")
        assert len(edges) == 1
        assert edges[0]["uuid"] == "proj_e1"
        assert edges[0]["source_node_uuid"] == "rep_1"
        assert edges[0]["target_node_uuid"] == "proj_x"
        assert edges[0]["score"] == pytest.approx(1.0)

        # Mock projected nodes returned by neo4j
        projected_node = {
            "uuid": "proj_n1",
            "name": "Project X",
            "summary": "Next-gen initiative",
            "labels": ["Entity", "Project"],
            "score": 0.88,
        }
        session.run.return_value = [projected_node]

        nodes = service.search_nodes(session, "g1", "Project X")
        assert len(nodes) == 1
        assert nodes[0]["uuid"] == "proj_n1"
        assert nodes[0]["labels"] == ["Project"]
        assert nodes[0]["score"] == pytest.approx(1.0)


# =====================================================================
# 4. Persistent requests.Session with HTTPAdapter Connection Pooling
# =====================================================================

class TestEmbeddingServiceSessionPooling:
    """Tests for persistent session and connection pooling in EmbeddingService."""

    def setup_method(self):
        Config.EMBEDDING_PROVIDER = "gemini"
        Config.GEMINI_API_KEY = "test-gemini-key"
        Config.EMBEDDING_MODEL = "gemini-embedding-2"
        Config.EMBEDDING_DIMENSIONS = 768

    def test_session_initialized_with_http_adapter_connection_pools(self):
        """Verify EmbeddingService sets up persistent session with pool_connections=10 and pool_maxsize=20."""
        service = EmbeddingService(provider="gemini", api_key="test-key", dimensions=768)

        assert isinstance(service._session, requests.Session)

        http_adapter = service._session.adapters.get("http://")
        https_adapter = service._session.adapters.get("https://")

        assert isinstance(http_adapter, HTTPAdapter)
        assert isinstance(https_adapter, HTTPAdapter)

        # Verify pool connection sizes configured in Issue #15
        assert http_adapter._pool_connections == 10
        assert http_adapter._pool_maxsize == 20
        assert https_adapter._pool_connections == 10
        assert https_adapter._pool_maxsize == 20

        service.close()

    def test_session_reused_across_multiple_embed_calls(self):
        """Verify requests session is reused across consecutive embed calls."""
        service = EmbeddingService(provider="gemini", api_key="test-key", dimensions=768)

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "embeddings": [{"values": [0.1] * 768}]
        }

        # Mock the session's post method
        mock_session_post = MagicMock(return_value=mock_resp)
        service._session.post = mock_session_post

        # Make two calls with different texts
        vec1 = service.embed("first query")
        vec2 = service.embed("second query")

        assert len(vec1) == 768
        assert len(vec2) == 768
        assert mock_session_post.call_count == 2
        # Ensure it called through service._session.post, not a new connection
        assert mock_session_post.call_args_list[0][0][0] == service._embed_url
        assert mock_session_post.call_args_list[1][0][0] == service._embed_url

        service.close()

    def test_session_close_and_context_manager(self):
        """Verify close() closes persistent session, and context manager lifecycle calls close()."""
        service = EmbeddingService(provider="gemini", api_key="test-key", dimensions=768)
        close_mock = MagicMock()
        service._session.close = close_mock

        with service as s:
            assert s is service
            assert not close_mock.called

        assert close_mock.call_count == 1

        # Calling close again does not raise
        service.close()
        assert close_mock.call_count == 2

    def test_session_close_idempotent_on_deleted_session(self):
        """Verify close() and __del__() tolerate missing or already-closed _session attribute."""
        service = EmbeddingService(provider="gemini", api_key="test-key", dimensions=768)
        service.close()
        delattr(service, "_session")

        # Must not raise attribute error
        service.close()
        del service
