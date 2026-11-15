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


def test_probe_llm_endpoint_blocks_alternative_ip_representations():
    """probe_llm_endpoint must reject dword, hex, and octal encodings of metadata addresses."""
    representations = [
        "http://2852039166",
        "http://2852039166/latest/meta-data",
        "http://0xa9fea9fe",
        "http://0xa9.0xfe.0xa9.0xfe",
        "http://0251.0376.0251.0376",
        "http://0251.0376.0251.0376:80",
    ]
    for url in representations:
        with patch("app.config.Config.get_llm_base_url", return_value=url),              patch("httpx.Client") as mock_client_cls:
            res = probe_llm_endpoint()
            assert not mock_client_cls.called, f"Outbound call made for alternative IP representation: {url}"
            assert res.get("status") == "unavailable"
            assert "blocked" in res.get("error", "").lower() or "ssrf" in res.get("error", "").lower()


def test_probe_llm_endpoint_blocks_ipv6_link_local_and_mapped_metadata():
    """probe_llm_endpoint must reject IPv6 link-local and IPv6-mapped IPv4 metadata addresses."""
    ipv6_cases = [
        "http://[fe80::1]:80",
        "http://[fe80::1]/v1",
        "http://[::ffff:169.254.169.254]",
        "http://[::ffff:169.254.169.254]:8080/latest/meta-data",
        "http://[::ffff:a9fe:a9fe]",
    ]
    for url in ipv6_cases:
        with patch("app.config.Config.get_llm_base_url", return_value=url),              patch("httpx.Client") as mock_client_cls:
            res = probe_llm_endpoint()
            assert not mock_client_cls.called, f"Outbound call made for IPv6 link-local / mapped IP: {url}"
            assert res.get("status") == "unavailable"
            assert "blocked" in res.get("error", "").lower() or "ssrf" in res.get("error", "").lower()


def test_probe_llm_endpoint_blocks_metadata_with_ports_and_paths():
    """probe_llm_endpoint must reject metadata IP addresses with custom ports and paths."""
    metadata_ports = [
        "http://169.254.169.254:8080",
        "http://169.254.169.254:1337/api",
        "http://169.254.169.254:80/latest/meta-data/iam/security-credentials/",
        "http://169.254.169.254:443",
    ]
    for url in metadata_ports:
        with patch("app.config.Config.get_llm_base_url", return_value=url),              patch("httpx.Client") as mock_client_cls:
            res = probe_llm_endpoint()
            assert not mock_client_cls.called, f"Outbound call made for metadata with port: {url}"
            assert res.get("status") == "unavailable"
            assert "blocked" in res.get("error", "").lower() or "ssrf" in res.get("error", "").lower()


def test_probe_llm_endpoint_legitimate_local_ollama_endpoints():
    """Legitimate local Ollama endpoints succeed without leaking bearer tokens or keys."""
    endpoints = [
        "http://127.0.0.1:11434",
        "http://localhost:11434",
        "http://[::1]:11434",
    ]
    # Default Ollama without API key
    for url in endpoints:
        with patch("app.config.Config.get_llm_base_url", return_value=url),              patch("app.config.Config.get_llm_api_key", return_value=""),              patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.get.return_value = mock_resp

            res = probe_llm_endpoint()

            assert res.get("status") == "ok"
            assert mock_client.get.called
            _args, kwargs = mock_client.get.call_args
            headers = kwargs.get("headers", {})
            assert "Authorization" not in headers or headers["Authorization"] == ""

    # Ollama with key configured still redacts on errors and succeeds on normal response
    secret_key = "local-ollama-secret-token"
    for url in endpoints:
        with patch("app.config.Config.get_llm_base_url", return_value=url),              patch("app.config.Config.get_llm_api_key", return_value=secret_key),              patch("httpx.Client") as mock_client_cls:
            mock_client = MagicMock()
            mock_client_cls.return_value.__enter__.return_value = mock_client
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_client.get.return_value = mock_resp

            res = probe_llm_endpoint()
            assert res.get("status") == "ok"
            assert mock_client.get.called

            mock_client.get.side_effect = Exception(f"Connection error with {secret_key}")
            err_res = probe_llm_endpoint()
            assert err_res.get("status") == "unavailable"
            assert secret_key not in err_res.get("error", "")
            assert "[REDACTED]" in err_res.get("error", "")
