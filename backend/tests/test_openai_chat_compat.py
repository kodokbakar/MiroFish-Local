"""
Unit tests for OpenAI chat completions compatibility helpers (port 012).
"""

from unittest.mock import MagicMock
import pytest

from app.utils.openai_chat_compat import (
    is_gpt5_family,
    _is_unsupported_param_error,
    _extract_error_message,
    create_chat_completion,
    extract_chat_completion_text,
)


def test_is_gpt5_family():
    # Valid GPT-5 family variants
    assert is_gpt5_family("gpt-5") is True
    assert is_gpt5_family("gpt-5-turbo") is True
    assert is_gpt5_family("GPT-5-mini") is True
    assert is_gpt5_family(" gpt-5-preview ") is True
    assert is_gpt5_family("GPT-5") is True

    # Non GPT-5 models
    assert is_gpt5_family(None) is False
    assert is_gpt5_family("") is False
    assert is_gpt5_family("gpt-4") is False
    assert is_gpt5_family("gpt-4o") is False
    assert is_gpt5_family("gpt-4-turbo") is False
    assert is_gpt5_family("o1-preview") is False
    assert is_gpt5_family("claude-3-sonnet") is False
    assert is_gpt5_family("deepseek-chat") is False


def test_is_unsupported_param_error():
    assert _is_unsupported_param_error("Unsupported parameter: temperature", "temperature") is True
    assert _is_unsupported_param_error("Model does not support max_tokens", "max_tokens") is True
    assert _is_unsupported_param_error("Unexpected keyword argument 'response_format'", "response_format") is True
    assert _is_unsupported_param_error("Temperature is only supported when reasoning is none", "temperature") is True
    assert _is_unsupported_param_error("Rate limit exceeded", "temperature") is False
    assert _is_unsupported_param_error("Connection timeout", "max_tokens") is False


def test_create_chat_completion_standard_model():
    client = MagicMock()
    fake_response = MagicMock()
    client.chat.completions.create.return_value = fake_response

    messages = [{"role": "user", "content": "hi"}]
    res = create_chat_completion(
        client,
        model="gpt-4o",
        messages=messages,
        temperature=0.7,
        max_tokens=2048,
        response_format={"type": "json_object"},
    )

    assert res == fake_response
    client.chat.completions.create.assert_called_once_with(
        model="gpt-4o",
        messages=messages,
        temperature=0.7,
        max_tokens=2048,
        response_format={"type": "json_object"},
    )


def test_create_chat_completion_gpt5_model():
    client = MagicMock()
    fake_response = MagicMock()
    client.chat.completions.create.return_value = fake_response

    messages = [{"role": "user", "content": "hi"}]
    res = create_chat_completion(
        client,
        model="gpt-5-turbo",
        messages=messages,
        temperature=0.7,
        max_tokens=4096,
        response_format={"type": "json_object"},
    )

    assert res == fake_response
    # GPT-5 should omit temperature and map max_tokens to max_completion_tokens
    client.chat.completions.create.assert_called_once_with(
        model="gpt-5-turbo",
        messages=messages,
        max_completion_tokens=4096,
        response_format={"type": "json_object"},
    )


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


def test_fallback_retry_temperature_unsupported():
    client = MagicMock()
    fake_response = MagicMock()
    # First call fails because temperature unsupported, second succeeds
    client.chat.completions.create.side_effect = [
        Exception("Error: temperature is not supported with this model"),
        fake_response,
    ]

    res = create_chat_completion(
        client,
        model="some-reasoning-model",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.5,
        max_tokens=1000,
    )

    assert res == fake_response
    assert client.chat.completions.create.call_count == 2
    # Second call should not have temperature
    second_call_kwargs = client.chat.completions.create.call_args_list[1].kwargs
    assert "temperature" not in second_call_kwargs
    assert second_call_kwargs["max_tokens"] == 1000


