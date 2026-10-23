"""
Tests verifying chat history preserves duplicate user messages in Step5Interaction.vue.
"""

from pathlib import Path
import unittest


class TestStep5InteractionHistory(unittest.TestCase):
    """Test suite for Step5Interaction chat history slicing and behavior."""

    def setUp(self):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.vue_file_path = (
            self.repo_root / "frontend" / "src" / "components" / "Step5Interaction.vue"
        )
        self.assertTrue(
            self.vue_file_path.is_file(),
            f"Step5Interaction.vue not found at {self.vue_file_path}",
        )
        self.content = self.vue_file_path.read_text(encoding="utf-8")

    def test_vue_file_does_not_contain_buggy_filters(self):
        """Verify buggy content/role filtering is no longer present."""
        buggy_filter_1 = "filter(msg => msg.role !== 'user' || msg.content !== message)"
        buggy_filter_2 = "filter(msg => msg.content !== message)"

        self.assertNotIn(
            buggy_filter_1,
            self.content,
            "Found buggy filter in Step5Interaction.vue (filter role/content)",
        )
        self.assertNotIn(
            buggy_filter_2,
            self.content,
            "Found buggy filter in Step5Interaction.vue (filter content)",
        )

    def test_vue_file_contains_slice_in_handlers(self):
        """Verify .slice(0, -1) is used in both sendToReportAgent and sendToAgent."""
        self.assertIn("const sendToReportAgent = async", self.content)
        report_agent_idx = self.content.find("const sendToReportAgent = async")
        agent_idx = self.content.find("const sendToAgent = async")
        self.assertGreater(report_agent_idx, -1)
        self.assertGreater(agent_idx, -1)

        # sendToReportAgent section
        report_section = self.content[report_agent_idx:agent_idx]
        self.assertIn(
            ".slice(0, -1)",
            report_section,
            "sendToReportAgent must use .slice(0, -1) on chat history",
        )

        # sendToAgent section
        agent_section = self.content[agent_idx : agent_idx + 1500]
        self.assertIn(
            ".slice(0, -1)",
            agent_section,
            "sendToAgent must use .slice(0, -1) on chat history",
        )

    def test_array_slicing_behavior_model_preserves_duplicate_messages(self):
        """Demonstrate old filter drops duplicate user messages while slice(0, -1) preserves them."""
        # Simulated chat history after user appends current message before sending
        chat_history = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "hello"},
        ]
        current_message = "hello"

        # Model old filter 1 (from sendToReportAgent):
        # filter(msg => msg.role !== 'user' || msg.content !== message)
        old_filter_1 = [
            msg
            for msg in chat_history
            if msg["role"] != "user" or msg["content"] != current_message
        ]

        # Model old filter 2 (from sendToAgent):
        # filter(msg => msg.content !== message)
        old_filter_2 = [
            msg for msg in chat_history if msg["content"] != current_message
        ]

        # Both old filters erroneously drop previous "hello" user message
        self.assertEqual(old_filter_1, [{"role": "assistant", "content": "hi"}])
        self.assertEqual(old_filter_2, [{"role": "assistant", "content": "hi"}])

        # New logic: slice(0, -1)
        new_slice = chat_history[:-1]

        # Correctly retains prior conversation including identical previous message
        self.assertEqual(
            new_slice,
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        )
        self.assertEqual(len(new_slice), 2)
        self.assertEqual(new_slice[0]["content"], "hello")
