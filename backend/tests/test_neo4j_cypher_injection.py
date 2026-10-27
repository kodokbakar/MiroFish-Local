"""
Tests for Cypher injection prevention in Neo4jStorage, NERExtractor, and GraphToolsService.

Covers Issue #2:
- Label validation via LABEL_PATTERN, is_valid_label, and validate_label.
- Vector 1: add_text entity type backtick label injection prevention.
- Vector 2: get_nodes_by_label Cypher injection prevention via dynamic label.
- NERExtractor sanitization fallback to 'Entity' on invalid/malicious labels.
- GraphToolsService.get_entities_by_type graceful handling of invalid labels.
"""

import pytest
from unittest.mock import MagicMock, patch

from app.storage.label_utils import LABEL_PATTERN, is_valid_label, validate_label
from app.storage.neo4j_storage import Neo4jStorage
from app.storage.ner_extractor import NERExtractor
from app.services.graph_tools import GraphToolsService, NodeInfo


# ====================================================================
# 1. Label Validation Unit Tests
# ====================================================================

class TestLabelValidation:
    """Tests for is_valid_label and validate_label."""

    @pytest.mark.parametrize("valid_label", [
        "Person",
        "Organization",
        "Custom_Type123",
        "Entity",
        "Concept_Node_42",
        "_InternalType",
        "A",
        "a",
        "123",  # string consisting of digits is allowed by ^[A-Za-z0-9_]+$
    ])
    def test_valid_labels_accepted(self, valid_label):
        assert is_valid_label(valid_label) is True
        assert validate_label(valid_label) == valid_label

    @pytest.mark.parametrize("invalid_label,reason", [
        # Backtick injection payloads
        ("Person`", "trailing backtick"),
        ("`", "lone backtick"),
        ("`Person`", "wrapped backtick"),
        ("Person` ON MATCH SET n.injected = true //", "Cypher clause breakout"),
        ("Person`} MATCH (m) DETACH DELETE m //", "Cypher match/delete breakout"),
        ("Person`:Admin", "multiple labels via backtick"),
        # Whitespace
        ("", "empty string"),
        (" ", "single space"),
        ("   ", "multiple spaces"),
        ("Person Name", "internal space"),
        (" Person", "leading space"),
        ("Person ", "trailing space"),
        ("Person\tType", "tab character"),
        # Cypher clauses and syntax
        ("Person:Admin", "colon separator"),
        ("Person; DROP TABLE", "semicolon statement separator"),
        ("Person--comment", "double dash comment"),
        ("MATCH (n) RETURN n", "raw cypher query"),
        ("Person WHERE 1=1", "where clause"),
        ("Person-Type", "hyphen"),
        # Newlines
        ("Person\n", "trailing newline"),
        ("Person\r\n", "windows newline"),
        ("Person\nDETACH DELETE n", "newline injection"),
        # Comments
        ("Person // comment", "line comment"),
        ("Person /* comment */", "block comment"),
        # Non-string types
        (None, "None type"),
        (123, "int type"),
        (45.67, "float type"),
        ([], "list type"),
        ({}, "dict type"),
        (True, "boolean type"),
    ])
    def test_invalid_labels_rejected(self, invalid_label, reason):
        assert is_valid_label(invalid_label) is False, f"Failed for {reason}: {invalid_label!r}"
        with pytest.raises(ValueError, match="Invalid Neo4j label"):
            validate_label(invalid_label)


# ====================================================================
# 2. Neo4jStorage.get_nodes_by_label Tests (Vector 2)
# ====================================================================

