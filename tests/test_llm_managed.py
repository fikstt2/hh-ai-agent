import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, Field

from database import Database
from llm.errors import (
    LLMAuthenticationError,
    LLMBlacklistError,
    LLMDailyLimitError,
    LLMInvalidResponseError,
    LLMRateLimitError,
    LLMTimeoutError,
)
from llm.managed import ManagedLLMProvider
from llm.providers.fake import FakeProvider
from llm.types import LLMRequest, LLMResponse


NOW = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)


class StructuredAnswer(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    suitable: bool
    confidence: float = Field(ge=0, le=1)


def request(*, structured: bool = False) -> LLMRequest:
    return LLMRequest(
        system_instructions="Never log secret-prompt-sentinel",
        user_content="secret-user-content-sentinel",
        model="test-model",
        temperature=0,
        max_output_tokens=100,
        timeout_seconds=30,
        operation="vacancy_analysis",
        json_schema=StructuredAnswer.model_json_schema() if structured else None,
    )


def response(text: str = "ok") -> LLMResponse:
    return LLMResponse(
        text=text,
        provider="fake",
        model="test-model",
        request_id="request-metadata-1",
        latency_ms=12,
        input_tokens=3,
        output_tokens=4,
    )


def managed(
    tmp_path: Path,
    outcomes: list[LLMResponse | Exception],
    *,
    max_retries: int = 1,
    max_rate_limit_retries: int | None = None,
    daily_limit: int = 10,
    sleeps: list[float] | None = None,
) -> tuple[ManagedLLMProvider, FakeProvider, Database]:
    database = Database(tmp_path / "agent.db")
    database.init()
    adapter = FakeProvider(outcomes)

    async def record_sleep(seconds: float) -> None:
        if sleeps is not None:
            sleeps.append(seconds)

    provider = ManagedLLMProvider(
        adapter,
        database,
        max_retries=max_retries,
        max_rate_limit_retries=max_rate_limit_retries,
        max_requests_per_day=daily_limit,
        sleep=record_sleep,
        now_factory=lambda: NOW,
    )
    return provider, adapter, database


@pytest.mark.parametrize("error", [LLMTimeoutError(), LLMRateLimitError()])
def test_retryable_errors_retry_once_with_bounded_delay(
    tmp_path: Path, error: Exception
) -> None:
    sleeps: list[float] = []
    provider, adapter, database = managed(
        tmp_path, [error, response()], sleeps=sleeps
    )

    result = asyncio.run(provider.generate_text(request()))

    assert result.text == "ok"
    assert len(adapter.requests) == 2
    assert sleeps == [0.5]
    assert database.llm_requests_today(NOW) == 2


def test_default_rate_limit_uses_full_retry_budget(tmp_path: Path) -> None:
    provider, adapter, _ = managed(
        tmp_path,
        [LLMRateLimitError(), LLMRateLimitError(), response("ok")],
        max_retries=5,
    )

    assert asyncio.run(provider.generate_text(request())).text == "ok"
    assert len(adapter.requests) == 3


def test_opt_in_rate_limit_cap_never_retries_more_than_once(tmp_path: Path) -> None:
    provider, adapter, _ = managed(
        tmp_path,
        [LLMRateLimitError(), LLMRateLimitError(), response("must not run")],
        max_retries=5,
        max_rate_limit_retries=1,
    )

    with pytest.raises(LLMRateLimitError):
        asyncio.run(provider.generate_text(request()))

    assert len(adapter.requests) == 2


def test_zero_retries_disables_rate_limit_retry(tmp_path: Path) -> None:
    provider, adapter, _ = managed(
        tmp_path, [LLMRateLimitError(), response("must not run")], max_retries=0
    )

    with pytest.raises(LLMRateLimitError):
        asyncio.run(provider.generate_text(request()))

    assert len(adapter.requests) == 1


@pytest.mark.parametrize("structured", [True, False])
def test_length_limited_response_is_rejected_even_when_parseable(
    tmp_path: Path, structured: bool
) -> None:
    truncated = replace(
        response('{"suitable":true,"confidence":0.9}'), finish_reason="length"
    )
    provider, adapter, _ = managed(tmp_path, [truncated], max_retries=0)

    with pytest.raises(LLMInvalidResponseError):
        if structured:
            asyncio.run(provider.generate_structured(request(structured=True), StructuredAnswer))
        else:
            asyncio.run(provider.generate_text(request()))
    assert len(adapter.requests) == 1


def test_authentication_error_is_not_retried(tmp_path: Path) -> None:
    provider, adapter, database = managed(
        tmp_path, [LLMAuthenticationError(), response()]
    )

    with pytest.raises(LLMAuthenticationError):
        asyncio.run(provider.generate_text(request()))

    assert len(adapter.requests) == 1
    assert database.llm_requests_today(NOW) == 1


@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [(100.0, 5.0), (float("nan"), 0.5), (-1.0, 0.5)],
)
def test_retry_after_hint_is_finite_and_bounded(
    tmp_path: Path, retry_after: float, expected_delay: float
) -> None:
    sleeps: list[float] = []
    provider, _, _ = managed(
        tmp_path,
        [LLMRateLimitError(retry_after), response()],
        sleeps=sleeps,
    )

    asyncio.run(provider.generate_text(request()))

    assert sleeps == [expected_delay]


