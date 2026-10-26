import io
import re

from app import create_app
from app.api import graph as graph_api
from app.models.project import ProjectManager, ProjectStatus
from app.utils.llm_client import LLMResponseError


def _post_ontology(client):
    return client.post(
        "/api/graph/ontology/generate",
        data={
            "simulation_requirement": "Simulate the discussion.",
            "files": (io.BytesIO(b"A short source document."), "source.md"),
        },
        content_type="multipart/form-data",
    )


def test_ontology_api_returns_safe_truncation_error_and_failed_project(
    tmp_path,
    monkeypatch,
):
    class FailingGenerator:
        def generate(self, **kwargs):
            raise LLMResponseError(
                "LLM JSON output was truncated at the token limit",
                finish_reason="length",
            )

    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(graph_api, "OntologyGenerator", FailingGenerator)

    app = create_app()
    app.config.update(TESTING=True)
    response = _post_ontology(app.test_client())

    assert response.status_code == 502
    assert response.json["success"] is False
    assert "token limit" in response.json["error"]
    assert "traceback" not in response.json

    project_id = response.json["data"]["project_id"]
    project = ProjectManager.get_project(project_id)
    assert project.status == ProjectStatus.FAILED
    assert project.error == response.json["error"]


def test_ontology_api_does_not_expose_provider_error_body(tmp_path, monkeypatch):
    class ProviderError(RuntimeError):
        status_code = 401
        request_id = "request-safe-id"
        body = {"error": {"message": "SECRET-PROVIDER-BODY"}}

    class FailingGenerator:
        def generate(self, **kwargs):
            raise ProviderError("SECRET-PROVIDER-BODY")

    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(graph_api, "OntologyGenerator", FailingGenerator)

    app = create_app()
    app.config.update(TESTING=True)
    response = _post_ontology(app.test_client())

    assert response.status_code == 502
    assert "HTTP 401" in response.json["error"]
    assert "request-safe-id" in response.json["error"]
    assert "SECRET-PROVIDER-BODY" not in response.get_data(as_text=True)
    assert "traceback" not in response.json


def test_ontology_api_sanitizes_request_id_and_truncates(tmp_path, monkeypatch):
    dirty_request_id = "req_123<script>alert('xss')</script>\r\nDrop Table; -- " + ("x" * 150)
    expected_sanitized = re.sub(r"[^a-zA-Z0-9._:-]", "", dirty_request_id)[:128]

    class ProviderError(RuntimeError):
        status_code = 502
        request_id = dirty_request_id
        body = {"error": {"message": "Bad Gateway from upstream"}}

    class FailingGenerator:
        def generate(self, **kwargs):
            raise ProviderError("Bad Gateway")

    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(graph_api, "OntologyGenerator", FailingGenerator)

    app = create_app()
    app.config.update(TESTING=True)
    response = _post_ontology(app.test_client())

    assert response.status_code == 502
    assert response.json["success"] is False
    assert "HTTP 502" in response.json["error"]
    assert expected_sanitized in response.json["error"]
    assert len(expected_sanitized) == 128
    raw_response = response.get_data(as_text=True)
    assert "<script>" not in raw_response
    assert "\r" not in raw_response
    assert "\n" not in response.json["error"]
    assert "Drop Table" not in raw_response

    project_id = response.json["data"]["project_id"]
    project = ProjectManager.get_project(project_id)
    assert project.status == ProjectStatus.FAILED
    assert project.error == response.json["error"]


def test_ontology_api_handles_provider_error_without_request_id(tmp_path, monkeypatch):
    class ProviderError(RuntimeError):
        status_code = 502
        request_id = None
        body = {"error": {"message": "Bad Gateway"}}

    class FailingGenerator:
        def generate(self, **kwargs):
            raise ProviderError("Bad Gateway")

    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(graph_api, "OntologyGenerator", FailingGenerator)

    app = create_app()
    app.config.update(TESTING=True)
    response = _post_ontology(app.test_client())

    assert response.status_code == 502
    assert response.json["error"] == "LLM provider request failed (HTTP 502)"


def test_ontology_api_handles_generic_exception_as_500(tmp_path, monkeypatch):
    class FailingGenerator:
        def generate(self, **kwargs):
            raise RuntimeError("Database connection suddenly dropped: password=supersecret")

    monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path))
    monkeypatch.setattr(graph_api, "OntologyGenerator", FailingGenerator)

    app = create_app()
    app.config.update(TESTING=True)
    response = _post_ontology(app.test_client())

    assert response.status_code == 500
    assert response.json["success"] is False
    assert response.json["error"] == "Ontology generation failed; check the server logs"
    assert "supersecret" not in response.get_data(as_text=True)
    assert "traceback" not in response.json

    project_id = response.json["data"]["project_id"]
    project = ProjectManager.get_project(project_id)
    assert project.status == ProjectStatus.FAILED
