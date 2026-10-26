"""
Tests verifying failure handling and polling termination in Step3Simulation.vue.
"""

import json
from pathlib import Path
import shutil
import subprocess
import unittest


class Step3Model:
    """Behavioral simulation of Step3Simulation runner status handling."""

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
        runner_status = data.get("runner_status") if isinstance(data, dict) else None
        is_completed = (
            runner_status == "completed"
            or runner_status == "stopped"
        )
        is_failed = runner_status == "failed"

        if is_completed:
            self.add_log("✓ Simulation completed")
            self.phase = 2
            self.stop_polling()
            self.emit("update-status", "completed")
        elif is_failed:
            err = data.get("error") if isinstance(data, dict) else None
            err_suffix = f": {err}" if err else ""
            self.add_log(f"✗ Simulation failed{err_suffix}")
            self.phase = 2
            self.stop_polling()
            self.emit("update-status", "error")


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
        # Use module-level Step3Model for simulation
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

    def test_runner_status_unexpected_strings_and_edge_values(self):
        """Verify non-failed runner_status strings do not halt polling or emit error."""
        non_failed_statuses = [
            "error",
            "FAILED",
            "failed ",
            "unknown",
            "running",
            "pending",
            "",
            None,
            123,
            False,
        ]
        for status in non_failed_statuses:
            with self.subTest(status=status):
                model = Step3Model()
                payload = (
                    {"runner_status": status, "error": "Some message"}
                    if status is not None
                    else {"error": "Some message"}
                )
                model.handle_run_status(payload)
                self.assertEqual(model.phase, 1)
                self.assertTrue(model.polling_active)
                self.assertEqual(model.emitted_status, "processing")
                self.assertEqual(model.logs, [])

    def test_error_field_edge_cases(self):
        """Verify error field formatting when runner_status is failed."""
        test_cases = [
            (None, "✗ Simulation failed"),
            ("", "✗ Simulation failed"),
            (False, "✗ Simulation failed"),
            (0, "✗ Simulation failed"),
            ("Runner crashed with exit code 1", "✗ Simulation failed: Runner crashed with exit code 1"),
            ("Out of memory (OOM)", "✗ Simulation failed: Out of memory (OOM)"),
            (500, "✗ Simulation failed: 500"),
        ]
        for err_input, expected_log in test_cases:
            with self.subTest(error=err_input):
                model = Step3Model()
                payload = {"runner_status": "failed"}
                if err_input is not None:
                    payload["error"] = err_input
                model.handle_run_status(payload)
                self.assertEqual(model.phase, 2)
                self.assertFalse(model.polling_active)
                self.assertEqual(model.emitted_status, "error")
                self.assertEqual(model.logs, [expected_log])

    def test_node_or_bun_js_runtime_edge_cases(self):
        """Execute JS runtime evaluation with Node or Bun to verify JS ternary and equality."""
        runtime = shutil.which("node") or shutil.which("bun")
        if not runtime:
            self.skipTest("Neither node nor bun executable found in PATH")

        js_script = """
        const testCases = [
            { runner_status: 'failed', error: 'OOM', expected_log: '✗ Simulation failed: OOM', expected_failed: true, expected_phase: 2, expected_status: 'error' },
            { runner_status: 'failed', error: '', expected_log: '✗ Simulation failed', expected_failed: true, expected_phase: 2, expected_status: 'error' },
            { runner_status: 'failed', error: null, expected_log: '✗ Simulation failed', expected_failed: true, expected_phase: 2, expected_status: 'error' },
            { runner_status: 'failed', error: undefined, expected_log: '✗ Simulation failed', expected_failed: true, expected_phase: 2, expected_status: 'error' },
            { runner_status: 'failed', error: false, expected_log: '✗ Simulation failed', expected_failed: true, expected_phase: 2, expected_status: 'error' },
            { runner_status: 'failed', error: 0, expected_log: '✗ Simulation failed', expected_failed: true, expected_phase: 2, expected_status: 'error' },
            { runner_status: 'error', error: 'msg', expected_log: null, expected_failed: false, expected_phase: 1, expected_status: 'processing' },
            { runner_status: 'FAILED', error: 'msg', expected_log: null, expected_failed: false, expected_phase: 1, expected_status: 'processing' },
            { runner_status: 'failed ', error: 'msg', expected_log: null, expected_failed: false, expected_phase: 1, expected_status: 'processing' },
            { runner_status: 'completed', error: null, expected_log: '✓ Simulation completed', expected_failed: false, expected_phase: 2, expected_status: 'completed' },
            { runner_status: 'stopped', error: null, expected_log: '✓ Simulation completed', expected_failed: false, expected_phase: 2, expected_status: 'completed' },
            { runner_status: 'running', error: null, expected_log: null, expected_failed: false, expected_phase: 1, expected_status: 'processing' },
            { runner_status: null, error: null, expected_log: null, expected_failed: false, expected_phase: 1, expected_status: 'processing' },
            { runner_status: '', error: null, expected_log: null, expected_failed: false, expected_phase: 1, expected_status: 'processing' }
        ];

        const results = testCases.map(data => {
            let phase = 1;
            let pollingActive = true;
            let emitted = 'processing';
            let log = null;
            const addLog = (m) => { log = m; };
            const stopPolling = () => { pollingActive = false; };
            const emit = (event, val) => { if (event === 'update-status') emitted = val; };

            const isCompleted = data.runner_status === 'completed' || data.runner_status === 'stopped';
            const isFailed = data.runner_status === 'failed';

            if (isCompleted) {
                addLog('✓ Simulation completed');
                phase = 2;
                stopPolling();
                emit('update-status', 'completed');
            } else if (isFailed) {
                addLog('✗ Simulation failed' + (data.error ? `: ${data.error}` : ''));
                phase = 2;
                stopPolling();
                emit('update-status', 'error');
            }

            return {
                isFailed,
                phase,
                pollingActive,
                emitted,
                log,
                expected_log: data.expected_log,
                expected_failed: data.expected_failed,
                expected_phase: data.expected_phase,
                expected_status: data.expected_status
            };
        });

        console.log(JSON.stringify(results));
        """
        res = subprocess.run(
            [runtime, "-e", js_script],
            capture_output=True,
            text=True,
            check=True,
        )
        records = json.loads(res.stdout.strip())
        for idx, item in enumerate(records):
            with self.subTest(case_index=idx):
                self.assertEqual(item["isFailed"], item["expected_failed"])
                self.assertEqual(item["phase"], item["expected_phase"])
                self.assertEqual(item["emitted"], item["expected_status"])
                self.assertEqual(item["log"], item["expected_log"])
                if item["expected_failed"]:
                    self.assertFalse(item["pollingActive"])
                elif item["expected_status"] == "completed":
                    self.assertFalse(item["pollingActive"])
                else:
                    self.assertTrue(item["pollingActive"])