def test_sqlite_daily_limit_blocks_before_adapter_call(tmp_path: Path) -> None:
    provider, adapter, database = managed(
        tmp_path, [response(), response()], daily_limit=1
    )

    assert asyncio.run(provider.generate_text(request())).text == "ok"
    with pytest.raises(LLMDailyLimitError):
        asyncio.run(provider.generate_text(request()))

    assert len(adapter.requests) == 1
    assert database.llm_requests_today(NOW) == 1


@pytest.mark.parametrize(
    "invalid",
    [
        "NOT YES",
        '{"suitable": "true", "confidence": 0.8}',
        '{"suitable": true, "confidence": 2}',
        '{"suitable": true, "confidence": 0.8, "extra": 1}',
    ],
)
def test_structured_response_is_validated_strictly_and_retried_once(
    tmp_path: Path, invalid: str
) -> None:
    provider, adapter, _ = managed(
        tmp_path,
        [response(invalid), response('{"suitable": false, "confidence": 0.2}')],
    )

    raw, parsed = asyncio.run(
        provider.generate_structured(request(structured=True), StructuredAnswer)
    )

    assert raw.text.startswith("{")
    assert parsed == StructuredAnswer(suitable=False, confidence=0.2)
    assert len(adapter.requests) == 2
    assert "previous response was invalid" in adapter.requests[1].system_instructions.lower()
    assert adapter.requests[1].json_schema == adapter.requests[0].json_schema


def test_invalid_structured_response_never_uses_more_than_one_correction(
    tmp_path: Path,
) -> None:
    provider, adapter, _ = managed(
        tmp_path,
        [response("bad"), response("still bad"), response('{"suitable": true, "confidence": 1}')],
        max_retries=5,
    )

    with pytest.raises(LLMInvalidResponseError):
        asyncio.run(
            provider.generate_structured(request(structured=True), StructuredAnswer)
        )

    assert len(adapter.requests) == 2


def test_adapter_invalid_response_never_uses_more_than_one_correction(
    tmp_path: Path,
) -> None:
    provider, adapter, _ = managed(
        tmp_path,
        [
            LLMInvalidResponseError(),
            LLMInvalidResponseError(),
            response("must not be reached"),
        ],
        max_retries=5,
    )

    with pytest.raises(LLMInvalidResponseError):
        asyncio.run(provider.generate_text(request()))

    assert len(adapter.requests) == 2


def test_blank_text_is_invalid_and_gets_one_correction(tmp_path: Path) -> None:
    provider, adapter, database = managed(
        tmp_path,
        [response("   "), response("corrected")],
        max_retries=1,
    )

    result = asyncio.run(provider.generate_text(request()))

    assert result.text == "corrected"
    assert len(adapter.requests) == 2
    assert database.llm_usage_stats()["failed_requests"] == 1
    assert database.llm_usage_stats()["successful_requests"] == 1


