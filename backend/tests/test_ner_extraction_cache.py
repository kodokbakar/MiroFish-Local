"""Regression tests for successful NER extraction caching (issue #58)."""

import json
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, patch

import pytest

from app.storage.extraction_cache import ExtractionCache
from app.storage.ner_extractor import NERExtractor, NERExtractionError
from app.storage.neo4j_storage import Neo4jStorage
from tests.test_neo4j_storage_ingestion import InMemoryNeo4jDriver


RESULT = {
    "entities": [
        {"name": "Alice", "type": "Person", "attributes": {}},
        {"name": "Bob", "type": "Person", "attributes": {}},
    ],
    "relations": [
        {"source": "Alice", "target": "Bob", "type": "KNOWS", "fact": "Alice knows Bob"},
    ],
}
ONTOLOGY = {"entity_types": [{"name": "Person"}], "relation_types": [{"name": "KNOWS"}]}


class FakeLLM:
    base_url = "http://localhost:11434/v1"
    model = "test-model"
    _num_ctx = 8192
    api_key = "SECRET_API_KEY"

    def __init__(self, result=RESULT):
        self.result = result
        self.calls = 0

    def chat_json(self, **kwargs):
        self.calls += 1
        return self.result


def test_second_extraction_hits_cache_across_instances(tmp_path):
    llm = FakeLLM()
    first = NERExtractor(llm, cache_dir=tmp_path)
    assert first.extract("Alice knows Bob", ONTOLOGY) == RESULT
    second = NERExtractor(llm, cache_dir=tmp_path)
    assert second.extract("Alice knows Bob", ONTOLOGY) == RESULT
    assert llm.calls == 1
    assert second.cache.stats() == {"hits": 1, "misses": 0}
    entry = next(tmp_path.glob("*.json"))
    assert '"text":' not in entry.read_text()
    assert llm.api_key not in entry.read_text()
    assert oct(entry.stat().st_mode & 0o777) == "0o600"


def test_key_invalidation_covers_text_ontology_provider_model_and_prompt(tmp_path, monkeypatch):
    import app.storage.ner_extractor as ner_module

    llm = FakeLLM()
    extractor = NERExtractor(llm, cache_dir=tmp_path)
    extractor.extract("Alice knows Bob", ONTOLOGY)
    extractor.extract("Different text", ONTOLOGY)
    extractor.extract("Alice knows Bob", {"entity_types": [{"name": "Other"}]})
    llm.model = "other-model"
    extractor.extract("Alice knows Bob", ONTOLOGY)
    llm.base_url = "https://other.example/v1"
    extractor.extract("Alice knows Bob", ONTOLOGY)
    monkeypatch.setattr(ner_module, "_USER_PROMPT", ner_module._USER_PROMPT + "\nNew template")
    extractor.extract("Alice knows Bob", ONTOLOGY)
    monkeypatch.setattr(ner_module, "EXTRACTION_TEMPERATURE", 0.2)
    extractor.extract("Alice knows Bob", ONTOLOGY)
    assert llm.calls == 7


@pytest.mark.parametrize("corrupt", [
    "{broken",
    json.dumps({"schema": 999, "key": "x", "created_at": time.time(), "result": RESULT}),
    json.dumps({"schema": 1, "key": "x", "created_at": time.time(), "result": {"entities": [], "relations": "bad"}}),
])
def test_corrupt_or_incompatible_entry_is_a_miss(tmp_path, corrupt):
    llm = FakeLLM()
    extractor = NERExtractor(llm, cache_dir=tmp_path)
    key = extractor._cache_key("Alice knows Bob", ONTOLOGY)
    (tmp_path / f"{key}.json").write_text(corrupt)
    assert extractor.extract("Alice knows Bob", ONTOLOGY) == RESULT
    assert llm.calls == 1
    assert extractor.cache.stats()["misses"] == 1


def test_failed_or_incomplete_extraction_is_not_cached(tmp_path):
    class FlakyLLM(FakeLLM):
        def chat_json(self, **kwargs):
            self.calls += 1
            if self.calls <= 2:
                raise TimeoutError("provider failed with sensitive content")
            return RESULT

    llm = FlakyLLM()
    extractor = NERExtractor(llm, max_retries=1, cache_dir=tmp_path)
    with pytest.raises(NERExtractionError, match="NER extraction failed"):
        extractor.extract("Alice knows Bob", ONTOLOGY)
    assert not list(tmp_path.glob("*.json"))
    assert extractor.extract("Alice knows Bob", ONTOLOGY) == RESULT
    assert llm.calls == 3

    invalid = FakeLLM({"entities": []})
    invalid_extractor = NERExtractor(invalid, max_retries=0, cache_dir=tmp_path / "invalid")
    with pytest.raises(NERExtractionError, match="NER extraction failed"):
        invalid_extractor.extract("incomplete", ONTOLOGY)
    assert not list((tmp_path / "invalid").glob("*.json"))


