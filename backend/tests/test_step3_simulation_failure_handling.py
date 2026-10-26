"""
Tests verifying failure handling and polling termination in Step3Simulation.vue.
"""

from pathlib import Path
import unittest


class TestStep3SimulationFailureHandling(unittest.TestCase):
    """Test suite for Step3Simulation fatal failure handling contract."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.vue_file_path = (
            self.repo_root / "frontend" / "src" / "components" / "Step3Simulation.vue"
        )
        self.assertTrue(
            self.vue_file_path.is_file(),
            f"Step3Simulation.vue not found at {self.vue_file_path}",
        )
        self.content = self.vue_file_path.read_text(encoding="utf-8")

    def test_vue_file_checks_runner_status_failed(self):
        """Verify isFailed is defined from data.runner_status in fetchRunStatus."""
        self.assertIn("const fetchRunStatus = async", self.content)
        fetch_idx = self.content.find("const fetchRunStatus = async")
        detail_idx = self.content.find("const fetchRunStatusDetail = async")
        self.assertGreater(fetch_idx, -1)
        self.assertGreater(detail_idx, -1)

        fetch_section = self.content[fetch_idx:detail_idx]
        self.assertIn(
            "const isFailed = data.runner_status === 'failed'",
            fetch_section,
            "fetchRunStatus must check runner_status === 'failed'",
        )

    def test_vue_file_failure_branch_actions(self):
        """Verify failure branch stops polling, updates phase, emits error, and logs."""
        fetch_idx = self.content.find("const fetchRunStatus = async")
        detail_idx = self.content.find("const fetchRunStatusDetail = async")
        fetch_section = self.content[fetch_idx:detail_idx]

        self.assertIn(
            "else if (isFailed)",
            fetch_section,
            "fetchRunStatus must have an else if (isFailed) branch",
        )
        failure_idx = fetch_section.find("else if (isFailed)")
        failure_block = fetch_section[failure_idx : failure_idx + 300]

        self.assertIn(
            "phase.value = 2",
            failure_block,
            "Failure branch must set phase.value = 2",
        )
        self.assertIn(
            "stopPolling()",
            failure_block,
            "Failure branch must call stopPolling()",
        )
        self.assertIn(
            "emit('update-status', 'error')",
            failure_block,
            "Failure branch must emit 'update-status', 'error'",
        )
        self.assertIn(
            "addLog('✗ Simulation failed' + (data.error ? `: ${data.error}` : ''))",
            failure_block,
            "Failure branch must log '✗ Simulation failed' with optional data.error",
        )

    def test_failure_handling_logic_model(self):
        """Verify behavioral simulation of the runner status handling contract."""
        class Step3Model:
            def __init__(self):
                self.phase = 1
                self.polling_active = True
                self.emitted_status = "processing"
                self.logs = []

            def stop_polling(self):
                self.polling_active = False

            def emit(self, event, status):
                if event == "update-status":
                    self.emitted_status = status

            def add_log(self, msg):
                self.logs.append(msg)

            def handle_run_status(self, data):
                is_completed = (
                    data.get("runner_status") == "completed"
                    or data.get("runner_status") == "stopped"
                )
                is_failed = data.get("runner_status") == "failed"

                if is_completed:
                    self.add_log("✓ Simulation completed")
                    self.phase = 2
                    self.stop_polling()
                    self.emit("update-status", "completed")
                elif is_failed:
                    err_suffix = f": {data['error']}" if data.get("error") else ""
                    self.add_log(f"✗ Simulation failed{err_suffix}")
                    self.phase = 2
                    self.stop_polling()
                    self.emit("update-status", "error")

        # Case 1: runner failed with error message
        model = Step3Model()
        model.handle_run_status({"runner_status": "failed", "error": "Runner process crashed"})
        self.assertEqual(model.phase, 2)
        self.assertFalse(model.polling_active)
        self.assertEqual(model.emitted_status, "error")
        self.assertEqual(model.logs, ["✗ Simulation failed: Runner process crashed"])

        # Case 2: runner failed without error message
        model = Step3Model()
        model.handle_run_status({"runner_status": "failed"})
        self.assertEqual(model.phase, 2)
        self.assertFalse(model.polling_active)
        self.assertEqual(model.emitted_status, "error")
        self.assertEqual(model.logs, ["✗ Simulation failed"])

        # Case 3: runner still running
        model = Step3Model()
        model.handle_run_status({"runner_status": "running"})
        self.assertEqual(model.phase, 1)
        self.assertTrue(model.polling_active)
        self.assertEqual(model.emitted_status, "processing")
        self.assertEqual(model.logs, [])
