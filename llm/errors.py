from __future__ import annotations


class LLMError(RuntimeError):
    category = "llm_error"
    retryable = False

    def __init__(self, message: str | None = None):
        super().__init__(message or self.category.replace("_", " "))


class LLMAuthenticationError(LLMError):
    category = "authentication"


class LLMPermissionError(LLMError):
    category = "permission"


class LLMRateLimitError(LLMError):
    category = "rate_limit"
    retryable = True

    def __init__(self, retry_after_seconds: float | None = None):
        super().__init__()
        self.retry_after_seconds = retry_after_seconds


class LLMTimeoutError(LLMError):
    category = "timeout"
    retryable = True


class LLMTransientError(LLMError):
    category = "transient_server"
    retryable = True


class LLMInvalidResponseError(LLMError):
    category = "invalid_response"
    retryable = True


class LLMUnsupportedCapabilityError(LLMError):
    category = "unsupported_capability"


class LLMConfigurationError(LLMError):
    category = "configuration"


class LLMDailyLimitError(LLMError):
    category = "daily_limit"


def error_for_http_status(
    status: int, retry_after_header: object = None
) -> LLMError:
    if status == 401:
        return LLMAuthenticationError()
    if status == 403:
        return LLMPermissionError()
    if status == 429:
        try:
            retry_after = float(retry_after_header)
        except (TypeError, ValueError):
            retry_after = None
        return LLMRateLimitError(retry_after)
    if status >= 500:
        return LLMTransientError()
    return LLMConfigurationError()
