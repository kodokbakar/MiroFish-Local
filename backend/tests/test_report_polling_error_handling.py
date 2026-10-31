"""
Tests verifying frontend report error handling, consecutive polling error stops,
and retry affordances in Step4Report.vue (Issue #30).
"""

from pathlib import Path
import unittest


class TestStep4ReportStructuralInvariants(unittest.TestCase):
    """Verify Step4Report.vue structural invariants for error handling and polling stops."""

    @classmethod
    def setUpClass(cls):
        cls.repo_root = Path(__file__).resolve().parents[2]
        cls.vue_file = cls.repo_root / "frontend" / "src" / "components" / "Step4Report.vue"
        cls.assertTrue(
            cls.vue_file.is_file(),
            f"Step4Report.vue not found at {cls.vue_file}",
        )
        cls.content = cls.vue_file.read_text(encoding="utf-8")

    def test_consecutive_error_constants_and_state_declared(self):
        """Verify error counters and MAX_CONSECUTIVE_ERRORS are properly declared."""
        self.assertIn("const consecutiveErrors = ref(0)", self.content)
        self.assertIn("const MAX_CONSECUTIVE_ERRORS = 5", self.content)
        self.assertIn("let agentErrors = 0", self.content)
        self.assertIn("let consoleErrors = 0", self.content)

    def test_handle_polling_error_implementation(self):
        """Verify handlePollingError tracks failures, updates error state, and stops polling."""
        fn_start = self.content.find("const handlePollingError = (err, source) => {")
        self.assertNotEqual(fn_start, -1, "handlePollingError function missing")

        fn_end = self.content.find("const retryPolling = () => {", fn_start)
        self.assertNotEqual(fn_end, -1, "retryPolling function missing after handlePollingError")

        fn_body = self.content[fn_start:fn_end]

        # Increments counters
        self.assertIn("consecutiveErrors.value++", fn_body)
        self.assertIn("if (source === 'agent log')", fn_body)
        self.assertIn("agentErrors++", fn_body)
        self.assertIn("consoleErrors++", fn_body)

        # Threshold check
        self.assertIn("consecutiveErrors.value >= MAX_CONSECUTIVE_ERRORS", fn_body)
        self.assertIn("agentErrors >= MAX_CONSECUTIVE_ERRORS", fn_body)
        self.assertIn("consoleErrors >= MAX_CONSECUTIVE_ERRORS", fn_body)
        self.assertIn("!hasError.value", fn_body)

        # Error state updates
        self.assertIn("hasError.value = true", fn_body)
        self.assertIn("errorMessage.value = 'Failed to fetch report logs after multiple attempts. Server may be unreachable.'", fn_body)
        self.assertIn("currentSectionIndex.value = null", fn_body)
        self.assertIn("emit('update-status', 'error')", fn_body)
        self.assertIn("stopPolling()", fn_body)
        self.assertIn("addLog('Report polling stopped: connection to server lost')", fn_body)

    def test_retry_polling_implementation(self):
        """Verify retryPolling resets error state, resets counters, and starts polling."""
        fn_start = self.content.find("const retryPolling = () => {")
        self.assertNotEqual(fn_start, -1, "retryPolling function missing")

        fn_end = self.content.find("const fetchAgentLog = async () => {", fn_start)
        self.assertNotEqual(fn_end, -1, "fetchAgentLog missing after retryPolling")

        fn_body = self.content[fn_start:fn_end]

        self.assertIn("hasError.value = false", fn_body)
        self.assertIn("errorMessage.value = ''", fn_body)
        self.assertIn("consecutiveErrors.value = 0", fn_body)
        self.assertIn("agentErrors = 0", fn_body)
        self.assertIn("consoleErrors = 0", fn_body)
        self.assertIn("startPolling()", fn_body)

    def test_fetch_agent_log_resets_counters_and_handles_error(self):
        """Verify fetchAgentLog resets error counters on success and routes catch to handlePollingError."""
        fn_start = self.content.find("const fetchAgentLog = async () => {")
        self.assertNotEqual(fn_start, -1, "fetchAgentLog missing")

        fn_end = self.content.find("const extractFinalContent =", fn_start)
        self.assertNotEqual(fn_end, -1, "extractFinalContent missing after fetchAgentLog")

        fn_body = self.content[fn_start:fn_end]

        # Reset on successful data retrieval
        self.assertIn("consecutiveErrors.value = 0", fn_body)
        self.assertIn("agentErrors = 0", fn_body)

        # Backend error event handling
        self.assertIn("if (log.action === 'error')", fn_body)
        self.assertIn("addLog('Report generation failed: ' + errorMessage.value)", fn_body)

        # Catch routes to handlePollingError
        self.assertIn("handlePollingError(err, 'agent log')", fn_body)

    def test_fetch_console_log_resets_counters_and_handles_error(self):
        """Verify fetchConsoleLog resets error counters on success and routes catch to handlePollingError."""
        fn_start = self.content.find("const fetchConsoleLog = async () => {")
        self.assertNotEqual(fn_start, -1, "fetchConsoleLog missing")

        fn_end = self.content.find("const startPolling = () => {", fn_start)
        self.assertNotEqual(fn_end, -1, "startPolling missing after fetchConsoleLog")

        fn_body = self.content[fn_start:fn_end]

        # Reset on successful data retrieval
        self.assertIn("consecutiveErrors.value = 0", fn_body)
        self.assertIn("consoleErrors = 0", fn_body)

        # Catch routes to handlePollingError
        self.assertIn("handlePollingError(err, 'console log')", fn_body)

    def test_report_id_watcher_resets_error_counters(self):
        """Verify watching props.reportId clears error counters and flags."""
        watch_start = self.content.find("watch(() => props.reportId, (newId) => {")
        self.assertNotEqual(watch_start, -1, "watch props.reportId missing")

        watch_end = self.content.find("}, { immediate: true })", watch_start)
        self.assertNotEqual(watch_end, -1, "watch end missing")

        watch_body = self.content[watch_start:watch_end]

        self.assertIn("hasError.value = false", watch_body)
        self.assertIn("errorMessage.value = ''", watch_body)
        self.assertIn("consecutiveErrors.value = 0", watch_body)
        self.assertIn("agentErrors = 0", watch_body)
        self.assertIn("consoleErrors = 0", watch_body)

    def test_template_error_banner_and_retry_button(self):
        """Verify template includes accessible error banner and retry button."""
        self.assertIn('<div v-if="hasError" class="report-error" role="alert">', self.content)
        self.assertIn('<strong>Report generation failed</strong>', self.content)
        self.assertIn('<span>{{ errorMessage }}</span>', self.content)
        self.assertIn('<button type="button" class="retry-btn" @click="retryPolling">Retry Polling</button>', self.content)

    def test_retry_button_styles_defined(self):
        """Verify CSS styles for .retry-btn are present."""
        self.assertIn('.retry-btn {', self.content)
        self.assertIn('.retry-btn:hover {', self.content)
        self.assertIn('.retry-btn:active {', self.content)


