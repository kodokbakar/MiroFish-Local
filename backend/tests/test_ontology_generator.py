from app.services.ontology_generator import OntologyGenerator
import pytest

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
