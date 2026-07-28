from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from database import Database
from llm.base import ProviderAdapter
from llm.errors import (
    LLMDailyLimitError,
    LLMError,
    LLMInvalidResponseError,
    LLMRateLimitError,
)
from llm.types import LLMRequest, LLMResponse


logger = logging.getLogger(__name__)
StructuredResult = TypeVar("StructuredResult", bound=BaseModel)


class ManagedLLMProvider:
    def __init__(
        self,
        adapter: ProviderAdapter,
        database: Database,
        *,
        max_retries: int,
        max_requests_per_day: int,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now_factory: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ):
        self.adapter = adapter
        self.database = database
        self.max_retries = max_retries
        self.max_requests_per_day = max_requests_per_day
        self._sleep = sleep
        self._now = now_factory

    async def generate_text(self, request: LLMRequest) -> LLMResponse:
        response, _ = await self._generate(request, None)
        return response

    async def generate_structured(
        self,
        request: LLMRequest,
        response_model: type[StructuredResult],
    ) -> tuple[LLMResponse, StructuredResult]:
        response, parsed = await self._generate(request, response_model)
        assert parsed is not None
        return response, parsed

    async def _generate(
        self,
        request: LLMRequest,
        response_model: type[StructuredResult] | None,
    ) -> tuple[LLMResponse, StructuredResult | None]:
        invalid_responses = 0
        active_request = request
        for attempt in range(self.max_retries + 1):
            row_id = self.database.reserve_llm_request(
                provider=self.adapter.name,
                model=request.model,
                operation=request.operation,
                now=self._now(),
                daily_limit=self.max_requests_per_day,
            )
            if row_id is None:
                raise LLMDailyLimitError()
            try:
                response = await self.adapter.complete(active_request)
                if not response.text.strip():
                    raise LLMInvalidResponseError()
                parsed = None
                if response_model is not None:
                    try:
                        parsed = response_model.model_validate_json(response.text)
                    except ValidationError as exc:
                        raise LLMInvalidResponseError() from exc
                self.database.complete_llm_request(
                    row_id,
                    success=True,
                    finished_at=self._now(),
                    input_tokens=response.input_tokens,
                    output_tokens=response.output_tokens,
                    latency_ms=response.latency_ms,
                    provider_request_id=response.request_id,
                )
                logger.info(
                    "llm_request provider=%s model=%s operation=%s success=true "
                    "latency_ms=%s request_id=%s input_tokens=%s output_tokens=%s",
                    self.adapter.name,
                    response.model,
                    request.operation,
                    response.latency_ms,
                    response.request_id or "",
                    response.input_tokens,
                    response.output_tokens,
                )
                return response, parsed
            except LLMError as exc:
                if isinstance(exc, LLMInvalidResponseError):
                    invalid_responses += 1
                    if invalid_responses == 1:
                        active_request = replace(
                            request,
                            system_instructions=(
                                f"{request.system_instructions}\nThe previous response was "
                                "invalid. Return only content matching the requested format."
                            ),
                        )
                self.database.complete_llm_request(
                    row_id,
                    success=False,
                    finished_at=self._now(),
                    error_type=exc.category,
                )
                logger.warning(
                    "llm_request provider=%s model=%s operation=%s success=false error_type=%s",
                    self.adapter.name,
                    request.model,
                    request.operation,
                    exc.category,
                )
                if (
                    not exc.retryable
                    or attempt >= self.max_retries
                    or invalid_responses > 1
                ):
                    raise
                await self._sleep(self._retry_delay(exc, attempt))
        raise AssertionError("unreachable")

    @staticmethod
    def _retry_delay(error: LLMError, attempt: int) -> float:
        if (
            isinstance(error, LLMRateLimitError)
            and error.retry_after_seconds is not None
            and math.isfinite(error.retry_after_seconds)
            and error.retry_after_seconds >= 0
        ):
            return max(0.5, min(error.retry_after_seconds, 5.0))
        return min(0.5 * (2**attempt), 5.0)

    async def close(self) -> None:
        await self.adapter.close()
