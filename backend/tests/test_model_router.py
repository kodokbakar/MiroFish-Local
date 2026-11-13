"""Tests for Issue #69: task-based model router for staged inference."""

import os
from unittest.mock import patch

from app.config import Config
from app.storage.ner_extractor import NERExtractor
from app.services.report_agent import ReportAgent
from app.services.oasis_profile_generator import OasisProfileGenerator
from app.services.simulation_config_generator import SimulationConfigGenerator
from app.utils.llm_client import LLMClient


def test_config_model_router_fallback():
    """Verify fallback to LLM_MODEL_NAME when stage-specific env vars are absent."""
    with patch.dict(os.environ, {"LLM_MODEL_NAME": "default-model:latest"}, clear=False):
        # Refresh Config values
        assert Config.get_stage_model("ner") == "default-model:latest"
        assert Config.get_stage_model("report") == "default-model:latest"
        assert Config.get_stage_model("simulation") == "default-model:latest"
        assert Config.get_stage_model("unknown_stage") == "default-model:latest"


def test_config_model_router_stage_override():
    """Verify stage-specific models override default when configured."""
    env_overrides = {
        "LLM_MODEL_NAME": "default-model:latest",
        "LLM_MODEL_NER": "qwen2.5:7b",
        "LLM_MODEL_REPORT": "qwen2.5:32b",
        "LLM_MODEL_SIMULATION": "llama3.1:8b",
    }
    with patch.dict(os.environ, env_overrides, clear=False):
        assert Config.get_stage_model("ner") == "qwen2.5:7b"
        assert Config.get_stage_model("report") == "qwen2.5:32b"
        assert Config.get_stage_model("simulation") == "llama3.1:8b"


def test_ner_extractor_uses_routed_model():
    """NERExtractor should instantiate LLMClient with LLM_MODEL_NER if set."""
    env_overrides = {
        "LLM_API_KEY": "test-key",
        "LLM_MODEL_NAME": "default-32b",
        "LLM_MODEL_NER": "ner-7b",
    }
    with patch.dict(os.environ, env_overrides, clear=False):
        extractor = NERExtractor(cache_dir="/tmp/test_ner_cache")
        assert extractor.llm.model == "ner-7b"


def test_report_agent_uses_routed_model():
    """ReportAgent should instantiate LLMClient with LLM_MODEL_REPORT if set."""
    env_overrides = {
        "LLM_API_KEY": "test-key",
        "LLM_MODEL_NAME": "default-32b",
        "LLM_MODEL_REPORT": "report-70b",
    }
    with patch.dict(os.environ, env_overrides, clear=False):
        mock_tools = object()
        agent = ReportAgent(
            graph_id="g1",
            simulation_id="s1",
            simulation_requirement="test",
            graph_tools=mock_tools,
        )
        assert agent.llm.model == "report-70b"


def test_oasis_generators_use_routed_simulation_model():
    """OasisProfileGenerator and SimulationConfigGenerator use LLM_MODEL_SIMULATION if set."""
    env_overrides = {
        "LLM_API_KEY": "test-key",
        "LLM_MODEL_NAME": "default-32b",
        "LLM_MODEL_SIMULATION": "sim-8b",
    }
    with patch.dict(os.environ, env_overrides, clear=False):
        profile_gen = OasisProfileGenerator()
        sim_config_gen = SimulationConfigGenerator()
        assert profile_gen.model_name == "sim-8b"
        assert sim_config_gen.model_name == "sim-8b"