class TestNeo4jStorageGetNodesByLabel:
    """Tests for Neo4jStorage.get_nodes_by_label validation."""

    def _create_storage_without_init(self):
        storage = Neo4jStorage.__new__(Neo4jStorage)
        storage._driver = MagicMock()
        return storage

    @pytest.mark.parametrize("payload", [
        "Person`} MATCH (m) DETACH DELETE m //",
        "Person` ON MATCH SET n.injected = true //",
        "`",
        "Person`",
        "",
        "Person Name",
        "Person:Admin",
        "Person\nWHERE 1=1",
    ])
    def test_get_nodes_by_label_raises_value_error_on_injection(self, payload):
        storage = self._create_storage_without_init()
        with pytest.raises(ValueError, match="Invalid Neo4j label"):
            storage.get_nodes_by_label("test_graph", payload)
        # Ensure driver session was never opened
        storage._driver.session.assert_not_called()

    def test_get_nodes_by_label_executes_query_on_valid_label(self):
        storage = self._create_storage_without_init()
        mock_session = MagicMock()
        storage._driver.session.return_value.__enter__.return_value = mock_session
        mock_tx = MagicMock()
        mock_record = {
            "n": {"uuid": "u1", "name": "Alice", "summary": "An engineer", "attributes_json": "{}"},
            "labels": ["Entity", "Person"],
        }
        mock_tx.run.return_value = [mock_record]
        mock_session.execute_read.side_effect = lambda tx_fn: tx_fn(mock_tx)
        storage._call_with_retry = lambda func, *args, **kwargs: func(*args, **kwargs)

        nodes = storage.get_nodes_by_label("test_graph", "Person")
        assert len(nodes) == 1
        assert nodes[0]["uuid"] == "u1"
        assert nodes[0]["name"] == "Alice"

        # Verify query was constructed with valid label safely
        mock_tx.run.assert_called_once()
        query_executed = mock_tx.run.call_args[0][0]
        assert "MATCH (n:Entity:`Person` {graph_id: $gid})" in query_executed


# ====================================================================
# 3. Neo4jStorage.add_text Tests (Vector 1)
# ====================================================================

class TestNeo4jStorageAddText:
    """Tests for Neo4jStorage.add_text label sanitization."""

    def _create_mocked_storage(self):
        storage = Neo4jStorage.__new__(Neo4jStorage)
        storage._driver = MagicMock()
        storage._embedding = MagicMock()
        storage._ner = MagicMock()
        storage._search = MagicMock()
        return storage

    def test_add_text_skips_malicious_label(self):
        storage = self._create_mocked_storage()
        storage.get_ontology = MagicMock(return_value={})

        malicious_etype = "Person` ON MATCH SET n.injected = true //"
        storage._ner.extract.return_value = {
            "entities": [
                {"name": "Alice", "type": malicious_etype, "attributes": {}}
            ],
            "relations": []
        }
        storage._embedding.get_embeddings.return_value = [[0.1] * 1536, [0.1] * 1536]

        mock_session = MagicMock()
        storage._driver.session.return_value.__enter__.return_value = mock_session

        mock_tx = MagicMock()
        record_mock = MagicMock()
        record_mock.__getitem__.side_effect = lambda k: "mock-uuid"
        mock_tx.run.return_value.single.return_value = record_mock
        mock_session.execute_write.side_effect = lambda tx_fn: tx_fn(mock_tx)
        storage._call_with_retry = lambda func, *args, **kwargs: func(*args, **kwargs)

        with patch("app.storage.neo4j_storage.logger.warning") as mock_warn:
            episode_id = storage.add_text("test_graph", "Alice went to Wonderland.")

        assert episode_id is not None

        # Verify no Cypher query containing malicious_etype was executed
        for call in mock_tx.run.call_args_list:
            query_str = call[0][0]
            assert malicious_etype not in query_str, f"Malicious query found: {query_str}"
            assert "SET n:`" not in query_str, f"Unexpected SET n: label query found: {query_str}"

        # Verify warning was logged
        assert any("Skipping invalid entity label" in str(call[0][0]) for call in mock_warn.call_args_list)

    def test_add_text_applies_valid_label(self):
        storage = self._create_mocked_storage()
        storage.get_ontology = MagicMock(return_value={})

        valid_etype = "Person"
        storage._ner.extract.return_value = {
            "entities": [
                {"name": "Alice", "type": valid_etype, "attributes": {}}
            ],
            "relations": []
        }
        storage._embedding.get_embeddings.return_value = [[0.1] * 1536, [0.1] * 1536]

        mock_session = MagicMock()
        storage._driver.session.return_value.__enter__.return_value = mock_session

        mock_tx = MagicMock()
        record_mock = MagicMock()
        record_mock.__getitem__.side_effect = lambda k: "mock-uuid"
        mock_tx.run.return_value.single.return_value = record_mock
        mock_session.execute_write.side_effect = lambda tx_fn: tx_fn(mock_tx)
        storage._call_with_retry = lambda func, *args, **kwargs: func(*args, **kwargs)

        episode_id = storage.add_text("test_graph", "Alice went to Wonderland.")
        assert episode_id is not None

        # Verify SET n:`Person` was executed
        executed_queries = [call[0][0] for call in mock_tx.run.call_args_list]
        assert any("SET n:`Person`" in q for q in executed_queries)


