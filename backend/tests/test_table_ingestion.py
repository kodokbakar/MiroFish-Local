"""Regression coverage for CSV and JSON project uploads and table chunking."""

import io

import pytest

from app import create_app
from app.api import graph as graph_api
from app.models.project import ProjectManager
from app.services.text_processor import TextProcessor
from app.utils.file_parser import FileParser, split_text_into_chunks


@pytest.mark.parametrize("filename,content,expected", [
    ("data.csv", 'name,note\nAlice,"one,two"\nBob,"first\nsecond"\n', 'note: "first\\nsecond"'),
    ("data.json", '[{"name":"Alice","score":1},{"name":"Bob","score":2}]', 'score: 2'),
    ("data.json", '{"records":[{"name":"Alice"}]}', 'name: "Alice"'),
])
def test_parse_tables_keep_fields_and_quoted_records(tmp_path, filename, content, expected):
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    text = FileParser.extract_text(str(path))
    assert "Source: data." in text
    assert "Columns:" in text
    assert expected in text


@pytest.mark.parametrize("filename,content", [
    ("bad.csv", ""),
    ("bad.csv", "name,note\nAlice\n"),
    ("bad.csv", 'name,note\nAlice,"unterminated\n'),
    ("bad.csv", "name,name\nA,B\n"),
    ("bad.json", ""),
    ("bad.json", "{bad}"),
    ("bad.json", '{"foo":1}'),
    ("bad.json", '[1,2]'),
])
def test_invalid_tables_raise_clear_error(tmp_path, filename, content):
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ValueError):
        FileParser.extract_text(str(path))


def test_table_chunks_repeat_headers_and_preserve_normal_rows(tmp_path):
    path = tmp_path / "people.csv"
    path.write_text('name,note\nAlice,"one,two"\nBob,"first\nsecond"\nCara,three\n')
    headers, records = FileParser.parse_table(str(path))
    chunks = TextProcessor.split_table("people.csv", headers, records, 100)
    assert len(chunks) >= 2
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert all(chunk.startswith("Source: people.csv\nColumns: name, note\n") for chunk in chunks)
    assert all(sum(f"Record {number}: " in chunk for chunk in chunks) == 1 for number in range(1, 4))
    assert sum("Record 2: name: \"Bob\", note: \"first\\nsecond\"" in chunk for chunk in chunks) == 1


def test_oversized_record_stays_bounded_with_field_context():
    chunks = TextProcessor.split_table("large.json", ["name", "bio"], [{"name": "A", "bio": "x" * 400}], 100)
    assert len(chunks) > 2
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert all("Columns:" in chunk for chunk in chunks)
    assert all("Record 1" in chunk for chunk in chunks)
    assert all('name: "A"' in chunk for chunk in chunks if "bio (part" in chunk)
    assert sum(chunk.count("x") for chunk in chunks) == 400


def test_wide_header_is_partitioned_into_relevant_columns():
    headers = [f"field_{number}" for number in range(20)]
    record = {key: number for number, key in enumerate(headers)}
    chunks = TextProcessor.split_table("wide.csv", headers, [record], 100)
    assert len(chunks) >= len(headers)
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert all("Source: wide.csv\nColumns:" in chunk for chunk in chunks)
    assert all(f"{key} (part 1): {number}" in "\n".join(chunks) for number, key in enumerate(headers))


def test_long_column_name_uses_compact_source_and_keeps_full_name():
    key = "column_" + "x" * 63
    chunks = TextProcessor.split_table("dataset.csv", [key], [{key: "value"}], 100)
    assert chunks
    assert all(len(chunk) <= 100 for chunk in chunks)
    assert all(key in chunk and "S:dataset." in chunk for chunk in chunks)
    assert "".join(chunk.rsplit(": ", 1)[1] for chunk in chunks) == '"value"'


@pytest.mark.parametrize("filename,content", [
    ("people.csv", b"name,role\nAlice,admin\nBob,user\n"),
    ("people.json", b'[{"name":"Alice","role":"admin"},{"name":"Bob","role":"user"}]'),
])
def test_upload_table_and_split_saved_project_files(tmp_path, monkeypatch, filename, content):
    class Generator:
        def generate(self, **kwargs):
            assert "name: \"Alice\"" in kwargs["document_texts"][0]
            return {"entity_types": [], "edge_types": [], "analysis_summary": "ok"}

    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(graph_api, "OntologyGenerator", Generator)
    app = create_app()
    app.config.update(TESTING=True)
    response = app.test_client().post(
        "/api/graph/ontology/generate",
        data={"simulation_requirement": "Discuss data", "files": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 200
    project_id = response.json["data"]["project_id"]
    project = ProjectManager.get_project(project_id)
    chunks = TextProcessor.split_project_files(project_id, project.files, 100, 10)
    assert "name" in chunks[0] and "Alice" in chunks[0]
    assert "role" in chunks[0]

    received = []

    class Builder:
        def __init__(self, storage):
            pass

        def create_graph(self, name):
            return "graph_1"

        def set_ontology(self, graph_id, ontology):
            pass

        def add_text_batches(self, graph_id, chunks, **kwargs):
            received.extend(chunks)
            return []

        def get_graph_data(self, graph_id):
            return {"node_count": 0, "edge_count": 0}

    class InlineThread:
        def __init__(self, target, daemon):
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(graph_api, "_get_storage", lambda: object())
    monkeypatch.setattr(graph_api, "GraphBuilderService", Builder)
    monkeypatch.setattr(graph_api.threading, "Thread", InlineThread)
    build = app.test_client().post(
        "/api/graph/build",
        json={"project_id": project_id, "chunk_size": 100, "chunk_overlap": 10},
    )
    assert build.status_code == 200
    assert received == chunks


@pytest.mark.parametrize("filename,content,expected", [
    ("bad.json", b"{bad}", "Malformed JSON"),
    ("bad.csv", b'name,note\nAlice,"unterminated', "Malformed CSV"),
    ("empty.csv", b"", "Empty table file"),
])
def test_invalid_upload_returns_400_without_calling_llm(tmp_path, monkeypatch, filename, content, expected):
    class Generator:
        def generate(self, **kwargs):
            pytest.fail("invalid input reached LLM")

    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(graph_api, "OntologyGenerator", Generator)
    app = create_app()
    app.config.update(TESTING=True)
    response = app.test_client().post(
        "/api/graph/ontology/generate",
        data={"simulation_requirement": "Discuss data", "files": (io.BytesIO(content), filename)},
        content_type="multipart/form-data",
    )
    assert response.status_code == 400
    assert expected in response.json["error"]
    assert list(tmp_path.iterdir()) == []


def test_prose_chunking_is_unchanged():
    text = "A normal prose document. " * 30
    assert TextProcessor.split_text(text, 100, 10) == split_text_into_chunks(text, 100, 10)
