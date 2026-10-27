"""
Unit tests for ontology generator, casing, remapping, deduplication, fallback limits, and long-text sampling (ports 019, 020, 021).
"""

import logging
from unittest.mock import MagicMock

import pytest

from app.services.ontology_generator import OntologyGenerator, _to_pascal_case
from app.utils.llm_client import LLMResponseError


class RecordingLLMClient:
    def __init__(self):
        self.calls = []

    def chat_json(self, **kwargs):
        self.calls.append(kwargs)
        return {
            "entity_types": [],
            "edge_types": [],
            "analysis_summary": "ok",
        }


def test_ontology_generation_does_not_cap_structured_output_tokens():
    llm = RecordingLLMClient()
    generator = OntologyGenerator(llm_client=llm)

    result = generator.generate(
        document_texts=["A short source document."],
        simulation_requirement="Simulate the public discussion.",
    )

    assert result["analysis_summary"] == "ok"
    assert llm.calls[0]["max_tokens"] is None
    assert llm.calls[0]["max_attempts"] == 2


def test_ontology_generator_propagates_llm_response_error():
    class FailingLLMClient:
        def chat_json(self, **kwargs):
            raise LLMResponseError(
                "LLM JSON output was truncated at the token limit",
                finish_reason="length",
            )

    generator = OntologyGenerator(llm_client=FailingLLMClient())

    with pytest.raises(LLMResponseError) as captured:
        generator.generate(
            document_texts=["A short source document."],
            simulation_requirement="Simulate the public discussion.",
        )

    assert captured.value.finish_reason == "length"
    assert "token limit" in str(captured.value)
# ============================================================================
# 1. Tests for _to_pascal_case helper function
# ============================================================================

class TestToPascalCase:
    """Tests for _to_pascal_case conversion function."""

    @pytest.mark.parametrize(
        ("input_name", "expected"),
        [
            ("works_for", "WorksFor"),
            ("public_figure", "PublicFigure"),
            ("social_media_influencer", "SocialMediaInfluencer"),
            ("long_snake_case_entity_name", "LongSnakeCaseEntityName"),
        ],
    )
    def test_snake_case_conversion(self, input_name: str, expected: str):
        """snake_case identifiers should convert to PascalCase."""
        assert _to_pascal_case(input_name) == expected

    @pytest.mark.parametrize(
        ("input_name", "expected"),
        [
            ("camelCase", "CamelCase"),
            ("specialTopic", "SpecialTopic"),
            ("mediaOutletAccount", "MediaOutletAccount"),
        ],
    )
    def test_camel_case_conversion(self, input_name: str, expected: str):
        """camelCase identifiers should split camel boundaries and convert to PascalCase."""
        assert _to_pascal_case(input_name) == expected

    @pytest.mark.parametrize(
        ("input_name", "expected"),
        [
            ("media-outlet", "MediaOutlet"),
            ("fact-checker", "FactChecker"),
            ("non-profit-organization", "NonProfitOrganization"),
        ],
    )
    def test_kebab_case_conversion(self, input_name: str, expected: str):
        """kebab-case identifiers should convert to PascalCase."""
        assert _to_pascal_case(input_name) == expected

    @pytest.mark.parametrize(
        ("input_name", "expected"),
        [
            ("PublicFigure", "PublicFigure"),
            ("Person", "Person"),
            ("Organization", "Organization"),
            ("SocialMediaInfluencer", "SocialMediaInfluencer"),
        ],
    )
    def test_already_pascal_case(self, input_name: str, expected: str):
        """Identifiers already in PascalCase should remain PascalCase."""
        assert _to_pascal_case(input_name) == expected

    @pytest.mark.parametrize(
        ("input_name", "expected"),
        [
            ("person", "Person"),
            ("organization", "Organization"),
            ("user", "User"),
            ("ROLE", "Role"),
        ],
    )
    def test_single_word(self, input_name: str, expected: str):
        """Single words should be capitalized."""
        assert _to_pascal_case(input_name) == expected

    def test_empty_string_returns_unknown(self):
        """Empty string should return 'Unknown' fallback."""
        assert _to_pascal_case("") == "Unknown"

    @pytest.mark.parametrize(
        ("input_name", "expected"),
        [
            ("!@#$%", "Unknown"),
            ("   ---   ", "Unknown"),
            ("hello world", "HelloWorld"),
            ("entity.with.dots", "EntityWithDots"),
            ("user_name_123", "UserName123"),
            ("mixed-case_with.symbols & more", "MixedCaseWithSymbolsMore"),
            ("foo--bar__baz", "FooBarBaz"),
        ],
    )
    def test_non_alphanumeric_characters(self, input_name: str, expected: str):
        """Non-alphanumeric characters act as separators, or return 'Unknown' if all empty."""
        assert _to_pascal_case(input_name) == expected


