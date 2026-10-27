"""
Tests for backend dependencies pinning and uv.lock validity.
"""

from pathlib import Path
import unittest
from packaging.specifiers import SpecifierSet

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


def validate_requires_python_constraint(
    raw_constraint: str,
    expected_min: str = "3.11",
    expected_max: str = "3.13",
) -> dict:
    """Validate that requires-python string enforces >=expected_min and <expected_max constraints."""
    if not isinstance(raw_constraint, str):
        raise TypeError("requires-python constraint must be a string")

    parts = [part.strip().replace(" ", "") for part in raw_constraint.split(",") if part.strip()]
    if not parts:
        raise ValueError("requires-python constraint cannot be empty")

    has_min = any(part == f">={expected_min}" for part in parts)
    has_max = any(part == f"<{expected_max}" for part in parts)

    if not has_min:
        raise ValueError(f"requires-python missing minimum version constraint '>={expected_min}'")
    if not has_max:
        raise ValueError(f"requires-python missing upper bound constraint '<{expected_max}'")

    return {"min": expected_min, "max": expected_max, "raw": raw_constraint}


class TestBackendDependencies(unittest.TestCase):
    """Test suite for backend dependencies."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.uv_lock_path = self.repo_root / "backend" / "uv.lock"
        self.pyproject_path = self.repo_root / "backend" / "pyproject.toml"

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

    def test_pyproject_toml_requires_python_constraint(self):
        """Verify backend/pyproject.toml exists and specifies requires-python = '>=3.11,<3.13'."""
        self.assertTrue(
            self.pyproject_path.is_file(),
            f"backend/pyproject.toml not found at {self.pyproject_path}",
        )

        with open(self.pyproject_path, "rb") as f:
            data = tomllib.load(f)

        project = data.get("project", {})
        self.assertIn("requires-python", project, "Missing requires-python in pyproject.toml [project] table")
        requires_python = project["requires-python"]
        self.assertEqual(requires_python, ">=3.11,<3.13")

        parsed = validate_requires_python_constraint(requires_python)
        self.assertEqual(parsed["min"], "3.11")
        self.assertEqual(parsed["max"], "3.13")

    def test_uv_lock_requires_python_constraint(self):
        """Verify backend/uv.lock exists and specifies requires-python = '>=3.11, <3.13'."""
        self.assertTrue(
            self.uv_lock_path.is_file(),
            f"backend/uv.lock not found at {self.uv_lock_path}",
        )

        with open(self.uv_lock_path, "rb") as f:
            data = tomllib.load(f)

        self.assertIn("requires-python", data, "Missing top-level requires-python in uv.lock")
        requires_python = data["requires-python"]
        self.assertEqual(requires_python, ">=3.11, <3.13")

        parsed = validate_requires_python_constraint(requires_python)
        self.assertEqual(parsed["min"], "3.11")
        self.assertEqual(parsed["max"], "3.13")

    def test_validate_requires_python_non_string_type_error(self):
        """Verify validate_requires_python_constraint raises TypeError for non-string inputs."""
        for invalid in [None, 123, ["3.11"], {"requires-python": ">=3.11"}]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(TypeError) as ctx:
                    validate_requires_python_constraint(invalid)
                self.assertIn("must be a string", str(ctx.exception))

    def test_validate_requires_python_empty_string_value_error(self):
        """Verify validate_requires_python_constraint raises ValueError for empty constraints."""
        for empty in ["", "   ", " , ,  "]:
            with self.subTest(empty=empty):
                with self.assertRaises(ValueError) as ctx:
                    validate_requires_python_constraint(empty)
                self.assertIn("cannot be empty", str(ctx.exception))

    def test_validate_requires_python_missing_min_bound(self):
        """Verify validate_requires_python_constraint fails when lower bound is missing or wrong."""
        with self.assertRaises(ValueError) as ctx:
            validate_requires_python_constraint("<3.13")
        self.assertIn("missing minimum version constraint '>=3.11'", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            validate_requires_python_constraint(">=3.10, <3.13")
        self.assertIn("missing minimum version constraint '>=3.11'", str(ctx.exception))

    def test_validate_requires_python_missing_max_bound(self):
        """Verify validate_requires_python_constraint fails when upper bound is missing or wrong."""
        with self.assertRaises(ValueError) as ctx:
            validate_requires_python_constraint(">=3.11")
        self.assertIn("missing upper bound constraint '<3.13'", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            validate_requires_python_constraint(">=3.11, <3.14")
        self.assertIn("missing upper bound constraint '<3.13'", str(ctx.exception))

    def test_requires_python_specifier_boundary_versions(self):
        """Verify SpecifierSet from pyproject and uv.lock accurately accepts 3.11-3.12 and rejects 3.10 and 3.13+."""
        with open(self.pyproject_path, "rb") as f:
            pyproject_data = tomllib.load(f)
        pyproject_spec = SpecifierSet(pyproject_data["project"]["requires-python"])

        with open(self.uv_lock_path, "rb") as f:
            uv_lock_data = tomllib.load(f)
        uv_lock_spec = SpecifierSet(uv_lock_data["requires-python"])

        for spec, source in [(pyproject_spec, "pyproject.toml"), (uv_lock_spec, "uv.lock")]:
            with self.subTest(source=source):
                # Valid supported versions: 3.11.x and 3.12.x
                self.assertIn("3.11.0", spec, f"{source} should allow Python 3.11.0")
                self.assertIn("3.11.9", spec, f"{source} should allow Python 3.11.9")
                self.assertIn("3.12.0", spec, f"{source} should allow Python 3.12.0")
                self.assertIn("3.12.9", spec, f"{source} should allow Python 3.12.9")

                # Unsupported lower versions (<3.11)
                self.assertNotIn("3.10.0", spec, f"{source} should reject Python 3.10.0")
                self.assertNotIn("3.10.14", spec, f"{source} should reject Python 3.10.14")

                # Unsupported upper versions (>=3.13) - prevents build failures on Python 3.13+
                self.assertNotIn("3.13.0", spec, f"{source} should reject Python 3.13.0")
                self.assertNotIn("3.13.2", spec, f"{source} should reject Python 3.13.2")
                self.assertNotIn("3.14.0", spec, f"{source} should reject Python 3.14.0")

    def test_validate_requires_python_whitespace_and_order_tolerance(self):
        """Verify validate_requires_python_constraint handles whitespace variations and reversed ordering."""
        variants = [
            ">= 3.11 , < 3.13",
            "  >=3.11 , <3.13  ",
            ">=3.11,\t<3.13",
            ">=3.11\n,<3.13",
            "<3.13, >=3.11",
            ">=3.11,, <3.13",
        ]
        for variant in variants:
            with self.subTest(variant=variant):
                parsed = validate_requires_python_constraint(variant)
                self.assertEqual(parsed["min"], "3.11")
                self.assertEqual(parsed["max"], "3.13")

    def test_validate_requires_python_operator_boundaries_and_custom_bounds(self):
        """Verify validate_requires_python_constraint handles operator boundaries and custom expected bounds."""
        # Custom bounds
        custom = validate_requires_python_constraint(">=3.10, <3.12", expected_min="3.10", expected_max="3.12")
        self.assertEqual(custom["min"], "3.10")
        self.assertEqual(custom["max"], "3.12")

        # Wrong comparison operators
        with self.assertRaises(ValueError) as ctx:
            validate_requires_python_constraint(">3.11, <3.13")
        self.assertIn("missing minimum version constraint '>=3.11'", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            validate_requires_python_constraint(">=3.11, <=3.13")
        self.assertIn("missing upper bound constraint '<3.13'", str(ctx.exception))

    def test_pyproject_toml_missing_or_invalid_project_table(self):
        """Verify handling of missing project table or missing requires-python key."""
        self.assertNotIn("project", tomllib.loads("[tool.other]\nkey = 'val'"))
        self.assertNotIn("requires-python", tomllib.loads("[project]\nname = 'test'").get("project", {}))
