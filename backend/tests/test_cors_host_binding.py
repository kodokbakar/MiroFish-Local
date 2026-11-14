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
