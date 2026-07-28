from __future__ import annotations

import time
from typing import Any

import httpx
from mistralai.client import Mistral
from mistralai.client.errors import NoResponseError
from mistralai.client.utils.retries import BackoffStrategy, RetryConfig

from llm.errors import (
    LLMInvalidResponseError,
    LLMTimeoutError,
    LLMTransientError,
    error_for_http_status,
)
from llm.types import LLMRequest, LLMResponse


class MistralProvider:
    name = "mistral"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        *,
        client: Any = None,
    ):
        self.api_key = api_key
        self.base_url = base_url or None
        self._client = client
        self._sync_http_client: httpx.Client | None = None
        self._async_http_client: httpx.AsyncClient | None = None

    def _get_client(self) -> Any:
        if self._client is None:
            self._sync_http_client = httpx.Client(follow_redirects=True)
            self._async_http_client = httpx.AsyncClient(follow_redirects=True)
            self._client = Mistral(
                api_key=self.api_key,
                server_url=self.base_url,
                client=self._sync_http_client,
                async_client=self._async_http_client,
                retry_config=RetryConfig(
                    "none", BackoffStrategy(500, 5000, 2, 5000), False
                ),
            )
        return self._client

    async def complete(self, request: LLMRequest) -> LLMResponse:
        arguments: dict[str, object] = {
            "model": request.model,
            "messages": [
                {"role": "system", "content": request.system_instructions},
                {"role": "user", "content": request.user_content},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_output_tokens,
            "timeout_ms": request.timeout_seconds * 1000,
        }
        if request.json_schema is not None:
            arguments["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.operation,
                    "schema": request.json_schema,
                    "strict": True,
                },
            }
        started = time.perf_counter()
        try:
            data = await self._get_client().chat.complete_async(**arguments)
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError() from exc
        except httpx.TransportError as exc:
            raise LLMTransientError() from exc
        except NoResponseError as exc:
            raise LLMTransientError() from exc
        except Exception as exc:
            status = getattr(exc, "status_code", None)
            if isinstance(status, int) and status >= 400:
                headers = getattr(exc, "headers", {})
                raise error_for_http_status(
                    status, headers.get("Retry-After")
                ) from exc
            raise LLMInvalidResponseError() from exc
        try:
            choice = data.choices[0]
            text = choice.message.content
        except (AttributeError, IndexError, TypeError) as exc:
            raise LLMInvalidResponseError() from exc
        if not isinstance(text, str):
            raise LLMInvalidResponseError()
        usage = getattr(data, "usage", None)
        return LLMResponse(
            text=text,
            provider=self.name,
            model=data.model if isinstance(getattr(data, "model", None), str) else request.model,
            request_id=data.id if isinstance(getattr(data, "id", None), str) else None,
            latency_ms=round((time.perf_counter() - started) * 1000),
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            finish_reason=choice.finish_reason
            if isinstance(getattr(choice, "finish_reason", None), str)
            else None,
        )

    async def close(self) -> None:
        if self._async_http_client is not None:
            await self._async_http_client.aclose()
        if self._sync_http_client is not None:
            self._sync_http_client.close()