class Step4ReportSimulationModel:
    """Simulation model of Step4Report polling and error handling state machine."""

    def __init__(self, max_consecutive_errors=5):
        self.max_consecutive_errors = max_consecutive_errors
        self.has_error = False
        self.error_message = ""
        self.consecutive_errors = 0
        self.agent_errors = 0
        self.console_errors = 0
        self.is_polling = False
        self.emitted_status = []
        self.logs = []
        self.current_section_index = None

    def start_polling(self):
        self.is_polling = True

    def stop_polling(self):
        self.is_polling = False

    def add_log(self, msg):
        self.logs.append(msg)

    def emit_status(self, status):
        self.emitted_status.append(status)

    def handle_polling_error(self, source, err="Network Error"):
        self.consecutive_errors += 1
        if source == "agent log":
            self.agent_errors += 1
        else:
            self.console_errors += 1

        if (
            self.consecutive_errors >= self.max_consecutive_errors
            or self.agent_errors >= self.max_consecutive_errors
            or self.console_errors >= self.max_consecutive_errors
        ) and not self.has_error:
            self.has_error = True
            self.error_message = (
                "Failed to fetch report logs after multiple attempts. Server may be unreachable."
            )
            self.current_section_index = None
            self.emit_status("error")
            self.stop_polling()
            self.add_log("Report polling stopped: connection to server lost")

    def handle_agent_log_success(self, logs=None):
        self.consecutive_errors = 0
        self.agent_errors = 0
        if logs:
            for log in logs:
                if log.get("action") == "error":
                    self.has_error = True
                    details = log.get("details", {})
                    self.error_message = details.get("message") or details.get("error") or "Report generation failed"
                    self.current_section_index = None
                    self.emit_status("error")
                    self.stop_polling()
                    self.add_log(f"Report generation failed: {self.error_message}")

    def handle_console_log_success(self, logs=None):
        self.consecutive_errors = 0
        self.console_errors = 0

    def retry_polling(self):
        self.has_error = False
        self.error_message = ""
        self.consecutive_errors = 0
        self.agent_errors = 0
        self.console_errors = 0
        self.start_polling()