@pytest.mark.parametrize("attributes", [None, ["invalid"]])
def test_null_or_invalid_attributes_keep_previous_empty_dict_behavior(tmp_path, attributes):
    llm = FakeLLM({
        "entities": [{"name": "Alice", "type": "Person", "attributes": attributes}],
        "relations": [],
    })
    extractor = NERExtractor(llm, cache_dir=tmp_path)
    expected = {"entities": [{"name": "Alice", "type": "Person", "attributes": {}}], "relations": []}
    assert extractor.extract("Alice arrives", ONTOLOGY) == expected
    assert extractor.extract("Alice arrives", ONTOLOGY) == expected
    assert llm.calls == 1


@pytest.mark.parametrize("invalid_result", [
    {"entities": [{}], "relations": []},
    {"entities": [], "relations": [{"source": "Alice"}]},
    {"entities": ["not a record"], "relations": []},
])
def test_incomplete_raw_records_fail_instead_of_becoming_cached_empty(tmp_path, invalid_result):
    llm = FakeLLM(invalid_result)
    extractor = NERExtractor(llm, max_retries=1, cache_dir=tmp_path)
    with pytest.raises(NERExtractionError, match="NER extraction failed"):
        extractor.extract("Alice arrives", ONTOLOGY)
    assert llm.calls == 2
    assert not list(tmp_path.glob("*.json"))


def test_unknown_injected_provider_identity_bypasses_cache(tmp_path):
    class UnknownLLM:
        def __init__(self):
            self.calls = 0

        def chat_json(self, **kwargs):
            self.calls += 1
            return RESULT

    llm = UnknownLLM()
    extractor = NERExtractor(llm, cache_dir=tmp_path)
    extractor.extract("Alice knows Bob", ONTOLOGY)
    extractor.extract("Alice knows Bob", ONTOLOGY)
    assert llm.calls == 2
    assert not list(tmp_path.glob("*.json"))


def test_concurrent_writers_publish_complete_entries(tmp_path):
    class SlowLLM(FakeLLM):
        def chat_json(self, **kwargs):
            time.sleep(0.01)
            return super().chat_json(**kwargs)

    llm = SlowLLM()
    extractor = NERExtractor(llm, cache_dir=tmp_path)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: extractor.extract("Alice knows Bob", ONTOLOGY), range(16)))
    assert results == [RESULT] * 16
    entries = list(tmp_path.glob("*.json"))
    assert len(entries) == 1
    assert json.loads(entries[0].read_text())["result"] == RESULT
    assert not list(tmp_path.glob("*.tmp"))


def test_cache_hit_still_creates_graph_specific_episodes_and_embeddings(tmp_path):
    llm = FakeLLM()
    extractor = NERExtractor(llm, cache_dir=tmp_path)
    driver = InMemoryNeo4jDriver()
    embedding = MagicMock()
    embedding.embed_batch.side_effect = lambda texts: [[0.1, 0.2] for _ in texts]
    with patch("app.storage.neo4j_storage.GraphDatabase.driver", return_value=driver):
        storage = Neo4jStorage(
            uri="bolt://localhost:7687", user="neo4j", password="password",
            embedding_service=embedding, ner_extractor=extractor,
        )
    first_id = storage.add_text("graph-one", "Alice knows Bob", ontology=ONTOLOGY)
    second_id = storage.add_text("graph-two", "Alice knows Bob", ontology=ONTOLOGY)
    assert first_id != second_id
    assert llm.calls == 1
    assert embedding.embed_batch.call_count == 2
    assert {episode["graph_id"] for episode in driver.episodes} == {"graph-one", "graph-two"}
    assert ("graph-one", "alice") in driver.entities
    assert ("graph-two", "alice") in driver.entities


def test_failed_extraction_is_not_cached_or_persisted(tmp_path):
    class FailedLLM(FakeLLM):
        def chat_json(self, **kwargs):
            self.calls += 1
            raise ValueError("provider response failed")

    llm = FailedLLM()
    extractor = NERExtractor(llm, max_retries=0, cache_dir=tmp_path)
    driver = InMemoryNeo4jDriver()
    embedding = MagicMock()
    with patch("app.storage.neo4j_storage.GraphDatabase.driver", return_value=driver):
        storage = Neo4jStorage(
            uri="bolt://localhost:7687", user="neo4j", password="password",
            embedding_service=embedding, ner_extractor=extractor,
        )
    # The extraction API used by parallel builds preserves the failure signal.
    with pytest.raises(NERExtractionError):
        extractor.extract("failed chunk", ONTOLOGY)
    assert not list(tmp_path.glob("*.json"))

    # Failed extraction aborts ingestion instead of persisting an empty episode.
    with pytest.raises(NERExtractionError):
        storage.add_text("graph-one", "failed chunk", ontology=ONTOLOGY)
    assert not driver.episodes
    assert not driver.entities
    assert llm.calls == 2
    assert not list(tmp_path.glob("*.json"))


def test_retention_limit_removes_oldest_entry(tmp_path):
    cache = ExtractionCache(tmp_path, max_entries=2)
    value = {"entities": [], "relations": []}
    for key in ("a" * 64, "b" * 64, "c" * 64):
        cache.put(key, value)
    assert len(list(tmp_path.glob("*.json"))) <= 2
