"""Ordered, bounded NER extraction and serial graph persistence."""

import logging
import threading
import time
from unittest.mock import Mock

import pytest

from app.services.graph_builder import GraphBuilderService
from app.storage.neo4j_storage import Neo4jStorage
from app.storage.ner_extractor import NERExtractionError, NERExtractor


def test_serial_mode_uses_add_text_and_preserves_order():
    storage = Mock()
    storage.add_text.side_effect = ["ep-0", "ep-1", "ep-2"]
    progress = []
    result = GraphBuilderService(storage).add_text_batches(
        "graph", ["0", "1", "2"], ontology={"entity_types": []},
        max_workers=1, progress_callback=lambda message, ratio: progress.append(ratio),
    )
    assert result == ["ep-0", "ep-1", "ep-2"]
    assert [call.args[1] for call in storage.add_text.call_args_list] == ["0", "1", "2"]
    assert storage.extract_text.call_count == 0
    assert progress[-1] == 1.0


def test_parallel_extraction_overlaps_but_persists_in_input_order():
    first_started = threading.Event()
    second_done = threading.Event()
    caller = threading.get_ident()
    completion = []
    persisted = []

    class Storage:
        def extract_text(self, text, ontology):
            if text == "0":
                first_started.set()
                assert second_done.wait(2)
            elif text == "1":
                assert first_started.wait(2)
                second_done.set()
            completion.append(text)
            return {"entities": [], "relations": []}

        def persist_extraction(self, graph_id, text, extraction):
            assert threading.get_ident() == caller
            assert graph_id == "graph"
            assert extraction == {"entities": [], "relations": []}
            persisted.append(text)
            return f"ep-{text}"

    ids = GraphBuilderService(Storage()).add_text_batches(
        "graph", ["0", "1", "2", "3"], ontology={}, max_workers=2,
    )
    assert completion.index("1") < completion.index("0")
    assert persisted == ["0", "1", "2", "3"]
    assert ids == ["ep-0", "ep-1", "ep-2", "ep-3"]


def test_pending_extractions_are_bounded():
    release_first = threading.Event()
    started_first = threading.Event()
    lock = threading.Lock()
    extracted = []
    failure = []

    class Storage:
        def extract_text(self, text, ontology):
            with lock:
                extracted.append(text)
            if text == "0":
                started_first.set()
                assert release_first.wait(3)
            return {"entities": [], "relations": []}

        def persist_extraction(self, graph_id, text, extraction):
            return f"ep-{text}"

    def run():
        try:
            GraphBuilderService(Storage()).add_text_batches(
                "graph", [str(index) for index in range(30)], ontology={}, max_workers=2,
            )
        except Exception as error:
            failure.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    assert started_first.wait(2)
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        with lock:
            if len(extracted) == 4:
                break
        time.sleep(0.01)
    with lock:
        assert len(extracted) <= 4
    release_first.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert not failure
    assert len(extracted) == 30


def test_parallel_extraction_failure_prevents_later_persistence():
    persisted = []

    class Storage:
        def extract_text(self, text, ontology):
            if text == "1":
                raise NERExtractionError("NER extraction failed (HTTP 429)")
            return {"entities": [], "relations": []}

        def persist_extraction(self, graph_id, text, extraction):
            persisted.append(text)
            return f"ep-{text}"

    with pytest.raises(NERExtractionError):
        GraphBuilderService(Storage()).add_text_batches(
            "graph", ["0", "1", "2", "3"], ontology={}, max_workers=2,
        )
    assert persisted == ["0"]


def test_neo4j_add_text_delegates_to_extraction_then_persistence():
    storage = Neo4jStorage.__new__(Neo4jStorage)
    storage.get_ontology = Mock(return_value={"entity_types": ["Person"]})
    extraction = {"entities": [], "relations": []}
    storage.extract_text = Mock(return_value=extraction)
    storage.persist_extraction = Mock(return_value="episode")
    assert storage.add_text("graph", "source text") == "episode"
    storage.extract_text.assert_called_once_with("source text", {"entity_types": ["Person"]})
    storage.persist_extraction.assert_called_once_with("graph", "source text", extraction)


def test_invalid_extraction_cannot_create_an_episode():
    storage = Neo4jStorage.__new__(Neo4jStorage)
    storage._driver = Mock()
    with pytest.raises(ValueError, match="Invalid NER extraction"):
        storage.persist_extraction("graph", "text", {"entities": None, "relations": []})
    storage._driver.session.assert_not_called()


@pytest.mark.parametrize("workers", [0, 5, -1, True, 1.5])
def test_worker_limit_is_validated(workers):
    with pytest.raises(ValueError, match="between 1 and 4"):
        GraphBuilderService(Mock()).add_text_batches("graph", ["text"], max_workers=workers)


class FakeProviderError(Exception):
    def __init__(self, status_code):
        super().__init__("private provider body")
        self.status_code = status_code


def test_ner_retries_transient_failures_then_succeeds(monkeypatch):
    calls = []

    class Client:
        def chat_json(self, **kwargs):
            calls.append(1)
            if len(calls) < 3:
                raise FakeProviderError(429)
            return {"entities": [], "relations": []}

    monkeypatch.setattr("app.storage.ner_extractor.time.sleep", lambda seconds: None)
    result = NERExtractor(Client(), max_retries=2).extract("text", {})
    assert result == {"entities": [], "relations": []}
    assert len(calls) == 3


def test_ner_permanent_failure_is_not_retried_or_logged(caplog):
    client = Mock()
    client.chat_json.side_effect = FakeProviderError(401)
    with caplog.at_level(logging.WARNING):
        with pytest.raises(NERExtractionError, match="HTTP 401"):
            NERExtractor(client, max_retries=2).extract("private text", {})
    assert client.chat_json.call_count == 1
    assert "private provider body" not in caplog.text
    assert "private text" not in caplog.text


def test_ner_exhaustion_and_malformed_output_fail_clearly(monkeypatch):
    monkeypatch.setattr("app.storage.ner_extractor.time.sleep", lambda seconds: None)
    client = Mock()
    client.chat_json.side_effect = FakeProviderError(503)
    with pytest.raises(NERExtractionError, match="HTTP 503"):
        NERExtractor(client, max_retries=2).extract("text", {})
    assert client.chat_json.call_count == 3

    client.chat_json.side_effect = None
    client.chat_json.return_value = {"entities": [], "relations": None}
    with pytest.raises(NERExtractionError, match="ValueError"):
        NERExtractor(client, max_retries=0).extract("text", {})


def test_ner_timeout_has_bounded_retry(monkeypatch):
    monkeypatch.setattr("app.storage.ner_extractor.time.sleep", lambda seconds: None)
    client = Mock()
    client.chat_json.side_effect = TimeoutError("provider timed out")
    with pytest.raises(NERExtractionError, match="TimeoutError"):
        NERExtractor(client, max_retries=1).extract("text", {})
    assert client.chat_json.call_count == 2


def test_graph_builder_logs_no_chunk_text(caplog):
    storage = Mock()
    storage.add_text.return_value = "episode"
    with caplog.at_level(logging.INFO, logger="mirofish.graph_builder"):
        GraphBuilderService(storage).add_text_batches(
            "graph", ["SECRET-CHUNK-CONTENT"], ontology={}, max_workers=1,
        )
    assert "SECRET-CHUNK-CONTENT" not in caplog.text
