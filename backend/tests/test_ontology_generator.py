from app.services.ontology_generator import OntologyGenerator


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