# ============================================================================
# 2. Tests for OntologyGenerator._validate_and_process
# ============================================================================

class TestOntologyGeneratorValidateAndProcess:
    """Tests for OntologyGenerator._validate_and_process post-processing logic."""

    @pytest.fixture
    def generator(self) -> OntologyGenerator:
        return OntologyGenerator(llm_client=MagicMock())

    def test_entity_name_pascal_case_normalization_and_logging(
        self, generator: OntologyGenerator, caplog: pytest.LogCaptureFixture
    ):
        """Entity names should be normalized to PascalCase and logged with warning."""
        raw_result = {
            "entity_types": [
                {"name": "public_figure", "description": "A public figure"},
                {"name": "media-outlet", "description": "News agency"},
                {"name": "specialAccount", "description": "Special account"},
                {"name": "Person", "description": "Already PascalCase"},
            ],
            "edge_types": [],
        }

        with caplog.at_level(logging.WARNING):
            processed = generator._validate_and_process(raw_result)

        names = [e["name"] for e in processed["entity_types"]]
        assert "PublicFigure" in names
        assert "MediaOutlet" in names
        assert "SpecialAccount" in names
        assert "Person" in names

        # Verify warning log was emitted for converted names
        assert "Entity type name 'public_figure' auto-converted to 'PublicFigure'" in caplog.text
        assert "Entity type name 'media-outlet' auto-converted to 'MediaOutlet'" in caplog.text
        assert "Entity type name 'specialAccount' auto-converted to 'SpecialAccount'" in caplog.text
        # Verify no warning for already PascalCase name
        assert "Entity type name 'Person' auto-converted" not in caplog.text

    def test_edge_name_uppercase_conversion_and_logging(
        self, generator: OntologyGenerator, caplog: pytest.LogCaptureFixture
    ):
        """Edge names should be converted to uppercase and logged with warning."""
        raw_result = {
            "entity_types": [],
            "edge_types": [
                {"name": "works_for", "description": "Employment relationship"},
                {"name": "interacts_with", "description": "Social interaction"},
                {"name": "SUPPORTS", "description": "Already uppercase"},
            ],
        }

        with caplog.at_level(logging.WARNING):
            processed = generator._validate_and_process(raw_result)

        edge_names = [e["name"] for e in processed["edge_types"]]
        assert edge_names == ["WORKS_FOR", "INTERACTS_WITH", "SUPPORTS"]

        # Verify warning log was emitted for converted edge names
        assert "Edge type name 'works_for' auto-converted to 'WORKS_FOR'" in caplog.text
        assert "Edge type name 'interacts_with' auto-converted to 'INTERACTS_WITH'" in caplog.text
        # Verify no warning for already uppercase edge
        assert "Edge type name 'SUPPORTS' auto-converted" not in caplog.text

    def test_edge_source_targets_remapping(self, generator: OntologyGenerator):
        """Edge source_targets references should be updated to match converted PascalCase entity names."""
        raw_result = {
            "entity_types": [
                {"name": "journalist_user", "description": "Journalist"},
                {"name": "media-outlet", "description": "Media outlet"},
            ],
            "edge_types": [
                {
                    "name": "works_for",
                    "description": "Employment",
                    "source_targets": [
                        {"source": "journalist_user", "target": "media-outlet"},
                        {"source": "Person", "target": "media-outlet"},
                    ],
                }
            ],
        }

        processed = generator._validate_and_process(raw_result)
        edge = processed["edge_types"][0]

        assert edge["source_targets"] == [
            {"source": "JournalistUser", "target": "MediaOutlet"},
            {"source": "Person", "target": "MediaOutlet"},
        ]

    def test_entity_deduplication_keeps_first_occurrence(
        self, generator: OntologyGenerator, caplog: pytest.LogCaptureFixture
    ):
        """Duplicates after casing normalization should be removed, keeping first occurrence."""
        raw_result = {
            "entity_types": [
                {"name": "public_figure", "description": "First definition: public figure"},
                {"name": "PublicFigure", "description": "Second definition: duplicate"},
                {"name": "public-figure", "description": "Third definition: duplicate"},
                {"name": "journalist", "description": "A reporter"},
            ],
            "edge_types": [],
        }

        with caplog.at_level(logging.WARNING):
            processed = generator._validate_and_process(raw_result)

        entity_map = {e["name"]: e["description"] for e in processed["entity_types"]}
        assert entity_map["PublicFigure"] == "First definition: public figure"
        assert entity_map["Journalist"] == "A reporter"

        # Count occurrences of PublicFigure
        pf_count = sum(1 for e in processed["entity_types"] if e["name"] == "PublicFigure")
        assert pf_count == 1

        # Check deduplication warning log
        assert "Duplicate entity type 'PublicFigure' removed during validation" in caplog.text

    def test_fallback_types_added_when_missing(self, generator: OntologyGenerator):
        """Person and Organization fallbacks should be added if not present."""
        raw_result = {
            "entity_types": [
                {"name": "journalist", "description": "Journalist"},
                {"name": "politician", "description": "Politician"},
            ],
            "edge_types": [],
        }

        processed = generator._validate_and_process(raw_result)
        names = [e["name"] for e in processed["entity_types"]]

        assert "Journalist" in names
        assert "Politician" in names
        assert "Person" in names
        assert "Organization" in names

        # Verify fallback properties
        person_type = next(e for e in processed["entity_types"] if e["name"] == "Person")
        assert any(attr["name"] == "full_name" for attr in person_type["attributes"])
        assert "ordinary citizen" in person_type["examples"]

        org_type = next(e for e in processed["entity_types"] if e["name"] == "Organization")
        assert any(attr["name"] == "org_name" for attr in org_type["attributes"])
        assert "small business" in org_type["examples"]

    def test_fallback_types_not_duplicated_if_already_present(
        self, generator: OntologyGenerator
    ):
        """If Person or Organization already exists, do not add duplicate fallback."""
        raw_result = {
            "entity_types": [
                {"name": "Person", "description": "Custom person type", "attributes": [], "examples": []},
            ],
            "edge_types": [],
        }

        processed = generator._validate_and_process(raw_result)
        names = [e["name"] for e in processed["entity_types"]]

        # Person appears once (custom version preserved), Organization fallback added
        assert names.count("Person") == 1
        assert names.count("Organization") == 1
        person_entity = next(e for e in processed["entity_types"] if e["name"] == "Person")
        assert person_entity["description"] == "Custom person type"

    def test_entity_slot_limit_enforced_when_fallbacks_needed(
        self, generator: OntologyGenerator
    ):
        """When 10 custom entities exist and fallbacks are missing, remove tail items to fit fallbacks."""
        # 10 custom entity types, none are Person or Organization
        entities = [{"name": f"entity_{i}", "description": f"Entity {i}"} for i in range(10)]
        raw_result = {
            "entity_types": entities,
            "edge_types": [],
        }

        processed = generator._validate_and_process(raw_result)

        assert len(processed["entity_types"]) == 10
        names = [e["name"] for e in processed["entity_types"]]
        # Fallback types must be present
        assert "Person" in names
        assert "Organization" in names
        # First 8 entities (entity_0 to entity_7) should be preserved
        for i in range(8):
            assert f"Entity{i}" in names
        # Last 2 entities (entity_8, entity_9) should have been displaced
        assert "Entity8" not in names
        assert "Entity9" not in names

    def test_edge_types_slot_limit(self, generator: OntologyGenerator):
        """Edge types should be capped at MAX_EDGE_TYPES (10)."""
        edges = [{"name": f"edge_{i}", "description": f"Edge {i}"} for i in range(15)]
        raw_result = {
            "entity_types": [],
            "edge_types": edges,
        }

        processed = generator._validate_and_process(raw_result)

        assert len(processed["edge_types"]) == 10
        expected_names = [f"EDGE_{i}" for i in range(10)]
        assert [e["name"] for e in processed["edge_types"]] == expected_names

    def test_default_fields_and_truncation(self, generator: OntologyGenerator):
        """Missing root fields should be defaulted, and long descriptions truncated to 100 chars."""
        long_desc = "x" * 150
        raw_result = {
            "entity_types": [{"name": "User", "description": long_desc}],
            "edge_types": [{"name": "FOLLOWS", "description": long_desc}],
        }

        processed = generator._validate_and_process(raw_result)

        assert processed["analysis_summary"] == ""
        user_entity = next(e for e in processed["entity_types"] if e["name"] == "User")
        assert len(user_entity["description"]) == 100
        assert user_entity["description"].endswith("...")
        assert user_entity["attributes"] == []
        assert user_entity["examples"] == []

        edge = processed["edge_types"][0]
        assert len(edge["description"]) == 100
        assert edge["description"].endswith("...")
        assert edge["source_targets"] == []
        assert edge["attributes"] == []


