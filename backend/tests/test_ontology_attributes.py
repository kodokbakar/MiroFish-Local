"""
Tests for ontology attribute normalization (port 018 / #135).
Ensures string attributes returned by LLMs are normalized into dicts with
name, type, and description fields across OntologyGenerator and GraphBuilderService.
"""

from unittest.mock import MagicMock
import pytest

from app.services.ontology_generator import OntologyGenerator
from app.services.graph_builder import GraphBuilderService


def test_validate_and_process_string_attributes():
    """String attributes in entity_types and edge_types are normalized by _validate_and_process()."""
    generator = OntologyGenerator(llm_client=MagicMock())
    raw = {
        "entity_types": [
            {
                "name": "Influencer",
                "description": "Key opinion leader",
                "attributes": ["follower_count", "verified_status"],
                "examples": ["KOL_1"]
            }
        ],
        "edge_types": [
            {
                "name": "INFLUENCES",
                "description": "Influence relation",
                "attributes": ["strength", "topic"],
                "source_targets": []
            }
        ]
    }

    processed = generator._validate_and_process(raw)

    influencer = next(e for e in processed["entity_types"] if e["name"] == "Influencer")
    assert influencer["attributes"] == [
        {"name": "follower_count", "type": "text", "description": "follower_count"},
        {"name": "verified_status", "type": "text", "description": "verified_status"},
    ]

    influences_edge = next(e for e in processed["edge_types"] if e["name"] == "INFLUENCES")
    assert influences_edge["attributes"] == [
        {"name": "strength", "type": "text", "description": "strength"},
        {"name": "topic", "type": "text", "description": "topic"},
    ]


def test_validate_and_process_mixed_attributes():
    """Mixed string and dict attributes are preserved/normalized correctly."""
    generator = OntologyGenerator(llm_client=MagicMock())
    raw = {
        "entity_types": [
            {
                "name": "Person",
                "description": "Individual person",
                "attributes": [
                    "nickname",
                    {"name": "age", "type": "integer", "description": "Age in years"},
                    {"name": "occupation", "type": "text", "description": "Job role"},
                ],
                "examples": ["Alice"]
            }
        ],
        "edge_types": [
            {
                "name": "INTERACTS_WITH",
                "description": "Interaction between accounts",
                "attributes": [
                    {"name": "frequency", "type": "integer", "description": "Interaction count"},
                    "platform",
                ],
                "source_targets": []
            }
        ]
    }

    processed = generator._validate_and_process(raw)

    person = next(e for e in processed["entity_types"] if e["name"] == "Person")
    assert person["attributes"] == [
        {"name": "nickname", "type": "text", "description": "nickname"},
        {"name": "age", "type": "integer", "description": "Age in years"},
        {"name": "occupation", "type": "text", "description": "Job role"},
    ]

    edge = next(e for e in processed["edge_types"] if e["name"] == "INTERACTS_WITH")
    assert edge["attributes"] == [
        {"name": "frequency", "type": "integer", "description": "Interaction count"},
        {"name": "platform", "type": "text", "description": "platform"},
    ]


def test_graph_builder_set_ontology_normalizes_string_attributes():
    """GraphBuilderService.set_ontology() normalizes string attributes before storing to storage."""
    mock_storage = MagicMock()
    service = GraphBuilderService(storage=mock_storage)

    ontology = {
        "entity_types": [
            {
                "name": "Organization",
                "description": "Company or group",
                "attributes": [
                    "location",
                    {"name": "headcount", "type": "integer", "description": "Employees count"},
                ]
            }
        ],
        "edge_types": [
            {
                "name": "PARTNERS_WITH",
                "description": "Partnership",
                "attributes": ["since_year"]
            }
        ],
        "relation_types": [
            {
                "name": "INVESTED_IN",
                "description": "Investment relation",
                "attributes": ["amount"]
            }
        ]
    }

    service.set_ontology("graph-123", ontology)

    mock_storage.set_ontology.assert_called_once()
    call_args = mock_storage.set_ontology.call_args[0]
    assert call_args[0] == "graph-123"

    passed_ontology = call_args[1]
    assert passed_ontology["entity_types"][0]["attributes"] == [
        {"name": "location", "type": "text", "description": "location"},
        {"name": "headcount", "type": "integer", "description": "Employees count"},
    ]
    assert passed_ontology["edge_types"][0]["attributes"] == [
        {"name": "since_year", "type": "text", "description": "since_year"},
    ]
    assert passed_ontology["relation_types"][0]["attributes"] == [
        {"name": "amount", "type": "text", "description": "amount"},
    ]


