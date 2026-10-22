"""
Tests for EmbeddingService with Gemini and Ollama providers.
"""

import unittest
from unittest.mock import patch, MagicMock
import requests

from app.config import Config
from app.storage.embedding_service import EmbeddingService, EmbeddingError


class TestEmbeddingService(unittest.TestCase):

    def setUp(self):
        # Reset Config defaults for testing
        Config.EMBEDDING_PROVIDER = "gemini"
        Config.GEMINI_API_KEY = "test-gemini-key"
        Config.EMBEDDING_MODEL = "gemini-embedding-2"
        Config.EMBEDDING_DIMENSIONS = 768

    def test_gemini_init_missing_key(self):
        Config.GEMINI_API_KEY = None
        with self.assertRaises(ValueError):
            EmbeddingService(provider="gemini", api_key=None)

    def test_gemini_init_endpoint_url(self):
        service = EmbeddingService(
            provider="gemini",
            api_key="test-key",
            model="gemini-embedding-2",
            dimensions=768
        )
        self.assertEqual(
            service._embed_url,
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents"
        )
        self.assertEqual(service._gemini_model, "models/gemini-embedding-2")

    def test_gemini_strip_model_prefix(self):
        service = EmbeddingService(
            provider="gemini",
            api_key="test-key",
            model="models/gemini-embedding-2",
            dimensions=768
        )
        self.assertEqual(
            service._embed_url,
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-2:batchEmbedContents"
        )
        self.assertEqual(service._gemini_model, "models/gemini-embedding-2")

    @patch("app.storage.embedding_service.requests.post")
    def test_gemini_embed_single_success(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        fake_vector = [0.1] * 768
        mock_resp.json.return_value = {
            "embeddings": [{"values": fake_vector}]
        }
        mock_post.return_value = mock_resp

        service = EmbeddingService(provider="gemini", api_key="test-key", dimensions=768)
        vec = service.embed("hello world")

        self.assertEqual(vec, fake_vector)
        self.assertEqual(len(vec), 768)
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args.kwargs
        self.assertEqual(call_kwargs["headers"]["x-goog-api-key"], "test-key")
        self.assertEqual(call_kwargs["json"]["requests"][0]["outputDimensionality"], 768)
        self.assertEqual(
            call_kwargs["json"]["requests"][0]["content"]["parts"][0]["text"],
            "hello world"
        )

    @patch("app.storage.embedding_service.requests.post")
    def test_gemini_embed_batch(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        fake_vector_1 = [0.1] * 768
        fake_vector_2 = [0.2] * 768
        mock_resp.json.return_value = {
            "embeddings": [
                {"values": fake_vector_1},
                {"values": fake_vector_2}
            ]
        }
        mock_post.return_value = mock_resp

        service = EmbeddingService(provider="gemini", api_key="test-key", dimensions=768)
        results = service.embed_batch(["text one", "text two"])

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], fake_vector_1)
        self.assertEqual(results[1], fake_vector_2)

    @patch("app.storage.embedding_service.requests.post")
    def test_gemini_embed_empty_handling(self, mock_post):
        service = EmbeddingService(provider="gemini", api_key="test-key", dimensions=768)
        with self.assertRaises(EmbeddingError):
            service.embed("")

        results = service.embed_batch(["", "   "])
        self.assertEqual(results, [[0.0] * 768, [0.0] * 768])
        mock_post.assert_not_called()

    @patch("app.storage.embedding_service.requests.post")
    def test_gemini_caching(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        fake_vector = [0.1] * 768
        mock_resp.json.return_value = {
            "embeddings": [{"values": fake_vector}]
        }
        mock_post.return_value = mock_resp

        service = EmbeddingService(provider="gemini", api_key="test-key", dimensions=768)
        vec1 = service.embed("same text")
        vec2 = service.embed("same text")

        self.assertEqual(vec1, vec2)
        self.assertEqual(mock_post.call_count, 1)

    @patch("app.storage.embedding_service.requests.post")
    def test_ollama_compatibility(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        fake_vector = [0.5] * 768
        mock_resp.json.return_value = {
            "embeddings": [fake_vector]
        }
        mock_post.return_value = mock_resp

        service = EmbeddingService(
            provider="ollama",
            base_url="http://localhost:11434",
            model="nomic-embed-text",
            dimensions=768
        )
        self.assertEqual(service._embed_url, "http://localhost:11434/api/embed")
        vec = service.embed("test ollama")

        self.assertEqual(vec, fake_vector)
        call_kwargs = mock_post.call_args.kwargs
        self.assertNotIn("x-goog-api-key", call_kwargs.get("headers", {}))
        self.assertEqual(call_kwargs["json"], {"model": "nomic-embed-text", "input": ["test ollama"]})

    @patch("app.storage.embedding_service.requests.post")
    def test_health_check(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "embeddings": [{"values": [0.1] * 768}]
        }
        mock_post.return_value = mock_resp

        service = EmbeddingService(provider="gemini", api_key="test-key", dimensions=768)
        self.assertTrue(service.health_check())
    def test_config_validate_gemini_missing_key(self):
        Config.LLM_API_KEY = "dummy"
        Config.NEO4J_URI = "bolt://localhost:7687"
        Config.NEO4J_PASSWORD = "pass"
        Config.EMBEDDING_PROVIDER = "gemini"
        Config.GEMINI_API_KEY = None
        errors = Config.validate()
        self.assertTrue(any("GEMINI_API_KEY" in err for err in errors))

    def test_config_validate_gemini_success(self):
        Config.LLM_API_KEY = "dummy"
        Config.NEO4J_URI = "bolt://localhost:7687"
        Config.NEO4J_PASSWORD = "pass"
        Config.EMBEDDING_PROVIDER = "gemini"
        Config.GEMINI_API_KEY = "valid-key"
        errors = Config.validate()
        self.assertEqual(errors, [])



if __name__ == "__main__":
    unittest.main()
