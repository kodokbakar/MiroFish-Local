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
        self.root_package_json_path = self.repo_root / "package.json"
        self.package_json_path = self.repo_root / "frontend" / "package.json"

    def test_frontend_package_json_valid_and_axios_pinned(self):
        """Verify frontend/package.json is valid JSON and axios is pinned to exact 1.18.1."""
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
            "1.18.1",
            f"axios must be pinned to exact '1.18.1', got '{axios_version}'",
        )

    def test_root_package_json_concurrently(self):
        """Verify root package.json specifies concurrently (e.g. ^9.2.4)."""
        self.assertTrue(
            self.root_package_json_path.is_file(),
            f"root package.json not found at {self.root_package_json_path}",
        )
        with open(self.root_package_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        dev_deps = data.get("devDependencies", {})
        deps = data.get("dependencies", {})
        all_deps = {**dev_deps, **deps}
        self.assertIn("concurrently", all_deps)
        self.assertTrue(
            all_deps["concurrently"].endswith("9.2.4") or all_deps["concurrently"].startswith("^9"),
            f"unexpected concurrently version: {all_deps['concurrently']}",
        )

    def test_frontend_package_json_vite(self):
        """Verify frontend/package.json specifies vite (e.g. ^7.3.6)."""
        with open(self.package_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        dev_deps = data.get("devDependencies", {})
        deps = data.get("dependencies", {})
        all_deps = {**dev_deps, **deps}
        self.assertIn("vite", all_deps)
        self.assertTrue(
            all_deps["vite"].startswith("^7") or "7.3.6" in all_deps["vite"],
            f"unexpected vite version: {all_deps['vite']}",
        )

    def test_frontend_package_json_vue_i18n_absent(self):
        """Verify vue-i18n is not present in frontend/package.json (fork invariant)."""
        with open(self.package_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        dependencies = data.get("dependencies", {})
        dev_dependencies = data.get("devDependencies", {})
        self.assertNotIn(
            "vue-i18n",
            dependencies,
            "vue-i18n found in frontend dependencies, violating fork invariant",
        )
        self.assertNotIn(
            "vue-i18n",
            dev_dependencies,
            "vue-i18n found in frontend devDependencies, violating fork invariant",
        )
