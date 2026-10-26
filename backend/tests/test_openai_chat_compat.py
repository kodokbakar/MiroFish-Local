"""
Unit tests for OpenAI chat completions compatibility helpers (port 013).
"""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.utils.openai_chat_compat import (
    create_chat_completion,
    extract_chat_completion_text,
    is_gpt5_family,
)


class CompletionRecorder:
    def __init__(self, result=None, error=None):
        self.result = result or object()
        self.error = error
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return self.result


def client_for(recorder):
    return SimpleNamespace(chat=SimpleNamespace(completions=recorder))


def test_gpt5_uses_completion_token_limit_without_temperature():
    recorder = CompletionRecorder()
    messages = [{"role": "user", "content": "hello"}]

    result = create_chat_completion(
        client_for(recorder),
        model="gpt-5-2025-08-07",
        messages=messages,
        temperature=0.2,
        max_tokens=123,
        response_format={"type": "json_object"},
    )

    assert result is recorder.result
    assert recorder.calls == [
        {
            "model": "gpt-5-2025-08-07",
            "messages": messages,
            "max_completion_tokens": 123,
            "response_format": {"type": "json_object"},
        }
    ]


def test_legacy_model_preserves_original_request_shape():
    recorder = CompletionRecorder()
    messages = [{"role": "user", "content": "hello"}]

    create_chat_completion(
        client_for(recorder),
        model="third-party-chat-model",
        messages=messages,
        temperature=0.7,
        max_tokens=456,
        response_format={"type": "json_object"},
    )

    assert recorder.calls == [
        {
            "model": "third-party-chat-model",
            "messages": messages,
            "temperature": 0.7,
            "max_tokens": 456,
            "response_format": {"type": "json_object"},
        }
    ]


def test_provider_error_is_propagated_without_guessing_or_retrying():
    provider_error = RuntimeError("unsupported max_tokens due to a server outage")
    recorder = CompletionRecorder(error=provider_error)

    with pytest.raises(RuntimeError) as captured:
        create_chat_completion(
            client_for(recorder),
            model="legacy-model",
            messages=[],
            max_tokens=10,
        )

    assert captured.value is provider_error
    assert len(recorder.calls) == 1


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("gpt-5", True),
        ("gpt-5-turbo", True),
        ("GPT-5-mini", True),
        (" gpt-5-preview ", True),
        ("GPT-5", True),
        (" GPT-5.1-mini ", True),
        ("gpt-4.1", False),
        ("gpt-4", False),
        ("gpt-4o", False),
        ("gpt-4-turbo", False),
        ("o1-preview", False),
        ("claude-3-sonnet", False),
        ("deepseek-chat", False),
        ("my-gpt-5-proxy", False),
        ("", False),
        (None, False),
    ],
)
def test_gpt5_family_detection(model, expected):
    assert is_gpt5_family(model) is expected


def test_extracts_text_from_supported_content_shapes():
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=[
                        {"text": {"value": "first"}},
                        {"content": " second"},
                        SimpleNamespace(text=" third"),
                        SimpleNamespace(text={"value": " fourth"}),
                        SimpleNamespace(content=" fifth"),
                    ]
                )
            )
        ]
    )

    assert extract_chat_completion_text(response) == "first second third fourth fifth"
    assert extract_chat_completion_text(SimpleNamespace(choices=[])) == ""


def test_extracts_text_string_content_and_missing_fields():
    response_str = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="direct string content"))]
    )
    assert extract_chat_completion_text(response_str) == "direct string content"

    assert extract_chat_completion_text(None) == ""
    assert extract_chat_completion_text(SimpleNamespace()) == ""
    assert extract_chat_completion_text(SimpleNamespace(choices=[SimpleNamespace(message=None)])) == ""
    assert extract_chat_completion_text(SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=None))])) == ""


def test_create_chat_completion_without_optional_parameters():
    recorder = CompletionRecorder()
    messages = [{"role": "user", "content": "hi"}]

    create_chat_completion(
        client_for(recorder),
        model="gpt-5",
        messages=messages,
    )
    assert recorder.calls == [
        {
            "model": "gpt-5",
            "messages": messages,
        }
    ]

    create_chat_completion(
        client_for(recorder),
        model="gpt-4o",
        messages=messages,
    )
    assert recorder.calls[1] == [
        {
            "model": "gpt-4o",
            "messages": messages,
        }
    ][0]


