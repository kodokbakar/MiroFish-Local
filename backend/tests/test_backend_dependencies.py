"""
Tests for backend dependencies pinning and uv.lock validity.
"""

from pathlib import Path
import unittest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # fallback if running on Python < 3.11


def validate_nltk_lock_data(data: dict, expected_version: str = "3.10.0") -> dict:
    """Validate that uv.lock parsed data contains valid nltk package with defusedxml dependency."""
    if not isinstance(data, dict):
        raise TypeError("Lockfile data must be a dictionary")
    packages = data.get("package")
    if not isinstance(packages, list):
        raise ValueError("Lockfile missing valid 'package' list")

    pkg_map = {pkg.get("name"): pkg for pkg in packages if isinstance(pkg, dict) and "name" in pkg}
    if "nltk" not in pkg_map:
        raise ValueError("Package 'nltk' not found in lockfile")

    nltk = pkg_map["nltk"]
    version = nltk.get("version")
    if version != expected_version:
        raise ValueError(f"nltk version '{version}' does not match expected '{expected_version}'")

    deps = [dep.get("name") for dep in nltk.get("dependencies", []) if isinstance(dep, dict) and "name" in dep]
    if "defusedxml" not in deps:
        raise ValueError("nltk dependencies missing 'defusedxml'")

    return nltk


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

        nltk = validate_nltk_lock_data(data, expected_version="3.10.0")
        self.assertEqual(nltk.get("version"), "3.10.0")

    def test_uv_lock_defusedxml_package_entry_exists(self):
        """Verify backend/uv.lock includes a top-level package entry for defusedxml."""
        with open(self.uv_lock_path, "rb") as f:
            data = tomllib.load(f)

        packages = {pkg.get("name"): pkg for pkg in data.get("package", []) if isinstance(pkg, dict) and "name" in pkg}
        self.assertIn("defusedxml", packages, "Package 'defusedxml' missing as a top-level package in uv.lock")
        self.assertTrue(packages["defusedxml"].get("version"), "defusedxml package entry has no version")

    def test_validation_negative_missing_package_section(self):
        """Verify validation fails when lockfile data lacks a valid package list."""
        with self.assertRaises(ValueError) as ctx:
            validate_nltk_lock_data({})
        self.assertIn("missing valid 'package' list", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            validate_nltk_lock_data({"package": "not-a-list"})
        self.assertIn("missing valid 'package' list", str(ctx.exception))

    def test_validation_negative_missing_nltk(self):
        """Verify validation fails when nltk is not present in package list."""
        data = {"package": [{"name": "click", "version": "8.1.7"}]}
        with self.assertRaises(ValueError) as ctx:
            validate_nltk_lock_data(data)
        self.assertIn("Package 'nltk' not found", str(ctx.exception))

    def test_validation_negative_nltk_version_mismatch(self):
        """Verify validation fails when nltk has an outdated or mismatched version."""
        data = {
            "package": [
                {
                    "name": "nltk",
                    "version": "3.9.2",
                    "dependencies": [{"name": "defusedxml"}],
                }
            ]
        }
        with self.assertRaises(ValueError) as ctx:
            validate_nltk_lock_data(data, expected_version="3.10.0")
        self.assertIn("does not match expected '3.10.0'", str(ctx.exception))

    def test_validation_negative_missing_defusedxml_dependency(self):
        """Verify validation fails when nltk is missing defusedxml dependency."""
        data = {
            "package": [
                {
                    "name": "nltk",
                    "version": "3.10.0",
                    "dependencies": [{"name": "click"}, {"name": "joblib"}],
                }
            ]
        }
        with self.assertRaises(ValueError) as ctx:
            validate_nltk_lock_data(data)
        self.assertIn("missing 'defusedxml'", str(ctx.exception))

    def test_lockfile_malformed_toml_syntax_error(self):
        """Verify parsing malformed TOML content raises TOMLDecodeError."""
        malformed_toml = b"[[package]\nname = 'broken'\nversion = "
        with self.assertRaises(tomllib.TOMLDecodeError):
            tomllib.loads(malformed_toml.decode("utf-8"))

    def test_lockfile_nonexistent_path_fails(self):
        """Verify checking a nonexistent lockfile path reports missing file."""
        nonexistent_path = self.repo_root / "backend" / "nonexistent_uv.lock"
        self.assertFalse(nonexistent_path.is_file())
