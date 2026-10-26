"""
Tests for backend dependencies pinning and uv.lock validity.
"""

from pathlib import Path
import unittest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # fallback if running on Python < 3.11


class TestBackendDependencies(unittest.TestCase):
    """Test suite for backend dependencies."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.uv_lock_path = self.repo_root / "backend" / "uv.lock"

    def test_uv_lock_nltk_pinned_and_defusedxml_dependency(self):
        """Verify backend/uv.lock pins nltk to 3.10.0 and includes defusedxml."""
        self.assertTrue(
            self.uv_lock_path.is_file(),
            f"backend/uv.lock not found at {self.uv_lock_path}",
        )

        with open(self.uv_lock_path, "rb") as f:
            data = tomllib.load(f)

        self.assertIsInstance(data, dict)
        packages = {pkg["name"]: pkg for pkg in data.get("package", []) if "name" in pkg}
        self.assertIn("nltk", packages, "package 'nltk' not found in backend/uv.lock")

        nltk = packages["nltk"]
        self.assertEqual(
            nltk.get("version"),
            "3.10.0",
            f"nltk must be pinned to version '3.10.0', got '{nltk.get('version')}'",
        )

        dep_names = [dep["name"] for dep in nltk.get("dependencies", []) if "name" in dep]
        self.assertIn(
            "defusedxml",
            dep_names,
            f"'defusedxml' not found in nltk dependencies: {dep_names}",
        )
