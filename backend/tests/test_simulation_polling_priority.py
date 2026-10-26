"""
Regression tests for runner failure priority over platform completion flags in Step3Simulation.vue (port 017).

Verifies that when runner_status is 'failed', the simulation terminates with an error
even if platform completion flags (e.g. twitter_completed, reddit_completed) are true.
"""

import json
from pathlib import Path
import re
import shutil
import subprocess
import unittest


class TestSimulationPollingPrioritySource(unittest.TestCase):
    """Structural invariant tests for Step3Simulation.vue source code."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.vue_file = cls.repo_root / "frontend" / "src" / "components" / "Step3Simulation.vue"
        cls.assertTrue(
            cls.vue_file.is_file(),
            f"Step3Simulation.vue not found at {cls.vue_file}",
        )
        cls.content = cls.vue_file.read_text(encoding="utf-8")

        # Locate fetchRunStatus
        cls.fetch_fn_idx = cls.content.find("const fetchRunStatus = async () => {")
        cls.assertTrue(
            cls.fetch_fn_idx != -1,
            "Could not locate 'const fetchRunStatus = async () => {' in Step3Simulation.vue",
        )

        # Locate checkPlatformsCompleted
        cls.check_platforms_idx = cls.content.find("const checkPlatformsCompleted = (data) => {")
        cls.assertTrue(
            cls.check_platforms_idx != -1,
            "Could not locate 'const checkPlatformsCompleted = (data) => {' in Step3Simulation.vue",
        )

        # Extract polling logic inside fetchRunStatus
        cls.fetch_body = cls.content[cls.fetch_fn_idx : cls.check_platforms_idx]

    def test_failure_check_precedes_completion_check(self):
        """isFailed branch must appear before isCompleted/platformsCompleted branch."""
        failed_branch_idx = self.fetch_body.find("if (isFailed)")
        completion_branch_idx = self.fetch_body.find("else if (isCompleted || platformsCompleted)")

        self.assertNotEqual(
            failed_branch_idx,
            -1,
            "Missing 'if (isFailed)' branch in fetchRunStatus",
        )
        self.assertNotEqual(
            completion_branch_idx,
            -1,
            "Missing 'else if (isCompleted || platformsCompleted)' branch in fetchRunStatus",
        )
        self.assertLess(
            failed_branch_idx,
            completion_branch_idx,
            "Runner failure check must precede platform completion check to prevent masking errors",
        )

    def test_no_vue_i18n_leakage(self):
        """Step3Simulation.vue should use plain string literals without vue-i18n calls."""
        # Upstream used t('log.simFailed'), which leaks vue-i18n into offline repo
        self.assertNotIn(
            "t('log.simFailed')",
            self.fetch_body,
            "Found upstream vue-i18n reference t('log.simFailed') in Step3Simulation.vue",
        )
        self.assertIn(
            "addLog('✗ Simulation failed'",
            self.fetch_body,
            "Expected plain English string '✗ Simulation failed' in Step3Simulation.vue",
        )

    def test_error_handling_emits_error_status(self):
        """Failure branch must emit 'error' and update phase."""
        self.assertIn("emit('update-status', 'error')", self.fetch_body)
        self.assertIn("emit('update-status', 'completed')", self.fetch_body)
        self.assertIn("stopPolling()", self.fetch_body)


class TestSimulationPollingExecution(unittest.TestCase):
    """Behavioral execution tests running extracted Vue logic in Node/Bun runtime."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.vue_file = cls.repo_root / "frontend" / "src" / "components" / "Step3Simulation.vue"
        cls.content = cls.vue_file.read_text(encoding="utf-8")

        # Extract checkPlatformsCompleted function
        match_fn = re.search(
            r"(const checkPlatformsCompleted\s*=\s*\(data\)\s*=>\s*\{.*?\n\})",
            cls.content,
            re.DOTALL,
        )
        cls.assertTrue(match_fn is not None, "Failed to extract checkPlatformsCompleted")
        cls.fn_source = match_fn.group(1)

        # Extract decision block
        match_block = re.search(
            r"(const isCompleted = .*?emit\('update-status', 'completed'\)\s*\n\s*\})",
            cls.content,
            re.DOTALL,
        )
        cls.assertTrue(match_block is not None, "Failed to extract status decision block")
        cls.block_source = match_block.group(1)

        cls.js_runtime = shutil.which("node") or shutil.which("bun")

    def _execute_harness(self, test_payloads):
        """Execute simulation status logic in JS runtime and return outcomes."""
        if not self.js_runtime:
            self.skipTest("Neither node nor bun executable found in PATH")

        payloads_json = json.dumps(test_payloads)
        harness_js = f"""
{self.fn_source}

function runPollingLogic(data) {{
  const logs = [];
  let phase = {{ value: 1 }};
  let pollingStopped = false;
  let statusUpdate = null;

  function addLog(msg) {{
    logs.push(msg);
  }}

  function stopPolling() {{
    pollingStopped = true;
  }}

  function emit(eventName, val) {{
    if (eventName === 'update-status') {{
      statusUpdate = val;
    }}
  }}

  {self.block_source}

  return {{
    logs,
    phase: phase.value,
    pollingStopped,
    statusUpdate
  }};
}}

const inputs = {payloads_json};
const outputs = {{}};
for (const [key, payload] of Object.entries(inputs)) {{
  outputs[key] = runPollingLogic(payload);
}}
console.log(JSON.stringify(outputs));
"""
        proc = subprocess.run(
            [self.js_runtime, "-e", harness_js],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(proc.stdout)

    def test_runner_failed_with_both_platforms_completed(self):
        """Runner failure must take precedence over platforms reporting complete."""
        payload = {
            "runner_status": "failed",
            "twitter_actions_count": 10,
            "twitter_completed": True,
            "reddit_actions_count": 8,
            "reddit_completed": True,
            "error": "Agent process exited with code 1",
        }
        out = self._execute_harness({"test": payload})["test"]

        self.assertEqual(out["statusUpdate"], "error")
        self.assertEqual(out["phase"], 2)
        self.assertTrue(out["pollingStopped"])
        self.assertIn("✗ Simulation failed: Agent process exited with code 1", out["logs"])
        self.assertNotIn("✓ Simulation completed", out["logs"])

    def test_runner_failed_without_explicit_error_message(self):
        """Runner failure without error text still resolves to error status."""
        payload = {
            "runner_status": "failed",
            "twitter_completed": True,
            "reddit_completed": True,
        }
        out = self._execute_harness({"test": payload})["test"]

        self.assertEqual(out["statusUpdate"], "error")
        self.assertEqual(out["phase"], 2)
        self.assertTrue(out["pollingStopped"])
        self.assertIn("✗ Simulation failed", out["logs"])
        self.assertNotIn("✓ Simulation completed", out["logs"])

    def test_runner_completed_resolves_to_completed(self):
        """Explicit runner_status 'completed' resolves to completed."""
        payload = {
            "runner_status": "completed",
            "twitter_actions_count": 5,
            "twitter_completed": True,
            "reddit_actions_count": 5,
            "reddit_completed": True,
        }
        out = self._execute_harness({"test": payload})["test"]

        self.assertEqual(out["statusUpdate"], "completed")
        self.assertEqual(out["phase"], 2)
        self.assertTrue(out["pollingStopped"])
        self.assertIn("✓ Simulation completed", out["logs"])

    def test_platforms_completed_while_runner_status_running(self):
        """If runner_status is still 'running' but all platforms finished, resolve to completed."""
        payload = {
            "runner_status": "running",
            "twitter_actions_count": 5,
            "twitter_completed": True,
            "reddit_actions_count": 5,
            "reddit_completed": True,
        }
        out = self._execute_harness({"test": payload})["test"]

        self.assertEqual(out["statusUpdate"], "completed")
        self.assertEqual(out["phase"], 2)
        self.assertTrue(out["pollingStopped"])
        self.assertIn("✓ Detected all platform simulations have ended", out["logs"])
        self.assertIn("✓ Simulation completed", out["logs"])

    def test_running_and_platforms_not_completed_continues_polling(self):
        """While running and platforms incomplete, neither terminal branch triggers."""
        payload = {
            "runner_status": "running",
            "twitter_actions_count": 5,
            "twitter_completed": False,
            "reddit_actions_count": 5,
            "reddit_completed": False,
        }
        out = self._execute_harness({"test": payload})["test"]

        self.assertIsNone(out["statusUpdate"])
        self.assertEqual(out["phase"], 1)
        self.assertFalse(out["pollingStopped"])
        self.assertEqual(out["logs"], [])

    def test_regression_buggy_order_contrast(self):
        """Verify that an inverted branch order reproduces the masking bug."""
        if not self.js_runtime:
            self.skipTest("Neither node nor bun executable found in PATH")

        inverted_harness = f"""
{self.fn_source}

function runBuggyLogic(data) {{
  let statusUpdate = null;
  const isCompleted = data.runner_status === 'completed' || data.runner_status === 'stopped';
  const isFailed = data.runner_status === 'failed';
  const platformsCompleted = checkPlatformsCompleted(data);

  // BUGGY ORDER: completion branch before failure branch
  if (isCompleted || platformsCompleted) {{
    statusUpdate = 'completed';
  }} else if (isFailed) {{
    statusUpdate = 'error';
  }}
  return statusUpdate;
}}

const payload = {{
  runner_status: 'failed',
  twitter_actions_count: 5,
  twitter_completed: true,
  reddit_actions_count: 5,
  reddit_completed: true
}};

console.log(JSON.stringify({{ buggyResult: runBuggyLogic(payload) }}));
"""
        proc = subprocess.run(
            [self.js_runtime, "-e", inverted_harness],
            capture_output=True,
            text=True,
            check=True,
        )
        res = json.loads(proc.stdout)
        # In the buggy logic, 'completed' masks 'error'
        self.assertEqual(res["buggyResult"], "completed")


if __name__ == "__main__":
    unittest.main()
