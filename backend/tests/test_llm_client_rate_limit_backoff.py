"""Regression test for Issue #64: HTTP 429 rate limit backoff and retry in LLMClient."""

from types import SimpleNamespace
from unittest.mock import patch
import httpx
from openai import RateLimitError, APIConnectionError, BadRequestError
import pytest

from app.utils.llm_client import LLMClient


def _make_rate_limit_error(retry_after: str = None):
    req = httpx.Request("POST", "http://test")
    headers = {"retry-after": retry_after} if retry_after else {}
    res = httpx.Response(429, request=req, headers=headers)
    return RateLimitError("Rate limit reached", response=res, body={"error": {"message": "429"}})


def _client_for(sequence):
    client = object.__new__(LLMClient)
    client.model = "compatible-model"
    client._num_ctx = 8192
    client.base_url = "http://api.openai.com/v1"
    client.client = SimpleNamespace(
        chat=SimpleNamespace(completions=sequence)
    )
    return client


class CompletionSequence:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.results:
            raise RuntimeError("Sequence exhausted")
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _response(content):
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=content))]
    )


def test_create_completion_retries_on_rate_limit_with_backoff():
    sequence = CompletionSequence(
        _make_rate_limit_error(),
        _make_rate_limit_error(),
        _response("Success after 429"),
    )
    client = _client_for(sequence)

    sleeps = []
    with patch("time.sleep", side_effect=sleeps.append):
        result = client.chat([{"role": "user", "content": "hi"}])

    assert result == "Success after 429"
    assert len(sequence.calls) == 3
    assert len(sleeps) == 2
    # Exponential backoff: second sleep should be larger or equal to base
    assert sleeps[0] >= 1.0
    assert sleeps[1] >= sleeps[0]


def test_create_completion_respects_retry_after_header():
    sequence = CompletionSequence(
        _make_rate_limit_error(retry_after="5"),
        _response("Success after retry-after"),
    )
    client = _client_for(sequence)

    sleeps = []
    with patch("time.sleep", side_effect=sleeps.append):
        result = client.chat([{"role": "user", "content": "hi"}])

    assert result == "Success after retry-after"
    assert len(sequence.calls) == 2
    assert len(sleeps) == 1
    assert sleeps[0] >= 5.0


def test_create_completion_exhausts_retries_and_raises():
    sequence = CompletionSequence(
        _make_rate_limit_error(),
        _make_rate_limit_error(),
        _make_rate_limit_error(),
        _make_rate_limit_error(),
        _make_rate_limit_error(),
        _make_rate_limit_error(),
    )
    client = _client_for(sequence)

    with patch("time.sleep"), pytest.raises(RateLimitError):
        client.chat([{"role": "user", "content": "hi"}])


def test_create_completion_does_not_retry_non_retryable_errors():
    req = httpx.Request("POST", "http://test")
    res = httpx.Response(400, request=req)
    bad_req = BadRequestError("Invalid param", response=res, body={"error": {"message": "bad"}})
    sequence = CompletionSequence(bad_req)
    client = _client_for(sequence)

    with patch("time.sleep") as mock_sleep, pytest.raises(BadRequestError):
        client.chat([{"role": "user", "content": "hi"}])

    mock_sleep.assert_not_called()
    assert len(sequence.calls) == 1
