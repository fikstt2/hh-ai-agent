import asyncio
from types import SimpleNamespace

import httpx
import pytest
from mistralai.client.errors import NoResponseError

from llm.errors import (
    LLMAuthenticationError,
    LLMInvalidResponseError,
    LLMPermissionError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransientError,
)
from llm.providers.mistral import MistralProvider
from llm.types import LLMRequest


class FakeChat:
    def __init__(self, outcome: object):
        self.outcome = outcome
        self.calls: list[dict] = []

    async def complete_async(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class FakeClient:
    def __init__(self, outcome: object):
        self.chat = FakeChat(outcome)


class StatusError(Exception):
    def __init__(self, status_code: int, secret: str = "secret-body"):
        super().__init__(secret)
        self.status_code = status_code


def request(*, structured: bool = True) -> LLMRequest:
    return LLMRequest(
        system_instructions="System boundary",
        user_content="User JSON",
        model="mistral-small-latest",
        temperature=0.1,
        max_output_tokens=222,
        timeout_seconds=9,
        operation="vacancy_analysis",
        json_schema={"type": "object", "required": ["suitable"]} if structured else None,
    )


def response(content: object = '{"suitable": true}') -> SimpleNamespace:
    return SimpleNamespace(
        id="mistral-request-1",
        model="mistral-small-actual",
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content), finish_reason="stop"
            )
        ],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=6),
    )


def test_mistral_uses_official_async_chat_and_strict_json_schema() -> None:
    client = FakeClient(response())
    provider = MistralProvider("dedicated-mistral-key", client=client)

    result = asyncio.run(provider.complete(request()))

    assert client.chat.calls == [
        {
            "model": "mistral-small-latest",
            "messages": [
                {"role": "system", "content": "System boundary"},
                {"role": "user", "content": "User JSON"},
            ],
            "temperature": 0.1,
            "max_tokens": 222,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "vacancy_analysis",
                    "schema": {"type": "object", "required": ["suitable"]},
                    "strict": True,
                },
            },
            "timeout_ms": 9000,
        }
    ]
    assert result.request_id == "mistral-request-1"
    assert result.model == "mistral-small-actual"
    assert result.input_tokens == 11
    assert result.output_tokens == 6
    assert result.finish_reason == "stop"


def test_plain_text_request_omits_response_format() -> None:
    client = FakeClient(response("letter"))
    provider = MistralProvider("key", client=client)

    assert asyncio.run(provider.complete(request(structured=False))).text == "letter"
    assert "response_format" not in client.chat.calls[0]


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, LLMAuthenticationError),
        (403, LLMPermissionError),
        (429, LLMRateLimitError),
        (500, LLMTransientError),
    ],
)
def test_sdk_status_errors_are_normalized_without_body(
    status: int, expected: type[Exception]
) -> None:
    provider = MistralProvider("key", client=FakeClient(StatusError(status)))

    with pytest.raises(expected) as raised:
        asyncio.run(provider.complete(request()))

    assert "secret-body" not in str(raised.value)


def test_sdk_response_validation_error_with_success_status_is_invalid() -> None:
    provider = MistralProvider("key", client=FakeClient(StatusError(200)))

    with pytest.raises(LLMInvalidResponseError):
        asyncio.run(provider.complete(request()))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (httpx.ReadTimeout("timeout"), LLMTimeoutError),
        (httpx.ConnectError("offline"), LLMTransientError),
        (NoResponseError(), LLMTransientError),
    ],
)
def test_httpx_transport_errors_are_normalized(
    error: Exception, expected: type[Exception]
) -> None:
    provider = MistralProvider("key", client=FakeClient(error))

    with pytest.raises(expected):
        asyncio.run(provider.complete(request()))


def test_malformed_sdk_envelope_is_rejected() -> None:
    provider = MistralProvider("key", client=FakeClient(response(content=["not text"])))

    with pytest.raises(LLMInvalidResponseError):
        asyncio.run(provider.complete(request()))
