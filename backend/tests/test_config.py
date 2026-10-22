"""
Tests for Flask configuration and debug mode settings.
"""

import importlib
import os
import unittest
import warnings
from unittest.mock import patch

import app.config as config_module
from app.config import Config


class TestConfig(unittest.TestCase):
    """Test suite for Config DEBUG setting and validation."""

    def setUp(self):
        self.original_debug = Config.DEBUG

    def tearDown(self):
        Config.DEBUG = self.original_debug
        importlib.reload(config_module)

    def test_default_debug_is_false_when_unset(self):
        """Verify Config.DEBUG defaults to False when FLASK_DEBUG is not set."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FLASK_DEBUG", None)
            importlib.reload(config_module)
            self.assertIs(config_module.Config.DEBUG, False)

    def test_debug_is_true_when_flask_debug_true(self):
        """Verify Config.DEBUG is True when FLASK_DEBUG is 'True' or 'true'."""
        for val in ("True", "true"):
            with self.subTest(val=val):
                with patch.dict(os.environ, {"FLASK_DEBUG": val}):
                    importlib.reload(config_module)
                    self.assertIs(config_module.Config.DEBUG, True)

    def test_validate_emits_runtime_warning_when_debug_true(self):
        """Verify Config.validate() emits RuntimeWarning when DEBUG is True."""
        Config.DEBUG = True
        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter("always")
            Config.validate()
            runtime_warnings = [
                w for w in recorded_warnings if issubclass(w.category, RuntimeWarning)
            ]
            self.assertEqual(len(runtime_warnings), 1)
            self.assertIn("Flask DEBUG mode is enabled", str(runtime_warnings[0].message))

    def test_validate_no_runtime_warning_when_debug_false(self):
        """Verify Config.validate() does not emit RuntimeWarning when DEBUG is False."""
        Config.DEBUG = False
        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter("always")
            Config.validate()
            runtime_warnings = [
                w for w in recorded_warnings if issubclass(w.category, RuntimeWarning)
            ]
            self.assertEqual(len(runtime_warnings), 0)


if __name__ == "__main__":
    unittest.main()
