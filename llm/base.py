from __future__ import annotations

from typing import Protocol, TypeVar

from pydantic import BaseModel

from llm.types import LLMRequest, LLMResponse


StructuredResult = TypeVar("StructuredResult", bound=BaseModel)


class ProviderAdapter(Protocol):
    name: str

    async def complete(self, request: LLMRequest) -> LLMResponse: ...

    async def close(self) -> None: ...


class LLMProvider(Protocol):
    async def generate_text(self, request: LLMRequest) -> LLMResponse: ...

    async def generate_structured(
        self,
        request: LLMRequest,
        response_model: type[StructuredResult],
    ) -> tuple[LLMResponse, StructuredResult]: ...

    async def close(self) -> None: ...
