from __future__ import annotations

import asyncio
from collections.abc import Iterable

from llm.types import LLMRequest, LLMResponse


class FakeProvider:
    name = "fake"

    def __init__(
        self,
        outcomes: Iterable[LLMResponse | Exception],
        *,
        delay_seconds: float = 0,
    ):
        self._outcomes = iter(outcomes)
        self.delay_seconds = delay_seconds
        self.requests: list[LLMRequest] = []
        self.closed = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self) -> None:
        self.closed = True
