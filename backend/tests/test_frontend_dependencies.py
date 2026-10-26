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
        self.package_lock_json_path = self.repo_root / "frontend" / "package-lock.json"

    def _load_package_json(self):
        self.assertTrue(
            self.package_json_path.is_file(),
            f"frontend/package.json not found at {self.package_json_path}",
        )
        with open(self.package_json_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _load_package_lock_json(self):
        self.assertTrue(
            self.package_lock_json_path.is_file(),
            f"frontend/package-lock.json not found at {self.package_lock_json_path}",
        )
        with open(self.package_lock_json_path, "r", encoding="utf-8") as f:
            return json.load(f)

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

    def test_frontend_package_lock_axios_version(self):
        """Verify frontend/package-lock.json has axios pinned to 1.18.1."""
        lock_data = self._load_package_lock_json()
        packages = lock_data.get("packages", {})
        self.assertIn("node_modules/axios", packages)
        self.assertEqual(
            packages["node_modules/axios"].get("version"),
            "1.18.1",
            "packages['node_modules/axios']['version'] must be '1.18.1'",
        )
        root_deps = packages.get("", {}).get("dependencies", {})
        self.assertEqual(
            root_deps.get("axios"),
            "1.18.1",
            "packages['']['dependencies']['axios'] must be '1.18.1'",
        )

    def test_frontend_package_lock_rollup_and_picomatch_versions(self):
        """Verify rollup is 4.60.1 and picomatch is 4.0.4 in frontend/package-lock.json."""
        lock_data = self._load_package_lock_json()
        packages = lock_data.get("packages", {})

        self.assertIn("node_modules/rollup", packages)
        self.assertEqual(
            packages["node_modules/rollup"].get("version"),
            "4.60.1",
            "packages['node_modules/rollup']['version'] must be '4.60.1'",
        )

        self.assertIn("node_modules/picomatch", packages)
        self.assertEqual(
            packages["node_modules/picomatch"].get("version"),
            "4.0.4",
            "packages['node_modules/picomatch']['version'] must be '4.0.4'",
        )

    def test_no_vue_i18n_in_package_or_lockfile(self):
        """Verify vue-i18n is not present in frontend package.json or package-lock.json."""
        pkg_data = self._load_package_json()
        pkg_deps = pkg_data.get("dependencies", {})
        pkg_dev_deps = pkg_data.get("devDependencies", {})
        self.assertNotIn("vue-i18n", pkg_deps, "vue-i18n must not be in package.json dependencies")
        self.assertNotIn("vue-i18n", pkg_dev_deps, "vue-i18n must not be in package.json devDependencies")

        lock_data = self._load_package_lock_json()
        packages = lock_data.get("packages", {})
        self.assertNotIn(
            "node_modules/vue-i18n",
            packages,
            "node_modules/vue-i18n must not be in package-lock.json packages",
        )
        root_deps = packages.get("", {}).get("dependencies", {})
        root_dev_deps = packages.get("", {}).get("devDependencies", {})
        self.assertNotIn("vue-i18n", root_deps, "vue-i18n must not be in package-lock.json root dependencies")
        self.assertNotIn("vue-i18n", root_dev_deps, "vue-i18n must not be in package-lock.json root devDependencies")
        top_level_deps = lock_data.get("dependencies", {})
        self.assertNotIn("vue-i18n", top_level_deps, "vue-i18n must not be in package-lock.json top-level dependencies")

    def test_frontend_package_lock_proxy_from_env_version(self):
        """Verify proxy-from-env in frontend/package-lock.json is 2.1.0."""
        lock_data = self._load_package_lock_json()
        packages = lock_data.get("packages", {})
        self.assertIn("node_modules/proxy-from-env", packages)
        self.assertEqual(
            packages["node_modules/proxy-from-env"].get("version"),
            "2.1.0",
            "packages['node_modules/proxy-from-env']['version'] must be '2.1.0'",
        )
