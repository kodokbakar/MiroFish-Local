"""
Tests for /api/graph/tasks endpoint.
"""

import unittest
from unittest.mock import patch

from app import create_app
from app.models.task import TaskManager, TaskStatus


class TestGraphTasksEndpoint(unittest.TestCase):
    """Test suite for graph tasks endpoint."""

    def setUp(self):
        self.task_manager = TaskManager()
        self.task_manager._tasks.clear()
        with patch("app.storage.Neo4jStorage"):
            self.app = create_app()
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def tearDown(self):
        self.task_manager._tasks.clear()

    def test_list_tasks_empty(self):
        """Verify GET /api/graph/tasks returns empty list when no tasks exist."""
        response = self.client.get("/api/graph/tasks")
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertEqual(data["count"], 0)
        self.assertEqual(data["data"], [])

    def test_list_tasks_with_tasks_success(self):
        """Verify GET /api/graph/tasks returns task dicts directly without error."""
        task_id = self.task_manager.create_task(
            task_type="build_graph",
            metadata={"graph_id": "test_graph_123"}
        )
        self.task_manager.update_task(
            task_id=task_id,
            status=TaskStatus.PROCESSING,
            progress=50,
            message="Processing nodes"
        )

        response = self.client.get("/api/graph/tasks")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 1)
        self.assertIsInstance(payload["data"], list)

        task_data = payload["data"][0]
        self.assertIsInstance(task_data, dict)
        self.assertEqual(task_data["task_id"], task_id)
        self.assertEqual(task_data["task_type"], "build_graph")
        self.assertEqual(task_data["status"], "processing")
        self.assertEqual(task_data["progress"], 50)
        self.assertEqual(task_data["message"], "Processing nodes")
        self.assertEqual(task_data["metadata"], {"graph_id": "test_graph_123"})

    def test_list_tasks_multiple_tasks(self):
        """Verify multiple tasks are returned ordered by created_at descending."""
        task_id_1 = self.task_manager.create_task("build_graph", metadata={"seq": 1})
        task_id_2 = self.task_manager.create_task("extract_entities", metadata={"seq": 2})

        response = self.client.get("/api/graph/tasks")
        self.assertEqual(response.status_code, 200)

        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["count"], 2)
        task_ids = [t["task_id"] for t in payload["data"]]
        self.assertIn(task_id_1, task_ids)
        self.assertIn(task_id_2, task_ids)


if __name__ == "__main__":
    unittest.main()