def _generator_for_test() -> OntologyGenerator:
    generator = OntologyGenerator(llm_client=object())
    generator.MAX_TEXT_LENGTH_FOR_LLM = 2000
    generator.LONG_TEXT_CHUNK_SIZE = 500
    generator.LONG_TEXT_CHUNK_OVERLAP = 0
    generator.MAX_LONG_TEXT_CHUNKS = 3
    generator.MIN_LONG_TEXT_EXCERPT = 120
    return generator


def test_short_ontology_context_keeps_original_text():
    generator = _generator_for_test()

    context = generator._build_document_context(["short document body"])

    assert context == "short document body"
    assert "[Long Document Automatic Chunk Summary]" not in context


def test_long_ontology_context_samples_across_document():
    generator = _generator_for_test()
    long_text = "BEGIN" + ("a" * 1050) + "MIDDLE" + ("b" * 1050) + "END"

    context = generator._build_document_context([long_text])

    assert len(context) <= generator.MAX_TEXT_LENGTH_FOR_LLM
    assert "[Long Document Automatic Chunk Summary]" in context
    assert "BEGIN" in context
    assert "MIDDLE" in context
    assert "END" in context
    assert "Chunk 1/" in context
    assert "Chunk 3/" in context
    assert "Chunk 5/" in context


