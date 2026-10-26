"""
OpenAI Chat Completions compatibility helpers.

This module keeps existing behavior for legacy models/providers while
gracefully adapting request parameters for GPT-5 family models.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


UNSUPPORTED_PARAM_HINTS = (
    "unsupported",
    "not supported",
    "does not support",
    "unknown parameter",
    "unexpected keyword",
    "extra fields",
    "only supported",
)


def is_gpt5_family(model: Optional[str]) -> bool:
    """Return True when model belongs to GPT-5 family aliases/snapshots."""
    if not model:
        return False
    return model.strip().lower().startswith("gpt-5")


def _is_unsupported_param_error(message: str, param_name: str) -> bool:
    msg = (message or "").lower()
    if param_name.lower() not in msg:
        return False
    return any(hint in msg for hint in UNSUPPORTED_PARAM_HINTS)


def _extract_error_message(error: Exception) -> str:
    # openai.BadRequestError string usually includes the API message; keep it generic
    return str(error)


def create_chat_completion(
    client: Any,
    *,
    model: str,
    messages: List[Dict[str, Any]],
    temperature: Optional[float] = None,
    max_tokens: Optional[int] = None,
    response_format: Optional[Dict[str, Any]] = None,
    extra_params: Optional[Dict[str, Any]] = None,
    max_attempts: int = 4,
) -> Any:
    """
    Create a chat completion with adaptive parameter fallback.

    Compatibility strategy:
    - For GPT-5 family, avoid sending temperature by default.
    - For token limit, try `max_completion_tokens` on GPT-5, `max_tokens` otherwise.
    - On parameter-support errors, adapt and retry without changing caller behavior.
    """
    kwargs: Dict[str, Any] = {
        "model": model,
        "messages": messages,
    }

    if response_format is not None:
        kwargs["response_format"] = response_format

    # GPT-5 family rejects temperature unless reasoning effort is explicitly `none`.
    if temperature is not None and not is_gpt5_family(model):
        kwargs["temperature"] = temperature

    if max_tokens is not None:
        if is_gpt5_family(model):
            kwargs["max_completion_tokens"] = max_tokens
        else:
            kwargs["max_tokens"] = max_tokens

    if extra_params:
        kwargs.update(extra_params)

    attempted_signatures = set()
    unsupported_params = set()
    last_error: Optional[Exception] = None

    for _ in range(max_attempts):
        signature = tuple(sorted(kwargs.keys()))
        if signature in attempted_signatures:
            break
        attempted_signatures.add(signature)

        try:
            return client.chat.completions.create(**kwargs)
        except Exception as error:
            last_error = error
            error_msg = _extract_error_message(error)
            changed = False

            if _is_unsupported_param_error(error_msg, "temperature") and "temperature" in kwargs:
                kwargs.pop("temperature", None)
                unsupported_params.add("temperature")
                changed = True

            if _is_unsupported_param_error(error_msg, "response_format") and "response_format" in kwargs:
                kwargs.pop("response_format", None)
                unsupported_params.add("response_format")
                changed = True

            if _is_unsupported_param_error(error_msg, "max_tokens") and "max_tokens" in kwargs:
                token_value = kwargs.pop("max_tokens")
                unsupported_params.add("max_tokens")
                if "max_completion_tokens" not in unsupported_params:
                    kwargs["max_completion_tokens"] = token_value
                changed = True

            elif (
                _is_unsupported_param_error(error_msg, "max_completion_tokens")
                and "max_completion_tokens" in kwargs
            ):
                token_value = kwargs.pop("max_completion_tokens")
                unsupported_params.add("max_completion_tokens")
                if "max_tokens" not in unsupported_params:
                    kwargs["max_tokens"] = token_value
                changed = True

            if not changed:
                raise

    if last_error:
        raise last_error
    raise RuntimeError("Chat completion failed with unknown error.")


def extract_chat_completion_text(response: Any) -> str:
    """Extract plain text from chat completion response across SDK content shapes."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return ""

    message = getattr(choices[0], "message", None)
    if message is None:
        return ""

    content = getattr(message, "content", "")

    if isinstance(content, str):
        return content

    if isinstance(content, list):
        chunks: List[str] = []
        for item in content:
            if isinstance(item, dict):
                text_obj = item.get("text")
                if isinstance(text_obj, dict):
                    text_obj = text_obj.get("value")
                if isinstance(text_obj, str):
                    chunks.append(text_obj)
                elif isinstance(item.get("content"), str):
                    chunks.append(item["content"])
                continue

            text_obj = getattr(item, "text", None)
            if isinstance(text_obj, dict):
                text_obj = text_obj.get("value")
            if isinstance(text_obj, str):
                chunks.append(text_obj)
                continue

            content_obj = getattr(item, "content", None)
            if isinstance(content_obj, str):
                chunks.append(content_obj)

        return "".join(chunks).strip()

    return str(content or "")
