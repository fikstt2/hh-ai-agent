from dataclasses import FrozenInstanceError

import pytest

from llm.errors import (
    LLMAuthenticationError,
    LLMConfigurationError,
    LLMDailyLimitError,
    LLMInvalidResponseError,
    LLMPermissionError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransientError,
    LLMUnsupportedCapabilityError,
)
from llm.types import LLMRequest, LLMResponse


def test_request_and_response_are_immutable_metadata_contracts() -> None:
    request = LLMRequest(
        system_instructions="System",
        user_content="User",
        model="model",
        temperature=0,
        max_output_tokens=100,
        timeout_seconds=30,
        operation="vacancy_analysis",
        json_schema={"type": "object"},
    )
    response = LLMResponse(
        text='{"ok": true}',
        provider="fake",
        model="model",
        request_id="req-1",
        latency_ms=12,
        input_tokens=3,
        output_tokens=4,
        finish_reason="stop",
    )

    assert request.operation == "vacancy_analysis"
    assert response.input_tokens == 3
    with pytest.raises(FrozenInstanceError):
        request.model = "other"


@pytest.mark.parametrize(
    ("error", "category", "retryable"),
    [
        (LLMAuthenticationError(), "authentication", False),
        (LLMPermissionError(), "permission", False),
        (LLMRateLimitError(), "rate_limit", True),
        (LLMTimeoutError(), "timeout", True),
        (LLMTransientError(), "transient_server", True),
        (LLMInvalidResponseError(), "invalid_response", True),
        (LLMUnsupportedCapabilityError(), "unsupported_capability", False),
        (LLMConfigurationError(), "configuration", False),
        (LLMDailyLimitError(), "daily_limit", False),
    ],
)
def test_normalized_errors_expose_only_category_and_retry_policy(
    error: Exception, category: str, retryable: bool
) -> None:
    assert error.category == category
    assert error.retryable is retryable


def test_rate_limit_can_carry_a_bounded_retry_hint() -> None:
    error = LLMRateLimitError(retry_after_seconds=3.5)

    assert error.retry_after_seconds == 3.5
    assert "3.5" not in str(error)