class TestStep4ReportPollingStateMachine(unittest.TestCase):
    """Behavioral unit tests simulating the Step4Report polling state machine."""

    def test_single_or_sporadic_errors_do_not_stop_polling(self):
        model = Step4ReportSimulationModel(max_consecutive_errors=5)
        model.start_polling()

        # 3 errors
        model.handle_polling_error("agent log")
        model.handle_polling_error("console log")
        model.handle_polling_error("agent log")

        self.assertTrue(model.is_polling)
        self.assertFalse(model.has_error)
        self.assertEqual(model.consecutive_errors, 3)

        # Success resets counter
        model.handle_agent_log_success([])
        self.assertEqual(model.consecutive_errors, 0)
        self.assertEqual(model.agent_errors, 0)

        # 2 more errors
        model.handle_polling_error("console log")
        model.handle_polling_error("console log")

        self.assertTrue(model.is_polling)
        self.assertFalse(model.has_error)
        self.assertEqual(model.consecutive_errors, 2)

    def test_consecutive_agent_log_errors_reach_threshold_and_stop(self):
        model = Step4ReportSimulationModel(max_consecutive_errors=5)
        model.start_polling()

        for i in range(4):
            model.handle_polling_error("agent log")
            self.assertTrue(model.is_polling)
            self.assertFalse(model.has_error)

        # 5th error hits threshold
        model.handle_polling_error("agent log")
        self.assertFalse(model.is_polling)
        self.assertTrue(model.has_error)
        self.assertIn("Server may be unreachable", model.error_message)
        self.assertIn("error", model.emitted_status)
        self.assertIn("Report polling stopped: connection to server lost", model.logs)

    def test_consecutive_console_log_errors_reach_threshold_and_stop(self):
        model = Step4ReportSimulationModel(max_consecutive_errors=5)
        model.start_polling()

        for _ in range(5):
            model.handle_polling_error("console log")

        self.assertFalse(model.is_polling)
        self.assertTrue(model.has_error)
        self.assertEqual(model.console_errors, 5)
        self.assertIn("error", model.emitted_status)

    def test_interleaved_errors_reach_consecutive_threshold_and_stop(self):
        model = Step4ReportSimulationModel(max_consecutive_errors=5)
        model.start_polling()

        # Interleave agent log and console log errors (neither reaches 5 alone, but consecutive does)
        sources = ["agent log", "console log", "agent log", "console log", "agent log"]
        for src in sources:
            model.handle_polling_error(src)

        self.assertEqual(model.consecutive_errors, 5)
        self.assertEqual(model.agent_errors, 3)
        self.assertEqual(model.console_errors, 2)
        self.assertFalse(model.is_polling)
        self.assertTrue(model.has_error)

    def test_retry_polling_clears_error_and_resumes(self):
        model = Step4ReportSimulationModel(max_consecutive_errors=5)
        model.start_polling()

        for _ in range(5):
            model.handle_polling_error("agent log")

        self.assertFalse(model.is_polling)
        self.assertTrue(model.has_error)

        # Retry
        model.retry_polling()
        self.assertTrue(model.is_polling)
        self.assertFalse(model.has_error)
        self.assertEqual(model.error_message, "")
        self.assertEqual(model.consecutive_errors, 0)
        self.assertEqual(model.agent_errors, 0)
        self.assertEqual(model.console_errors, 0)

    def test_backend_error_log_action_immediately_halts(self):
        model = Step4ReportSimulationModel(max_consecutive_errors=5)
        model.start_polling()

        error_log = {
            "action": "error",
            "details": {"message": "Agent execution timeout"},
        }
        model.handle_agent_log_success([error_log])

        self.assertFalse(model.is_polling)
        self.assertTrue(model.has_error)
        self.assertEqual(model.error_message, "Agent execution timeout")
        self.assertIn("error", model.emitted_status)
        self.assertIn("Report generation failed: Agent execution timeout", model.logs)