def test_create_chat_completion_with_extra_params():
    client = MagicMock()
    fake_response = MagicMock()
    client.chat.completions.create.return_value = fake_response

    messages = [{"role": "user", "content": "hi"}]
    extra = {"extra_body": {"options": {"num_ctx": 8192}}}
    res = create_chat_completion(
        client,
        model="llama3",
        messages=messages,
        extra_params=extra,
    )

    assert res == fake_response
    client.chat.completions.create.assert_called_once_with(
        model="llama3",
        messages=messages,
        extra_body={"options": {"num_ctx": 8192}},
    )


def test_create_chat_completion_gpt5_with_extra_params():
    client = MagicMock()
    fake_response = MagicMock()
    client.chat.completions.create.return_value = fake_response

    messages = [{"role": "user", "content": "hi"}]
    extra = {
        "seed": 42,
        "timeout": 30.0,
        "extra_body": {"reasoning_effort": "low"},
    }
    res = create_chat_completion(
        client,
        model="gpt-5-nano",
        messages=messages,
        temperature=0.5,
        max_tokens=1024,
        response_format={"type": "json_object"},
        extra_params=extra,
    )

    assert res == fake_response
    client.chat.completions.create.assert_called_once_with(
        model="gpt-5-nano",
        messages=messages,
        max_completion_tokens=1024,
        response_format={"type": "json_object"},
        seed=42,
        timeout=30.0,
        extra_body={"reasoning_effort": "low"},
    )
    assert "temperature" not in client.chat.completions.create.call_args.kwargs


def test_llm_client_chat_with_think_tags_and_ollama():
    from app.utils.llm_client import LLMClient

    with patch("app.utils.llm_client.OpenAI"):
        # Test Ollama client
        client = LLMClient(api_key="mock", base_url="http://localhost:11434/v1", model="llama3")
        mock_response = MagicMock()
        mock_response.choices = [
            MagicMock(message=MagicMock(content="<think>internal thought</think>Final answer"))
        ]
        client.client.chat.completions.create.return_value = mock_response

        res = client.chat(messages=[{"role": "user", "content": "hi"}])
        assert res == "Final answer"

        called_kwargs = client.client.chat.completions.create.call_args.kwargs
        assert "extra_body" in called_kwargs
        assert called_kwargs["extra_body"]["options"]["num_ctx"] == client._num_ctx


def test_oasis_profile_generator_uses_chat_compat():
    from app.services.oasis_profile_generator import OasisProfileGenerator

    with patch("app.services.oasis_profile_generator.OpenAI"):
        generator = OasisProfileGenerator(api_key="mock", model_name="gpt-5")
        fake_profile = {
            "name": "Alex",
            "user_id": "alex_01",
            "bio": "Researcher in robotics.",
            "persona": "Curious and logical.",
            "country": "US",
            "profession": "Engineer",
        }
        mock_choice = MagicMock()
        mock_choice.finish_reason = "stop"
        mock_choice.message.content = json.dumps(fake_profile)
        mock_response = MagicMock(choices=[mock_choice])

        with patch("app.services.oasis_profile_generator.create_chat_completion", return_value=mock_response) as mock_create:
            res = generator._generate_profile_with_llm(
                entity_name="Alex",
                entity_type="person",
                entity_summary="An engineer",
                entity_attributes={},
                context="",
            )
            assert res["name"] == "Alex"
            assert mock_create.called
            assert mock_create.call_args.kwargs["model"] == "gpt-5"


def test_simulation_config_generator_uses_chat_compat():
    from app.services.simulation_config_generator import SimulationConfigGenerator

    with patch("app.services.simulation_config_generator.OpenAI"):
        generator = SimulationConfigGenerator(api_key="mock", model_name="gpt-5")
        data = {"platforms": ["twitter"], "status": "ok"}
        mock_choice = MagicMock()
        mock_choice.finish_reason = "stop"
        mock_choice.message.content = json.dumps(data)
        mock_response = MagicMock(choices=[mock_choice])

        with patch("app.services.simulation_config_generator.create_chat_completion", return_value=mock_response) as mock_create:
            res = generator._call_llm_with_retry(prompt="test prompt", system_prompt="test system")
            assert res == data
            assert mock_create.called
            assert mock_create.call_args.kwargs["model"] == "gpt-5"
