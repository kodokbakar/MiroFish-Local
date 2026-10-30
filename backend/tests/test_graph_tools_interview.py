import unittest
from unittest.mock import MagicMock, patch

from app.services.graph_tools import GraphToolsService


class TestInterviewAgentBio(unittest.TestCase):
    def test_selection_accepts_explicitly_null_bio(self):
        profile = {"realname": "Agent with no bio", "bio": None}
        llm_client = MagicMock()
        llm_client.chat_json.return_value = {
            "selected_indices": [0],
            "reasoning": "Selected for the interview",
        }
        service = GraphToolsService(storage=MagicMock(), llm_client=llm_client)

        selected, indices, _ = service._select_agents_for_interview(
            profiles=[profile],
            interview_requirement="Discuss the topic",
            simulation_requirement="",
            max_agents=1,
        )

        self.assertEqual(selected, [profile])
        self.assertEqual(indices, [0])
        self.assertIn('"bio": ""', llm_client.chat_json.call_args.kwargs["messages"][1]["content"])

    def test_interview_result_uses_empty_bio_when_profile_bio_is_null(self):
        profile = {"realname": "Agent with no bio", "bio": None}
        service = GraphToolsService(storage=MagicMock(), llm_client=MagicMock())
        service._load_agent_profiles = MagicMock(return_value=[profile])
        service._select_agents_for_interview = MagicMock(
            return_value=([profile], [0], "Selected for the interview")
        )
        service._generate_interview_summary = MagicMock(return_value="Summary")
        api_result = {
            "success": True,
            "result": {
                "results": {
                    "twitter_0": {"response": "A response from Twitter."},
                    "reddit_0": {"response": "A response from Reddit."},
                }
            },
        }

        with patch(
            "app.services.simulation_runner.SimulationRunner.interview_agents_batch",
            return_value=api_result,
        ):
            result = service.interview_agents(
                simulation_id="test-simulation",
                interview_requirement="Discuss the topic",
                custom_questions=["What happened?"],
            )

        self.assertEqual(result.interviewed_count, 1)
        self.assertEqual(result.interviews[0].agent_bio, "")
