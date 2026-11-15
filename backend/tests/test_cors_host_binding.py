"""
Regression tests for Issue #90 (Item 067):
- Restrict CORS wildcard and require origin verification on API endpoints
- Restrict default Flask host binding to 127.0.0.1 (not 0.0.0.0)
"""

from unittest.mock import patch

import run
from app import create_app


def test_cors_rejects_untrusted_origin():
    """
    Test that /api/* endpoints reject untrusted origins.
    Currently fails because app/__init__.py configures CORS(app, resources={r"/api/*": {"origins": "*"}})
    which reflects any Origin header (including http://evil.com).
    """
    app = create_app()
    client = app.test_client()

    response = client.get('/api/status', headers={'Origin': 'http://evil.com'})

    # An untrusted origin should not receive an Access-Control-Allow-Origin header allowing it
    allowed_origin = response.headers.get('Access-Control-Allow-Origin')
    assert allowed_origin != 'http://evil.com', (
        f"Untrusted origin 'http://evil.com' was permitted by CORS (got {allowed_origin})"
    )
    assert allowed_origin != '*', "Wildcard '*' origin permitted on /api/* endpoints"


def test_flask_default_host_binding():
    """
    Test that backend/run.py defaults host to 127.0.0.1 instead of 0.0.0.0.
    Currently fails because run.py uses os.environ.get('FLASK_HOST', '0.0.0.0').
    """
    with patch.dict(run.os.environ, {}, clear=True), \
         patch.object(run.Config, 'validate', return_value=[]), \
         patch.object(run, 'create_app') as mock_create_app:
        mock_app = mock_create_app.return_value
        run.main()

        mock_app.run.assert_called_once()
        _, kwargs = mock_app.run.call_args
        assert kwargs.get('host') == '127.0.0.1', (
            f"Expected default host to be '127.0.0.1', but got {kwargs.get('host')}"
        )


def test_cors_origin_case_and_trailing_slash():
    """
    Test origin verification handles trailing slash and case differences.
    Standard Origin headers do not contain trailing slashes (RFC 6454),
    so requests with a trailing slash or altered host case should not match
    exact allowed origins unless explicitly configured.
    """
    app = create_app()
    client = app.test_client()

    # Trailing slash variation should not be allowed
    resp_slash = client.get('/api/status', headers={'Origin': 'http://localhost:3000/'})
    assert resp_slash.headers.get('Access-Control-Allow-Origin') is None

    # Exact valid match succeeds
    resp_valid = client.get('/api/status', headers={'Origin': 'http://localhost:3000'})
    assert resp_valid.headers.get('Access-Control-Allow-Origin') == 'http://localhost:3000'


def test_cors_allowed_origins_multiple_with_whitespace():
    """
    Test multiple comma-separated origins with surrounding whitespace in CORS_ALLOWED_ORIGINS.
    """
    raw_env = " http://a.example.com ,  http://b.example.com "
    with patch.dict('os.environ', {'CORS_ALLOWED_ORIGINS': raw_env}):
        app = create_app()
        client = app.test_client()

        # Both trimmed origins should be allowed
        resp_a = client.get('/api/status', headers={'Origin': 'http://a.example.com'})
        assert resp_a.headers.get('Access-Control-Allow-Origin') == 'http://a.example.com'

        resp_b = client.get('/api/status', headers={'Origin': 'http://b.example.com'})
        assert resp_b.headers.get('Access-Control-Allow-Origin') == 'http://b.example.com'

        # Other origins blocked
        resp_c = client.get('/api/status', headers={'Origin': 'http://c.example.com'})
        assert resp_c.headers.get('Access-Control-Allow-Origin') is None


def test_cors_rejects_subdomain_and_port_variations():
    """
    Test rejection of wildcard subdomain, prefix spoofing, and port variations.
    """
    app = create_app()
    client = app.test_client()

    variations = [
        'http://evil-localhost:3000',
        'http://localhost.evil.com:3000',
        'http://localhost:3001',
        'http://localhost:80',
        'https://localhost:3000',
        'http://127.0.0.1:8080',
    ]
    for origin in variations:
        resp = client.get('/api/status', headers={'Origin': origin})
        allowed = resp.headers.get('Access-Control-Allow-Origin')
        assert allowed is None, f"Origin variation '{origin}' should be rejected, but got {allowed}"


def test_cors_preflight_options_untrusted_and_trusted():
    """
    Test pre-flight OPTIONS requests for untrusted vs trusted origins.
    Untrusted origin should receive no Access-Control-Allow-Origin header.
    Trusted origin should receive matching Access-Control-Allow-Origin header.
    """
    app = create_app()
    client = app.test_client()

    # Untrusted origin pre-flight
    untrusted_resp = client.options(
        '/api/status',
        headers={
            'Origin': 'http://evil.com',
            'Access-Control-Request-Method': 'GET',
        },
    )
    assert untrusted_resp.headers.get('Access-Control-Allow-Origin') is None

    # Trusted origin pre-flight
    trusted_resp = client.options(
        '/api/status',
        headers={
            'Origin': 'http://localhost:3000',
            'Access-Control-Request-Method': 'GET',
        },
    )
    assert trusted_resp.headers.get('Access-Control-Allow-Origin') == 'http://localhost:3000'
