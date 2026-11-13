"""Tests for Issue #70: /api/status telemetry and infrastructure health check."""

import os
from unittest.mock import MagicMock, patch

import pytest
from app import create_app


@pytest.fixture
def app():
    app = create_app()
    app.config["TESTING"] = True
    return app


@pytest.fixture
def client(app):
    with app.test_client() as client:
        yield client


def test_api_status_ok_when_components_healthy(app, client):
    """When Neo4j, LLM, and disk are healthy, /api/status returns HTTP 200 with status ok."""
    mock_storage = MagicMock()
    mock_storage.check_health.return_value = {
        "status": "ok",
        "latency_ms": 5.2,
        "indexes": {"online": 9, "populating": 0, "failed": 0},
    }
    app.extensions["neo4j_storage"] = mock_storage

    mock_llm_check = {
        "status": "ok",
        "endpoint": "http://localhost:11434/v1",
        "latency_ms": 12.4,
        "model": "qwen2.5:32b",
    }

    mock_disk = {
        "status": "ok",
        "free_gb": 100.5,
        "total_gb": 500.0,
        "used_percent": 79.9,
    }

    with patch("app.api.status.probe_llm_endpoint", return_value=mock_llm_check), \
         patch("app.api.status.check_disk_usage", return_value=mock_disk):
        response = client.get("/api/status")

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "ok"
        assert data["components"]["neo4j"]["status"] == "ok"
        assert data["components"]["llm"]["status"] == "ok"
        assert data["components"]["disk"]["status"] == "ok"
        # No credentials exposed
        assert "password" not in str(data).lower()
        assert "api_key" not in str(data).lower()


def test_api_status_503_when_neo4j_unreachable(app, client):
    """When Neo4j is offline/unreachable, /api/status returns HTTP 503 unavailable."""
    mock_storage = MagicMock()
    mock_storage.check_health.return_value = {
        "status": "unavailable",
        "error": "Connection refused to Neo4j Bolt",
    }
    app.extensions["neo4j_storage"] = mock_storage

    with patch("app.api.status.probe_llm_endpoint", return_value={"status": "ok"}), \
         patch("app.api.status.check_disk_usage", return_value={"status": "ok"}):
        response = client.get("/api/status")

        assert response.status_code == 503
        data = response.get_json()
        assert data["status"] == "unavailable"
        assert data["components"]["neo4j"]["status"] == "unavailable"
        assert "Connection refused" in data["components"]["neo4j"]["error"]


def test_api_status_degraded_when_llm_or_indexes_degraded(app, client):
    """When LLM is unreachable or index populating, status is degraded with HTTP 200."""
    mock_storage = MagicMock()
    mock_storage.check_health.return_value = {
        "status": "degraded",
        "indexes": {"online": 8, "populating": 1, "failed": 0},
    }
    app.extensions["neo4j_storage"] = mock_storage

    mock_llm_check = {
        "status": "unavailable",
        "error": "LLM endpoint timeout",
    }

    with patch("app.api.status.probe_llm_endpoint", return_value=mock_llm_check), \
         patch("app.api.status.check_disk_usage", return_value={"status": "ok"}):
        response = client.get("/api/status")

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "degraded"
        assert data["components"]["llm"]["status"] == "unavailable"