def test_very_long_ontology_context_selects_representative_chunks():
    generator = _generator_for_test()
    chunks = ["BEGIN"] + [
        f"CHUNK{i:02d}-" + (str(i) * 490)
        for i in range(12)
    ] + ["FINALEND"]
    long_text = "".join(chunks)

    context = generator._build_document_context([long_text])

    assert len(context) <= generator.MAX_TEXT_LENGTH_FOR_LLM
    assert "BEGIN" in context
    assert "FINALEND" in context
    assert context.count("--- Document 1 / Chunk") == generator.MAX_LONG_TEXT_CHUNKS


def test_ontology_context_compression_loop_reduces_excerpt_budget():
    generator = _generator_for_test()
    generator.MAX_TEXT_LENGTH_FOR_LLM = 500
    generator.MIN_LONG_TEXT_EXCERPT = 40
    generator.MAX_LONG_TEXT_CHUNKS = 1

    render_calls = []
    original_render = generator._render_chunked_context

    def spy_render(*args, **kwargs):
        render_calls.append(kwargs["excerpt_limit"])
        return original_render(*args, **kwargs)

    generator._render_chunked_context = spy_render

    with pytest.MonkeyPatch().context() as mp:
        mp.setattr(generator, "_calculate_excerpt_budget", lambda count: 150)
        context = generator._build_document_context(["A" * 2000])

    assert len(render_calls) > 1
    assert render_calls[0] > render_calls[1]
    assert len(context) <= generator.MAX_TEXT_LENGTH_FOR_LLM
    assert "...(chunked context compressed within ontology analysis length limit)..." not in context


def test_ontology_context_fallback_truncation_when_exceeding_limit():
    generator = _generator_for_test()
    generator.MAX_TEXT_LENGTH_FOR_LLM = 350
    generator.MIN_LONG_TEXT_EXCERPT = 40
    generator.MAX_LONG_TEXT_CHUNKS = 1

    long_text = "BEGIN" + ("x" * 2000) + "END"
    context = generator._build_document_context([long_text])

    assert len(context) == generator.MAX_TEXT_LENGTH_FOR_LLM
    assert "...(chunked context compressed within ontology analysis length limit)..." in context


