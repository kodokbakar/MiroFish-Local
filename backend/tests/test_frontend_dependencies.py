"""
Tests for frontend dependencies pinning and package.json validity.
"""

import json
from pathlib import Path
import unittest


class TestFrontendDependencies(unittest.TestCase):
    """Test suite for frontend dependencies."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.package_json_path = self.repo_root / "frontend" / "package.json"

    def test_frontend_package_json_valid_and_axios_pinned(self):
        """Verify frontend/package.json is valid JSON and axios is pinned to exact 1.14.0."""
        self.assertTrue(
            self.package_json_path.is_file(),
            f"frontend/package.json not found at {self.package_json_path}",
        )

        with open(self.package_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertIsInstance(data, dict)
        self.assertIn("dependencies", data)
        dependencies = data["dependencies"]
        self.assertIn("axios", dependencies)

        axios_version = dependencies["axios"]
        self.assertFalse(
            axios_version.startswith("^") or axios_version.startswith("~"),
            f"axios version '{axios_version}' has leading range prefix (^ or ~)",
        )
        self.assertEqual(
            axios_version,
            "1.14.0",
            f"axios must be pinned to exact '1.14.0', got '{axios_version}'",
        )