def test_validate_and_process_scalar_string_attributes():
    """Scalar string in attributes is normalized into a single-item list with dict."""
    generator = OntologyGenerator(llm_client=MagicMock())
    raw = {
        "entity_types": [
            {
                "name": "Influencer",
                "description": "Key opinion leader",
                "attributes": "follower_count",
                "examples": ["KOL_1"]
            }
        ],
        "edge_types": [
            {
                "name": "INFLUENCES",
                "description": "Influence relation",
                "attributes": "strength",
                "source_targets": []
            }
        ]
    }

    processed = generator._validate_and_process(raw)

    influencer = next(e for e in processed["entity_types"] if e["name"] == "Influencer")
    assert influencer["attributes"] == [
        {"name": "follower_count", "type": "text", "description": "follower_count"}
    ]

    edge = next(e for e in processed["edge_types"] if e["name"] == "INFLUENCES")
    assert edge["attributes"] == [
        {"name": "strength", "type": "text", "description": "strength"}
    ]


def test_validate_and_process_empty_none_and_missing_attributes():
    """Empty list, None, and omitted attributes default to empty lists without crashing."""
    generator = OntologyGenerator(llm_client=MagicMock())
    raw = {
        "entity_types": [
            {"name": "EmptyList", "description": "desc", "attributes": []},
            {"name": "NoneAttr", "description": "desc", "attributes": None},
            {"name": "MissingAttr", "description": "desc"},
        ],
        "edge_types": [
            {"name": "EMPTY_REL", "description": "desc", "attributes": []},
            {"name": "NONE_REL", "description": "desc", "attributes": None},
            {"name": "MISSING_REL", "description": "desc"},
        ]
    }

    processed = generator._validate_and_process(raw)

    for name in ("EmptyList", "NoneAttr", "MissingAttr"):
        entity = next(e for e in processed["entity_types"] if e["name"] == name)
        assert entity["attributes"] == []

    for name in ("EMPTY_REL", "NONE_REL", "MISSING_REL"):
        edge = next(e for e in processed["edge_types"] if e["name"] == name)
        assert edge["attributes"] == []


def test_graph_builder_set_ontology_scalar_string_attributes():
    """GraphBuilderService.set_ontology() normalizes scalar string attributes across entity, edge, and relation types."""
    mock_storage = MagicMock()
    service = GraphBuilderService(storage=mock_storage)

    ontology = {
        "entity_types": [
            {"name": "Topic", "description": "Subject", "attributes": "topic_id"}
        ],
        "edge_types": [
            {"name": "TAGGED_WITH", "description": "Tagging", "attributes": "confidence"}
        ],
        "relation_types": [
            {"name": "MENTIONS", "description": "Mention", "attributes": "sentiment"}
        ]
    }

    service.set_ontology("graph-scalar", ontology)

    passed_ontology = mock_storage.set_ontology.call_args[0][1]
    assert passed_ontology["entity_types"][0]["attributes"] == [
        {"name": "topic_id", "type": "text", "description": "topic_id"}
    ]
    assert passed_ontology["edge_types"][0]["attributes"] == [
        {"name": "confidence", "type": "text", "description": "confidence"}
    ]
    assert passed_ontology["relation_types"][0]["attributes"] == [
        {"name": "sentiment", "type": "text", "description": "sentiment"}
    ]


def test_graph_builder_set_ontology_handles_empty_none_and_non_dict_elements():
    """GraphBuilderService.set_ontology() safely handles empty, None, or non-dict structures."""
    mock_storage = MagicMock()
    service = GraphBuilderService(storage=mock_storage)

    ontology = {
        "entity_types": [
            {"name": "Normal", "attributes": []},
            {"name": "NoneAttr", "attributes": None},
            {"name": "MissingAttr"},
            "invalid_non_dict_item"
        ],
        "edge_types": [
            {"name": "NormalEdge", "attributes": []},
            {"name": "NoneEdge", "attributes": None},
        ],
        "relation_types": None
    }

    # Should not raise exception
    service.set_ontology("graph-edge-cases", ontology)
    mock_storage.set_ontology.assert_called_once()