# ====================================================================
# 4. NERExtractor._validate_and_clean Tests
# ====================================================================

class TestNERExtractorValidateAndClean:
    """Tests for NERExtractor label validation and fallback."""

    def test_malicious_type_falls_back_to_entity(self, caplog):
        extractor = NERExtractor.__new__(NERExtractor)

        raw_result = {
            "entities": [
                {
                    "name": "EvilEntity",
                    "type": "Person` ON MATCH SET n.injected = true //",
                    "attributes": {"role": "hacker"}
                },
                {
                    "name": "GoodEntity",
                    "type": "Person",
                    "attributes": {"role": "user"}
                }
            ],
            "relations": []
        }
        ontology = {"entity_types": [{"name": "Person"}]}
        with patch("app.storage.ner_extractor.logger.warning") as mock_warn:
            cleaned = extractor._validate_and_clean(raw_result, ontology)

        entities = cleaned["entities"]
        assert len(entities) == 2

        evil = next(e for e in entities if e["name"] == "EvilEntity")
        assert evil["type"] == "Entity", "Malicious type must fallback to 'Entity'"

        good = next(e for e in entities if e["name"] == "GoodEntity")
        assert good["type"] == "Person", "Valid type must remain intact"

        assert any("invalid label" in str(call[0][0]) for call in mock_warn.call_args_list)
    @pytest.mark.parametrize("bad_type", [
        "Person`",
        "`",
        "Person:Admin",
        "Person\nDETACH DELETE",
        "Person Name",
        "",
    ])
    def test_various_invalid_types_fallback_to_entity(self, bad_type):
        extractor = NERExtractor.__new__(NERExtractor)
        raw_result = {
            "entities": [{"name": "TestEntity", "type": bad_type, "attributes": {}}],
            "relations": []
        }
        cleaned = extractor._validate_and_clean(raw_result, {})
        assert cleaned["entities"][0]["type"] == "Entity"


# ====================================================================
# 5. GraphToolsService.get_entities_by_type Tests
# ====================================================================

class TestGraphToolsGetEntitiesByType:
    def test_get_entities_by_type_returns_empty_list_on_invalid_type(self):
        mock_storage = MagicMock()
        mock_storage.get_nodes_by_label.side_effect = ValueError("Invalid Neo4j label: 'bad`type'")

        service = GraphToolsService(storage=mock_storage)

        with patch("app.services.graph_tools.logger.warning") as mock_warn:
            result = service.get_entities_by_type("test_graph", "bad`type")

        assert result == []
        assert any("Invalid entity type" in str(call[0][0]) for call in mock_warn.call_args_list)
    def test_get_entities_by_type_returns_nodes_on_valid_type(self):
        mock_storage = MagicMock()
        mock_storage.get_nodes_by_label.return_value = [
            {
                "uuid": "u1",
                "name": "Alice",
                "labels": ["Entity", "Person"],
                "summary": "Alice in Wonderland",
                "attributes": {"age": 20}
            }
        ]

        service = GraphToolsService(storage=mock_storage)
        result = service.get_entities_by_type("test_graph", "Person")

        assert len(result) == 1
        assert isinstance(result[0], NodeInfo)
        assert result[0].uuid == "u1"
        assert result[0].name == "Alice"
        assert result[0].labels == ["Entity", "Person"]
