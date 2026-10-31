"""
Unit tests for chunk_size and chunk_overlap validation (Issue #39).
Tests split_text_into_chunks defensive bounds and /api/graph/build endpoint validation.
"""

import time
import pytest
from unittest.mock import patch, MagicMock

from app import create_app
from app.models.project import Project, ProjectManager, ProjectStatus
from app.utils.file_parser import split_text_into_chunks


class TestSplitTextIntoChunks:
    """Test suite for split_text_into_chunks guardrails and infinite loop prevention."""

    def test_overlap_greater_than_or_equal_to_chunk_size(self):
        """When overlap >= chunk_size, overlap should be clamped to chunk_size - 1."""
        text = "abcdefghijklmnopqrstuvwxyz" * 20  # 520 chars
        start_time = time.time()
        # chunk_size = 100, overlap = 100 -> clamped to 99
        chunks = split_text_into_chunks(text, chunk_size=100, overlap=100)
        elapsed = time.time() - start_time

        assert elapsed < 1.0, f"Execution took too long ({elapsed:.2f}s), possible infinite loop"
        assert len(chunks) > 0
        assert all(len(c) <= 100 for c in chunks)

        # Test overlap > chunk_size
        start_time = time.time()
        chunks2 = split_text_into_chunks(text, chunk_size=100, overlap=200)
        elapsed2 = time.time() - start_time
        assert elapsed2 < 1.0
        assert len(chunks2) > 0

    def test_chunk_overlap_alias_keyword(self):
        """Support chunk_overlap keyword argument as an alias."""
        text = "Hello world. " * 30
        chunks = split_text_into_chunks(text, chunk_size=100, chunk_overlap=150)
        assert len(chunks) > 0

    def test_negative_overlap(self):
        """Negative overlap should be clamped to 0."""
        text = "abcdefghijklmnopqrstuvwxyz" * 10
        chunks = split_text_into_chunks(text, chunk_size=100, overlap=-50)
        assert len(chunks) > 0
        chunks_alias = split_text_into_chunks(text, chunk_size=100, chunk_overlap=-20)
        assert len(chunks_alias) > 0

    def test_zero_and_negative_chunk_size(self):
        """chunk_size <= 0 should default to 100."""
        text = "The quick brown fox jumps over the lazy dog. " * 20
        start_time = time.time()
        chunks_zero = split_text_into_chunks(text, chunk_size=0, overlap=10)
        assert time.time() - start_time < 1.0
        assert len(chunks_zero) > 0

        start_time = time.time()
        chunks_neg = split_text_into_chunks(text, chunk_size=-50, overlap=10)
        assert time.time() - start_time < 1.0
        assert len(chunks_neg) > 0

    def test_boundary_adjustments_strict_forward_progress(self):
        """Ensure start strictly advances even when punctuation boundaries alter end."""
        # Create text with punctuation repeating very frequently
        text = "A. B. C. D. E. F. G. H. I. J. K. L. M. N. O. P. " * 30
        start_time = time.time()
        chunks = split_text_into_chunks(text, chunk_size=100, overlap=99)
        elapsed = time.time() - start_time

        assert elapsed < 1.0
        assert len(chunks) > 0

    def test_empty_and_short_text(self):
        """Empty text returns empty list; short text returns single chunk."""
        assert split_text_into_chunks("") == []
        assert split_text_into_chunks("   \n\t  ") == []
        assert split_text_into_chunks("Short sentence.", chunk_size=500) == ["Short sentence."]

    def test_large_text_fast_execution(self):
        """Large text should chunk quickly without stalling."""
        text = "Paragraph of text with some content. " * 2000  # ~74,000 chars
        start_time = time.time()
        chunks = split_text_into_chunks(text, chunk_size=500, overlap=50)
        elapsed = time.time() - start_time

        assert elapsed < 2.0
        assert len(chunks) > 100

    def test_type_resilience(self):
        """String numbers or invalid types are safely converted or defaulted."""
        text = "Resilient chunking test content. " * 20
        chunks = split_text_into_chunks(text, chunk_size="150", overlap="20")
        assert len(chunks) > 0
        chunks_bad = split_text_into_chunks(text, chunk_size="invalid", overlap=None)
        assert len(chunks_bad) > 0


class TestBuildGraphEndpointChunkValidation:
    """Test suite for /api/graph/build chunk parameter validation."""

    @pytest.fixture
    def client_and_project(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ProjectManager, "PROJECTS_DIR", str(tmp_path))
        with patch("app.storage.Neo4jStorage"):
            app = create_app()
        app.config.update(TESTING=True)

        project = ProjectManager.create_project(name="Validation Test Project")
        project.status = ProjectStatus.ONTOLOGY_GENERATED
        project.ontology = {"entity_types": ["Person"], "relation_types": ["KNOWS"]}
        ProjectManager.save_project(project)
        ProjectManager.save_extracted_text(
            project.project_id,
            "Sample extracted document text for testing graph building."
        )

        return app.test_client(), project

    @pytest.mark.parametrize(
        "chunk_size,chunk_overlap",
        [
            (99, 10),      # chunk_size < 100
            (0, 0),        # chunk_size = 0
            (-50, 0),      # chunk_size negative
            (500, -1),     # chunk_overlap negative
            (500, 500),    # chunk_overlap == chunk_size
            (200, 300),    # chunk_overlap > chunk_size
            ("abc", 50),   # unparseable chunk_size
            (500, "xyz"),  # unparseable chunk_overlap
            (True, 10),    # boolean chunk_size
            (200, True),   # boolean chunk_overlap
            (150.5, 10),   # float with decimal part
            (200, 50.5),   # float with decimal part
        ],
    )
    def test_build_graph_rejects_invalid_chunk_config(self, client_and_project, chunk_size, chunk_overlap):
        client, project = client_and_project

        response = client.post(
            "/api/graph/build",
            json={
                "project_id": project.project_id,
                "chunk_size": chunk_size,
                "chunk_overlap": chunk_overlap,
            },
        )

        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert data["error"] == (
            "Invalid chunk configuration: chunk_size must be >= 100 and chunk_overlap must be >= 0 and < chunk_size"
        )

    @pytest.mark.parametrize(
        "chunk_size,chunk_overlap",
        [
            (100, 0),
            (100, 99),
            (500, 50),
            ("200", "50"),
            (1000, 100),
        ],
    )
    def test_build_graph_accepts_valid_chunk_config(self, client_and_project, chunk_size, chunk_overlap):
        client, project = client_and_project

        with patch("threading.Thread.start"):
            response = client.post(
                "/api/graph/build",
                json={
                    "project_id": project.project_id,
                    "chunk_size": chunk_size,
                    "chunk_overlap": chunk_overlap,
                },
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert "task_id" in data["data"]

        updated_project = ProjectManager.get_project(project.project_id)
        assert updated_project.chunk_size == int(chunk_size)
        assert updated_project.chunk_overlap == int(chunk_overlap)
