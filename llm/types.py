from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LLMRequest:
    system_instructions: str
    user_content: str
    model: str
    temperature: float
    max_output_tokens: int
    timeout_seconds: int
    operation: str
    json_schema: dict[str, object] | None = None


@dataclass(frozen=True)
class LLMResponse:
    text: str
    provider: str
    model: str
    request_id: str | None = None
    latency_ms: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    finish_reason: str | None = None
