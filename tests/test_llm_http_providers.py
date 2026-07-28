import asyncio
from typing import Any

import pytest

from llm.errors import (
    LLMAuthenticationError,
    LLMInvalidResponseError,
    LLMPermissionError,
    LLMRateLimitError,
    LLMTransientError,
)
from llm.providers.ollama import OllamaProvider
from llm.providers.openai_compatible import OpenAICompatibleProvider
from llm.types import LLMRequest


class FakeResponse:
    def __init__(self, *, status: int = 200, data: Any = None, headers: dict | None = None):
        self.status = status
        self.data = data
        self.headers = headers or {}

    async def json(self) -> Any:
        if isinstance(self.data, Exception):
            raise self.data
        return self.data


class ResponseContext:
    def __init__(self, response: FakeResponse):
        self.response = response

    async def __aenter__(self) -> FakeResponse:
        return self.response

    async def __aexit__(self, *_: object) -> None:
        return None


class FakeSession:
    def __init__(self, responses: list[FakeResponse]):
        self.responses = iter(responses)
        self.calls: list[tuple[str, dict]] = []
        self.closed = False

    def post(self, url: str, **kwargs: object) -> ResponseContext:
        self.calls.append((url, kwargs))
        return ResponseContext(next(self.responses))

    async def close(self) -> None:
        self.closed = True


def request(*, structured: bool = False) -> LLMRequest:
    return LLMRequest(
        system_instructions="System boundary",
        user_content="User JSON",
        model="chosen-model",
        temperature=0.2,
        max_output_tokens=321,
        timeout_seconds=7,
        operation="vacancy_analysis",
        json_schema={"type": "object", "required": ["suitable"]} if structured else None,
    )


def test_ollama_uses_separate_system_prompt_and_schema_format() -> None:
    session = FakeSession(
        [
            FakeResponse(
                data={
                    "response": '{"suitable": true}',
                    "model": "chosen-model",
                    "prompt_eval_count": 10,
                    "eval_count": 4,
                    "done_reason": "stop",
                }
            )
        ]
    )
    provider = OllamaProvider("http://localhost:11434/api/generate", session=session)

    result = asyncio.run(provider.complete(request(structured=True)))

    url, call = session.calls[0]
    assert url == "http://localhost:11434/api/generate"
    assert call["json"] == {
        "model": "chosen-model",
        "system": "System boundary",
        "prompt": "User JSON",
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": 321},
        "format": {"type": "object", "required": ["suitable"]},
    }
    assert call["timeout"].total == 7
    assert result.input_tokens == 10
    assert result.output_tokens == 4
    assert result.finish_reason == "stop"


def test_ollama_plain_text_omits_format() -> None:
    session = FakeSession([FakeResponse(data={"response": "letter"})])
    provider = OllamaProvider("http://localhost:11434/api/generate", session=session)

    assert asyncio.run(provider.complete(request())).text == "letter"
    assert "format" not in session.calls[0][1]["json"]


def test_openai_compatible_posts_only_to_chat_completions() -> None:
    session = FakeSession(
        [
            FakeResponse(
                data={
                    "id": "chat-1",
                    "model": "actual-model",
                    "choices": [
                        {"message": {"content": '{"suitable": true}'}, "finish_reason": "stop"}
                    ],
                    "usage": {"prompt_tokens": 8, "completion_tokens": 3},
                }
            )
        ]
    )
    provider = OpenAICompatibleProvider(
        "https://provider.example/v1/", "dedicated-key", json_mode=True, session=session
    )

    result = asyncio.run(provider.complete(request(structured=True)))

    url, call = session.calls[0]
    assert url == "https://provider.example/v1/chat/completions"
    assert call["headers"] == {
        "Authorization": "Bearer dedicated-key",
        "Content-Type": "application/json",
    }
    assert call["json"] == {
        "model": "chosen-model",
        "messages": [
            {"role": "system", "content": "System boundary"},
            {"role": "user", "content": "User JSON"},
        ],
        "temperature": 0.2,
        "max_tokens": 321,
        "response_format": {"type": "json_object"},
    }
    assert result.request_id == "chat-1"
    assert result.model == "actual-model"
    assert result.input_tokens == 8
    assert result.output_tokens == 3


def test_openai_compatible_can_disable_best_effort_json_mode() -> None:
    session = FakeSession(
        [FakeResponse(data={"choices": [{"message": {"content": "{}"}}]})]
    )
    provider = OpenAICompatibleProvider(
        "http://localhost:8000/v1", "key", json_mode=False, session=session
    )

    asyncio.run(provider.complete(request(structured=True)))

    assert "response_format" not in session.calls[0][1]["json"]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, LLMAuthenticationError),
        (403, LLMPermissionError),
        (429, LLMRateLimitError),
        (500, LLMTransientError),
    ],
)
@pytest.mark.parametrize("provider_kind", ["ollama", "compatible"])
def test_http_statuses_are_normalized_without_response_content_or_key(
    status: int, expected: type[Exception], provider_kind: str
) -> None:
    secret = "secret-response-or-key"
    session = FakeSession([FakeResponse(status=status, data={"error": secret})])
    provider = (
        OllamaProvider("http://localhost:11434/api/generate", session=session)
        if provider_kind == "ollama"
        else OpenAICompatibleProvider(
            "https://provider.example/v1", secret, json_mode=True, session=session
        )
    )

    with pytest.raises(expected) as raised:
        asyncio.run(provider.complete(request()))

    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    "provider",
    [
        lambda session: OllamaProvider("http://localhost:11434/api/generate", session=session),
        lambda session: OpenAICompatibleProvider(
            "https://provider.example/v1", "key", json_mode=True, session=session
        ),
    ],
)
def test_malformed_envelopes_are_rejected(provider: object) -> None:
    session = FakeSession([FakeResponse(data={"unexpected": "secret-body"})])

    with pytest.raises(LLMInvalidResponseError) as raised:
        asyncio.run(provider(session).complete(request()))

    assert "secret-body" not in str(raised.value)


def test_injected_sessions_are_not_closed() -> None:
    session = FakeSession([])
    provider = OllamaProvider("http://localhost:11434/api/generate", session=session)

    asyncio.run(provider.close())

    assert session.closed is False