def test_fallback_retry_response_format_unsupported():
    client = MagicMock()
    fake_response = MagicMock()
    client.chat.completions.create.side_effect = [
        Exception("Unsupported parameter: response_format"),
        fake_response,
    ]

    res = create_chat_completion(
        client,
        model="legacy-model",
        messages=[{"role": "user", "content": "hi"}],
        response_format={"type": "json_object"},
    )

    assert res == fake_response
    assert client.chat.completions.create.call_count == 2
    second_call_kwargs = client.chat.completions.create.call_args_list[1].kwargs
    assert "response_format" not in second_call_kwargs


def test_fallback_retry_max_tokens_to_max_completion_tokens():
    client = MagicMock()
    fake_response = MagicMock()
    client.chat.completions.create.side_effect = [
        Exception("unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."),
        fake_response,
    ]

    res = create_chat_completion(
        client,
        model="custom-gpt-model",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=2048,
    )

    assert res == fake_response
    assert client.chat.completions.create.call_count == 2
    second_call_kwargs = client.chat.completions.create.call_args_list[1].kwargs
    assert "max_tokens" not in second_call_kwargs
    assert second_call_kwargs["max_completion_tokens"] == 2048


def test_fallback_retry_max_completion_tokens_to_max_tokens():
    client = MagicMock()
    fake_response = MagicMock()
    # GPT-5 model starts with max_completion_tokens; if provider does not support it:
    client.chat.completions.create.side_effect = [
        Exception("unsupported parameter: 'max_completion_tokens'"),
        fake_response,
    ]

    res = create_chat_completion(
        client,
        model="gpt-5-custom",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=2048,
    )

    assert res == fake_response
    assert client.chat.completions.create.call_count == 2
    second_call_kwargs = client.chat.completions.create.call_args_list[1].kwargs
    assert "max_completion_tokens" not in second_call_kwargs
    assert second_call_kwargs["max_tokens"] == 2048


def test_fallback_retry_non_param_error_raises():
    client = MagicMock()
    client.chat.completions.create.side_effect = ValueError("Authentication failed")

    with pytest.raises(ValueError, match="Authentication failed"):
        create_chat_completion(
            client,
            model="gpt-4o",
            messages=[{"role": "user", "content": "hi"}],
        )

    assert client.chat.completions.create.call_count == 1


def test_extract_chat_completion_text_string():
    class Message:
        content = "Hello, world!"

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    assert extract_chat_completion_text(Response()) == "Hello, world!"


def test_extract_chat_completion_text_empty():
    assert extract_chat_completion_text(None) == ""
    assert extract_chat_completion_text(MagicMock(choices=[])) == ""

    empty_choice = MagicMock()
    empty_choice.message = None
    assert extract_chat_completion_text(MagicMock(choices=[empty_choice])) == ""

    choice_none_content = MagicMock()
    choice_none_content.message.content = None
    assert extract_chat_completion_text(MagicMock(choices=[choice_none_content])) == ""


def test_extract_chat_completion_text_dict_chunks():
    class Message:
        content = [
            {"type": "text", "text": "Part 1 "},
            {"type": "text", "text": {"value": "Part 2 "}},
            {"type": "text", "content": "Part 3"},
        ]

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    assert extract_chat_completion_text(Response()) == "Part 1 Part 2 Part 3"


def test_extract_chat_completion_text_object_chunks():
    class Item1:
        text = "Alpha "

    class Item2:
        text = {"value": "Beta "}

    class Item3:
        content = "Gamma"

    class Message:
        content = [Item1(), Item2(), Item3()]

    class Choice:
        message = Message()

    class Response:
        choices = [Choice()]

    assert extract_chat_completion_text(Response()) == "Alpha Beta Gamma"


def test_llm_client_chat_with_think_tags_and_ollama():
    from app.utils.llm_client import LLMClient
    from unittest.mock import patch

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
    import json
    from unittest.mock import patch
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
    import json
    from unittest.mock import patch
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
