"""
EmbeddingService — embedding generation via local Ollama API or Google Gemini API.

Supports:
- Local Ollama (/api/embed)
- Google Gemini (batchEmbedContents with outputDimensionality)
"""

import time
import logging
from typing import List, Optional
from functools import lru_cache

import requests

from ..config import Config

logger = logging.getLogger('mirofish.embedding')


class EmbeddingService:
    """Generate embeddings using local Ollama server or Gemini API."""

    def __init__(
        self,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        dimensions: Optional[int] = None,
        max_retries: int = 3,
        timeout: int = 30,
    ):
        self.provider = (provider or Config.EMBEDDING_PROVIDER).lower()
        self.model = model or Config.EMBEDDING_MODEL
        self.dimensions = dimensions or Config.EMBEDDING_DIMENSIONS
        self.api_key = api_key or Config.GEMINI_API_KEY
        self.base_url = (base_url or Config.EMBEDDING_BASE_URL).rstrip('/')
        self.max_retries = max_retries
        self.timeout = timeout

        if self.provider == 'gemini':
            self.api_key = api_key or Config.GEMINI_API_KEY
            if not self.api_key:
                raise ValueError("GEMINI_API_KEY not configured")
            clean_model = self.model.removeprefix('models/')
            self._gemini_model = f"models/{clean_model}"
            self._embed_url = (
                f"https://generativelanguage.googleapis.com/v1beta/"
                f"{self._gemini_model}:batchEmbedContents"
            )
        elif self.provider == 'openai':
            self.api_key = api_key or Config.EMBEDDING_API_KEY or Config.LLM_API_KEY
            if not self.api_key:
                raise ValueError("EMBEDDING_API_KEY or LLM_API_KEY not configured for openai provider")
            self._embed_url = f"{self.base_url}/embeddings"
        else:
            self._embed_url = f"{self.base_url}/api/embed"
        # Simple in-memory cache (text -> embedding vector)
        # Using dict instead of lru_cache because lists aren't hashable
        self._cache: dict[str, List[float]] = {}
        self._cache_max_size = 2000

    def embed(self, text: str) -> List[float]:
        """
        Generate embedding for a single text.

        Args:
            text: Input text to embed

        Returns:
            768-dimensional float vector

        Raises:
            EmbeddingError: If Ollama request fails after retries
        """
        if not text or not text.strip():
            raise EmbeddingError("Cannot embed empty text")

        text = text.strip()

        # Check cache
        if text in self._cache:
            return self._cache[text]

        vectors = self._request_embeddings([text])
        vector = vectors[0]

        # Cache result
        self._cache_put(text, vector)

        return vector

    def embed_batch(self, texts: List[str], batch_size: int = 32) -> List[List[float]]:
        """
        Generate embeddings for multiple texts.

        Processes in batches to avoid overwhelming Ollama.

        Args:
            texts: List of input texts
            batch_size: Number of texts per request

        Returns:
            List of embedding vectors (same order as input)
        """
        if not texts:
            return []

        results: List[Optional[List[float]]] = [None] * len(texts)
        uncached_indices: List[int] = []
        uncached_texts: List[str] = []

        # Check cache first
        for i, text in enumerate(texts):
            text = text.strip() if text else ""
            if text in self._cache:
                results[i] = self._cache[text]
            elif text:
                uncached_indices.append(i)
                uncached_texts.append(text)
            else:
                # Empty text — zero vector
                results[i] = [0.0] * self.dimensions

        # Batch-embed uncached texts
        if uncached_texts:
            all_vectors: List[List[float]] = []
            for start in range(0, len(uncached_texts), batch_size):
                batch = uncached_texts[start:start + batch_size]
                vectors = self._request_embeddings(batch)
                all_vectors.extend(vectors)

            # Place results and cache
            for idx, vec, text in zip(uncached_indices, all_vectors, uncached_texts):
                results[idx] = vec
                self._cache_put(text, vec)

        return results  # type: ignore

    def _request_embeddings(self, texts: List[str]) -> List[List[float]]:
        """
        Make HTTP request to Ollama /api/embed endpoint with retry.

        Args:
            texts: List of texts to embed (Ollama supports batch in single request)

        Returns:
            List of embedding vectors
        """
        if self.provider == 'gemini':
            headers = {
                "Content-Type": "application/json",
                "x-goog-api-key": self.api_key,
            }
            payload = {
                "requests": [
                    {
                        "model": self._gemini_model,
                        "content": {"parts": [{"text": t}]},
                        "outputDimensionality": self.dimensions,
                    }
                    for t in texts
                ]
            }
        elif self.provider == 'openai':
            headers = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
            payload = {
                "model": self.model,
                "input": texts,
            }
        else:
            headers = {"Content-Type": "application/json"}
            payload = {
                "model": self.model,
                "input": texts,
            }

        last_error = None
        for attempt in range(self.max_retries):
            try:
                response = requests.post(
                    self._embed_url,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                data = response.json()

                if self.provider == 'gemini':
                    raw_embeddings = data.get("embeddings", [])
                    if len(raw_embeddings) != len(texts):
                        raise EmbeddingError(
                            f"Expected {len(texts)} embeddings, got {len(raw_embeddings)}"
                        )
                    return [item.get("values", []) for item in raw_embeddings]
                elif self.provider == 'openai':
                    raw_data = data.get("data", [])
                    if len(raw_data) != len(texts):
                        raise EmbeddingError(
                            f"Expected {len(texts)} embeddings, got {len(raw_data)}"
                        )
                    sorted_items = sorted(raw_data, key=lambda x: x.get("index", 0))
                    return [item.get("embedding", []) for item in sorted_items]
                else:
                    embeddings = data.get("embeddings", [])
                    if len(embeddings) != len(texts):
                        raise EmbeddingError(
                            f"Expected {len(texts)} embeddings, got {len(embeddings)}"
                        )
                    return embeddings

            except requests.exceptions.ConnectionError as e:
                last_error = e
                logger.warning(
                    f"{self.provider.capitalize()} connection failed (attempt {attempt + 1}/{self.max_retries}): {e}"
                )
            except requests.exceptions.Timeout as e:
                last_error = e
                logger.warning(
                    f"{self.provider.capitalize()} request timed out (attempt {attempt + 1}/{self.max_retries})"
                )
            except requests.exceptions.HTTPError as e:
                last_error = e
                logger.error(
                    f"{self.provider.capitalize()} HTTP error: {e.response.status_code} - {e.response.text}"
                )
                if e.response.status_code >= 500 or e.response.status_code == 429:
                    # Server error or rate limit — retry
                    pass
                else:
                    # Client error (4xx) — don't retry
                    raise EmbeddingError(
                        f"{self.provider.capitalize()} embedding failed: {e}"
                    ) from e
            except (KeyError, ValueError) as e:
                raise EmbeddingError(
                    f"Invalid {self.provider.capitalize()} response: {e}"
                ) from e

            # Exponential backoff
            if attempt < self.max_retries - 1:
                wait = 2 ** attempt
                logger.info(f"Retrying in {wait}s...")
                time.sleep(wait)

        raise EmbeddingError(
            f"{self.provider.capitalize()} embedding failed after {self.max_retries} retries: {last_error}"
        )

    def _cache_put(self, text: str, vector: List[float]) -> None:
        """Add to cache, evicting oldest entries if full."""
        if len(self._cache) >= self._cache_max_size:
            # Remove ~10% of oldest entries
            keys_to_remove = list(self._cache.keys())[:self._cache_max_size // 10]
            for key in keys_to_remove:
                del self._cache[key]
        self._cache[text] = vector

    def health_check(self) -> bool:
        """Check if embedding endpoint is reachable."""
        try:
            vec = self.embed("health check")
            return len(vec) == self.dimensions
        except Exception:
            return False


class EmbeddingError(Exception):
    """Raised when embedding generation fails."""
    pass