def test_multiple_documents_tracking_across_documents_and_chunks():
    generator = _generator_for_test()
    generator.MAX_LONG_TEXT_CHUNKS = 4

    doc1 = "DOC1_START " + ("1" * 1200) + " DOC1_END"
    doc2 = "DOC2_START " + ("2" * 1200) + " DOC2_END"

    chunks = generator._collect_document_chunks([doc1, doc2])
    assert len(chunks) == 6

    doc1_chunks = [c for c in chunks if c["document_index"] == 1]
    doc2_chunks = [c for c in chunks if c["document_index"] == 2]

    assert len(doc1_chunks) == 3
    assert [c["chunk_index"] for c in doc1_chunks] == [1, 2, 3]
    assert all(c["total_document_chunks"] == 3 for c in doc1_chunks)

    assert len(doc2_chunks) == 3
    assert [c["chunk_index"] for c in doc2_chunks] == [1, 2, 3]
    assert all(c["total_document_chunks"] == 3 for c in doc2_chunks)

    context = generator._build_document_context([doc1, doc2])
    assert len(context) <= generator.MAX_TEXT_LENGTH_FOR_LLM
    assert "[Long Document Automatic Chunk Summary]" in context
    assert "--- Document 1 / Chunk" in context
    assert "--- Document 2 / Chunk" in context


def test_empty_and_whitespace_documents_context():
    generator = _generator_for_test()

    assert generator._build_document_context([]) == ""
    assert generator._build_document_context(["", "   "]) == "\n\n---\n\n   "

    long_whitespace = "   " * 1000
    assert len(long_whitespace) > generator.MAX_TEXT_LENGTH_FOR_LLM
    assert generator._build_document_context([long_whitespace]) == ""


def test_exact_boundary_at_max_text_length():
    generator = _generator_for_test()

    exact_text = "x" * generator.MAX_TEXT_LENGTH_FOR_LLM
    context_exact = generator._build_document_context([exact_text])
    assert context_exact == exact_text
    assert "[Long Document Automatic Chunk Summary]" not in context_exact

    plus_one_text = "x" * (generator.MAX_TEXT_LENGTH_FOR_LLM + 1)
    context_plus_one = generator._build_document_context([plus_one_text])
    assert "[Long Document Automatic Chunk Summary]" in context_plus_one
    assert len(context_plus_one) <= generator.MAX_TEXT_LENGTH_FOR_LLM


def test_max_long_text_chunks_one_edge_case():
    generator = _generator_for_test()
    generator.MAX_LONG_TEXT_CHUNKS = 1

    long_text = "BEGIN " + ("A" * 3000) + " END"
    chunks = generator._collect_document_chunks([long_text])
    assert len(chunks) > 1

    selected = generator._select_representative_chunks(chunks)
    assert len(selected) == 1
    assert selected[0]["chunk_index"] == 1

    context = generator._build_document_context([long_text])
    assert "[Long Document Automatic Chunk Summary]" in context
    assert context.count("--- Document 1 / Chunk") == 1
    assert len(context) <= generator.MAX_TEXT_LENGTH_FOR_LLM


def test_build_user_message_integrates_context_and_requirements():
    generator = _generator_for_test()

    msg_short = generator._build_user_message(
        ["Short doc body"],
        "Simulate public reaction",
        "Focus on local policies",
    )
    assert "## Simulation Requirements\n\nSimulate public reaction" in msg_short
    assert "## Document Content\n\nShort doc body" in msg_short
    assert "## Additional Explanation\n\nFocus on local policies" in msg_short
    assert "**Rules to follow**:" in msg_short
    assert "[Long Document Automatic Chunk Summary]" not in msg_short

    long_doc = "BEGIN " + ("B" * 2500) + " END"
    msg_long = generator._build_user_message(
        [long_doc],
        "Simulate market trends",
        None,
    )
    assert "## Simulation Requirements\n\nSimulate market trends" in msg_long
    assert "[Long Document Automatic Chunk Summary]" in msg_long
    assert "## Additional Explanation" not in msg_long
    assert "**Rules to follow**:" in msg_long