def test_logs_and_database_contain_metadata_but_not_content_or_secrets(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    provider, _, database = managed(tmp_path, [response("secret-response-sentinel")])

    with caplog.at_level("INFO"):
        result = asyncio.run(provider.generate_text(request()))
    assert result.text == "secret-response-sentinel"

    with sqlite3.connect(database.path) as connection:
        stored = repr(connection.execute("SELECT * FROM llm_requests").fetchall())
    logs = caplog.text

    assert "request-metadata-1" in stored
    assert "provider=fake" in logs
    assert "model=test-model" in logs
    assert "latency_ms=12" in logs
    assert "input_tokens=3" in logs
    assert "output_tokens=4" in logs
    for secret in (
        "secret-prompt-sentinel",
        "secret-user-content-sentinel",
        "secret-response-sentinel",
    ):
        assert secret not in stored
        assert secret not in logs


def test_close_delegates_to_the_single_selected_adapter(tmp_path: Path) -> None:
    provider, adapter, _ = managed(tmp_path, [])

    asyncio.run(provider.close())

    assert adapter.closed is True


# ---------------------------------------------------------------------------
# LLM blacklist: GigaChat sensitive-topic refusal is NOT retryable
# ---------------------------------------------------------------------------

def test_blacklist_error_not_retried(tmp_path: Path) -> None:
    """A policy refusal must not consume the retry budget."""
    refusal = response(
        text=(
            "Извините, я не могу обсуждать эту тему. "
            "Разговоры на некоторые темы временно ограничены."
        )
    )
    provider, adapter, _ = managed(
        tmp_path, [refusal, response()], max_retries=3
    )

    with pytest.raises(LLMBlacklistError):
        asyncio.run(provider.generate_text(request()))

    # Only one adapter call — no retry.
    assert len(adapter.requests) == 1


def test_blacklist_finish_reason_not_retried(tmp_path: Path) -> None:
    """finish_reason='blacklist' is treated the same as marker text."""
    refusal = replace(response(), finish_reason="blacklist")
    provider, adapter, _ = managed(
        tmp_path, [refusal, response()], max_retries=3
    )

    with pytest.raises(LLMBlacklistError):
        asyncio.run(provider.generate_text(request()))

    assert len(adapter.requests) == 1


def test_blacklist_category_is_distinct() -> None:
    """category='blacklist' and retryable=False are part of the public contract."""
    assert LLMBlacklistError.category == "blacklist"
    assert LLMBlacklistError.retryable is False


# ---------------------------------------------------------------------------
# JSON pre-fix: safe for valid JSON, helps GigaChat glitches
# ---------------------------------------------------------------------------

def test_valid_json_untouched_by_prefix(tmp_path: Path) -> None:
    """Valid JSON from Mistral/OpenAI/Ollama must parse on the first try."""
    # No GigaChat-style glitch, plain valid JSON.
    valid = '{"suitable": true, "confidence": 0.85}'
    provider, adapter, _ = managed(
        tmp_path, [response(text=valid)], max_retries=1
    )

    _, parsed = asyncio.run(
        provider.generate_structured(request(structured=True), StructuredAnswer)
    )

    assert parsed.suitable is True
    assert parsed.confidence == 0.85
    # Exactly one call — strict parse succeeded, no fallback attempts.
    assert len(adapter.requests) == 1


def test_gigachat_bracket_glitch_recovered(tmp_path: Path) -> None:
    """The `"),` glitch is fixed by the pre-fix regex, without a retry."""
    glitched = '{"suitable": true, "confidence": 0.9")}'
    provider, adapter, _ = managed(
        tmp_path, [response(text=glitched)], max_retries=0
    )

    _, parsed = asyncio.run(
        provider.generate_structured(request(structured=True), StructuredAnswer)
    )

    assert parsed.suitable is True
    assert parsed.confidence == 0.9
