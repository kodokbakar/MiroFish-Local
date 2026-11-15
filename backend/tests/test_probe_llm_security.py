"""Regression tests for Issue #91: SSRF and Bearer token leakage in probe_llm_endpoint."""

from unittest.mock import MagicMock, patch

from app.api.status import probe_llm_endpoint


def test_probe_llm_endpoint_blocks_metadata_ssrf():
    """probe_llm_endpoint must reject cloud metadata / link-local addresses (e.g. 169.254.169.254)

    and not attempt outbound HTTP connections to them.
    """
    metadata_url = "http://169.254.169.254/latest/meta-data"
    with patch("app.config.Config.get_llm_base_url", return_value=metadata_url), \
         patch("httpx.Client") as mock_client_cls:
        res = probe_llm_endpoint()

        # Currently probe_llm_endpoint does not validate the host/IP and creates an httpx.Client to probe it.
        # When secured, httpx.Client should NOT be called for blocked/metadata addresses,
        # and the result should indicate unavailable/blocked rather than connecting.
        assert not mock_client_cls.called, "probe_llm_endpoint made outbound HTTP request to cloud metadata address!"
        assert res.get("status") == "unavailable"
        assert "ssrf" in res.get("error", "").lower() or "blocked" in res.get("error", "").lower() or "disallowed" in res.get("error", "").lower()


def test_probe_llm_endpoint_does_not_leak_bearer_token_to_arbitrary_untrusted_hosts():
    """probe_llm_endpoint must not transmit Authorization: Bearer <api_key> to untrusted arbitrary domains.

    API keys should only be sent to trusted public providers or explicitly configured safe hosts.
    """
    untrusted_url = "http://attacker-controlled.example.com/v1"
    secret_key = "secret-super-sensitive-api-key-12345"

    with patch("app.config.Config.get_llm_base_url", return_value=untrusted_url), \
         patch("app.config.Config.get_llm_api_key", return_value=secret_key), \
         patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client_cls.return_value.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.get.return_value = mock_resp

        probe_llm_endpoint()

        # Check call arguments to get()
        if mock_client.get.called:
            _args, kwargs = mock_client.get.call_args
            headers = kwargs.get("headers", {})
            auth_header = headers.get("Authorization", "")
            # Currently it unconditionally sends "Authorization: Bearer secret-super-sensitive-api-key-12345"
            assert "Authorization" not in headers or auth_header == "", (
                f"Bearer token was leaked in request headers to untrusted host {untrusted_url}: {auth_header}"
            )


def test_probe_llm_endpoint_rejects_disallowed_schemes():
    """probe_llm_endpoint must reject non-http/https schemes like file, ftp, gopher."""
    for bad_url in ["file:///etc/passwd", "gopher://127.0.0.1:6379", "ftp://localhost"]:
        with patch("app.config.Config.get_llm_base_url", return_value=bad_url), \
             patch("httpx.Client") as mock_client_cls:
            res = probe_llm_endpoint()
            assert not mock_client_cls.called
            assert res.get("status") == "unavailable"
            assert "disallowed" in res.get("error", "").lower() or "scheme" in res.get("error", "").lower()


def test_probe_llm_endpoint_blocks_metadata_hostnames():
    """probe_llm_endpoint must reject known cloud metadata hostnames."""
    with patch("app.config.Config.get_llm_base_url", return_value="http://metadata.google.internal/computeMetadata/v1"), \
         patch("httpx.Client") as mock_client_cls:
        res = probe_llm_endpoint()
        assert not mock_client_cls.called
        assert res.get("status") == "unavailable"
        assert "blocked" in res.get("error", "").lower() or "metadata" in res.get("error", "").lower()


def test_probe_llm_endpoint_allows_bearer_token_for_trusted_and_loopback_hosts():
    """probe_llm_endpoint attaches Authorization header for localhost and trusted providers."""
    cases = [
        ("http://localhost:11434/v1", "ollama-key"),
        ("https://api.openai.com/v1", "sk-proj-openai-secret"),
    ]
    for url, key in cases:
        with patch("app.config.Config.get_llm_base_url", return_value=url), \
             patch("app.config.Config.get_llm_api_key", return_value=key), \
             patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.get.return_value = mock_resp

            probe_llm_endpoint()

            assert mock_client.get.called
            _args, kwargs = mock_client.get.call_args
            headers = kwargs.get("headers", {})
            assert headers.get("Authorization") == f"Bearer {key}"


def test_probe_llm_endpoint_redacts_api_key_in_error():
    """probe_llm_endpoint must redact sensitive API keys from returned error strings."""
    secret = "secret-super-sensitive-token-999"
    with patch("app.config.Config.get_llm_base_url", return_value="http://localhost:11434/v1"), \
         patch("app.config.Config.get_llm_api_key", return_value=secret), \
         patch("httpx.Client", side_effect=Exception(f"Connection failed with {secret}")):
        res = probe_llm_endpoint()
        assert res.get("status") == "unavailable"
        assert secret not in res.get("error", "")
        assert "[REDACTED]" in res.get("error", "")
