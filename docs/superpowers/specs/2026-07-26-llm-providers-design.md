# Extensible LLM Providers Design

## Goal

Replace the domain layer's direct Ollama HTTP dependency with a small,
provider-neutral contract. Keep Ollama working, add Mistral as the first cloud
provider, support explicit custom OpenAI-compatible chat endpoints, and keep all
existing browser and application-approval safety boundaries unchanged.

## Constraints

- The process selects exactly one provider through `LLM_PROVIDER`; no automatic
  fallback or cross-provider retry is allowed.
- The default remains local `ollama`, preserving the existing private workflow.
- Mistral uses the official `mistralai==2.7.0` Python SDK and its native JSON
  Schema response format.
- OpenAI-compatible support is limited to `/chat/completions`; it does not claim
  Responses API, Anthropic Messages, or complete OpenAI SDK compatibility.
- No provider receives Telegram credentials, browser state, cookies, local
  paths, database contents, logs, or the complete environment.
- Tests never call a real LLM API, HH.ru, or Telegram.
- `approval.py`, browser backends, Telegram approval, and physical submission
  are unchanged except for passing an injected analyzer through `main.py`.

For the pinned `mistralai==2.7.0` wheel, the verified import is
`from mistralai.client import Mistral`; the wheel exposes `mistralai` itself as
a namespace package. On macOS ARM64, `chat.complete_async`, `timeout_ms`, strict
JSON Schema response format, and explicit retry configuration were inspected
under Python 3.14.5. The complete dependency set and full test suite also passed in a
clean Homebrew Python 3.13.14 environment.

## Approaches considered

### OpenAI-compatible client only

This is the smallest transport surface, but it would force Ollama and Mistral
through one wire convention. It would not use the official Mistral SDK's native
JSON Schema contract, typed errors, lifecycle, or request metadata, and it would
make provider-specific error normalization implicit. Rejected.

### Universal gateway library

A library such as LiteLLM would provide many integrations, routing, and
fallback. Those capabilities are outside this project's needs and introduce a
larger dependency surface plus privacy and cost risk from accidental routing.
Rejected; there is no automatic fallback requirement to justify it.

### Small project-owned contract and adapters

Selected. The project uses only text generation and schema-constrained JSON, so
a narrow contract is sufficient. Provider-specific SDK and HTTP behavior stays
inside adapters, while retry, quota, usage, and domain validation are shared.

## Package layout

```text
llm/
├── __init__.py
├── base.py
├── errors.py
├── factory.py
├── managed.py
├── types.py
└── providers/
    ├── __init__.py
    ├── fake.py
    ├── mistral.py
    ├── ollama.py
    └── openai_compatible.py
```

`ProviderAdapter` is the internal transport contract. `LLMProvider` is the
domain-facing contract implemented by `ManagedLLMProvider`. This split prevents
three adapters from duplicating retry, quota, usage, and Pydantic validation.

## Common types

`LLMRequest` is an immutable dataclass containing only:

- `system_instructions: str`;
- `user_content: str`;
- `model: str`;
- `temperature: float`;
- `max_output_tokens: int`;
- `timeout_seconds: int`;
- `operation: str`;
- `json_schema: dict[str, object] | None`.

`LLMResponse` contains:

- `text: str`;
- `provider: str`;
- `model: str`;
- `request_id: str | None`;
- `latency_ms: int`;
- `input_tokens: int | None`;
- `output_tokens: int | None`;
- `finish_reason: str | None`.

The domain interface is generic for structured results:

```python
class LLMProvider(Protocol):
    async def generate_text(self, request: LLMRequest) -> LLMResponse: ...
    async def generate_structured(
        self, request: LLMRequest, schema: type[T]
    ) -> tuple[LLMResponse, T]: ...
    async def close(self) -> None: ...
```

The provider receives a Pydantic type only in the managed layer. Transport
adapters receive its generated JSON Schema and return text; local Pydantic
validation remains authoritative even when a remote API promises strict JSON.

## Error model and retry policy

The normalized error classes are:

- `LLMAuthenticationError`;
- `LLMPermissionError`;
- `LLMRateLimitError`;
- `LLMTimeoutError`;
- `LLMTransientError`;
- `LLMInvalidResponseError`;
- `LLMUnsupportedCapabilityError`;
- `LLMConfigurationError`;
- `LLMDailyLimitError`.

Adapters translate SDK or HTTP errors without including response bodies, API
keys, prompts, or full responses in exception messages. The managed provider
uses at most `LLM_MAX_RETRIES + 1` total attempts:

- timeout and normalized transient 5xx failures are retryable;
- 429 is retryable with bounded exponential backoff and an optional clamped
  `Retry-After` value;
- 401, 403, configuration, unsupported capability, and daily limit are not
  retried;
- empty text, invalid JSON, and Pydantic schema failures receive no more than
  one correction retry and still respect the global retry limit.

Backoff starts at 0.5 seconds, doubles, and is capped at 5 seconds. Sleep is
injectable in tests. The selected adapter never changes during retries.

## SQLite quota and usage

`Database.init()` creates an additive `llm_requests` table:

```text
id, provider, model, operation, started_at, finished_at,
success, input_tokens, output_tokens, latency_ms,
provider_request_id, error_type
```

It stores no prompt, response text, API key, profile, vacancy content, or local
path. `reserve_llm_request()` uses `BEGIN IMMEDIATE`, counts rows for the local
calendar day, and inserts an in-progress row in the same transaction. Every
actual network attempt consumes one reservation, including retries and crashed
in-progress attempts. This is conservative and prevents concurrency or restart
from bypassing `LLM_MAX_REQUESTS_PER_DAY`.

`complete_llm_request()` records success/failure and optional usage metadata.
Missing token usage never blocks processing. Structured logs contain only the
same metadata fields.

## Configuration

Common variables:

```ini
LLM_PROVIDER=ollama
LLM_MODEL=llama3
LLM_TIMEOUT_SECONDS=30
LLM_MAX_RETRIES=1
LLM_TEMPERATURE=0
LLM_MAX_OUTPUT_TOKENS=1200
LLM_MAX_REQUESTS_PER_DAY=100
```

Provider variables:

```ini
OLLAMA_URL=http://localhost:11434/api/generate
MISTRAL_API_KEY=
MISTRAL_BASE_URL=
OPENAI_COMPATIBLE_BASE_URL=
OPENAI_COMPATIBLE_API_KEY=
OPENAI_COMPATIBLE_JSON_MODE=true
```

The common `LLM_MODEL` is selected because the process has exactly one active
provider. The previous `OLLAMA_MODEL` remains a deprecated fallback only for
`LLM_PROVIDER=ollama`, preserving existing local `.env` files without allowing
a cloud provider to inherit an Ollama model name.

Validation rules:

- provider is one of `ollama`, `mistral`, `openai_compatible`;
- model is non-empty;
- timeout, output-token limit, and daily request limit are positive integers;
- retries are a non-negative integer;
- temperature is a finite number in `[0, 2]`;
- Mistral requires its own API key; its optional custom URL must be HTTPS;
- Ollama requires an HTTP(S) URL; plain HTTP is allowed only for `localhost`,
  `127.0.0.1`, or `::1`;
- custom OpenAI-compatible endpoints require their own API key and base URL;
  HTTP is allowed only for the same loopback hosts, while every remote endpoint
  requires HTTPS;
- validation errors name the missing variable but never include its value.

No key is reused across providers.

## Provider adapters

### Ollama

`OllamaProvider` owns one reusable `aiohttp.ClientSession`. It calls the
configured `/api/generate` endpoint with separate `system` and `prompt` fields,
temperature and output-token options, and the requested JSON Schema as `format`
for structured operations. It maps `prompt_eval_count`, `eval_count`, and
`done_reason` when available.

### Mistral

`MistralProvider` uses `mistralai==2.7.0` and owned sync/async `httpx` clients
passed to `Mistral`. The provider closes both clients explicitly. The SDK's
own retry strategy is disabled so the project-wide bound remains authoritative.

Text requests omit `response_format` and use the SDK's text default. Structured
requests use:

```python
{
    "type": "json_schema",
    "json_schema": {
        "name": request.operation,
        "schema": request.json_schema,
        "strict": True,
    },
}
```

The adapter maps response ID, model, prompt/completion tokens, and finish
reason. It supplies no tools.

### OpenAI-compatible

`OpenAICompatibleProvider` owns one `aiohttp.ClientSession` and posts only to
`{base_url}/chat/completions` with its dedicated bearer key. It uses system and
user messages, temperature, `max_tokens`, and optional
`response_format={"type": "json_object"}`. When JSON mode is disabled, schema
validation still happens locally. Compatibility beyond this endpoint is not
claimed.

### Fake

`FakeProvider` is a deterministic adapter with a queue of responses or
normalized errors plus optional delay. Tests can model text, JSON, timeout,
rate limit, authentication, malformed JSON, schema mismatch, and latency
without a network call.

## Domain analyzer and prompt injection boundary

`ai_analyzer.py` becomes provider-neutral. `VacancyAnalyzer` receives
`LLMProvider` and settings in its constructor. `SuitabilityResult` is a strict,
frozen Pydantic model with forbidden extra keys, a strict boolean, confidence
in `[0, 1]`, and a stripped reason of 1–500 characters.

System instructions state that vacancy content is untrusted data, not
instructions, and explicitly forbid obeying requests inside it, revealing the
system prompt, or copying embedded instructions into a letter. User content is
a JSON object with separate `candidate` and `vacancy` values. The
provider receives no tools, browser, filesystem, or application capability.

This is a defense boundary, not a mathematical guarantee against every model
hallucination. Local checks additionally reject empty output, Markdown fences,
known service prefaces, echoed injection phrases, and URLs absent from the
candidate profile. Longer letters are truncated to `cover_letter.max_length`
and stripped without deleting ordinary quotes or apostrophes. Semantic claims
that cannot be identified deterministically remain a documented model risk.

Provider or validation failure returns
`SuitabilityResult(suitable=False, confidence=0.0, reason="Invalid model response")`.
That row becomes `rejected_by_llm`; no letter or approval request is created.

## Factory, lifecycle, and CLI

`create_llm_provider(settings, database)` builds exactly one adapter, wraps it
in `ManagedLLMProvider`, and returns it. Clients are created once per process.
`main.run()` injects the provider into `VacancyAnalyzer` and closes it in
`finally`; no provider is created inside analysis methods.

`python3 main.py --check-llm` validates configuration, initializes SQLite,
creates the selected provider, performs one small `llm_healthcheck` text
request, prints provider/model/latency/success, and closes the provider. It does
not start a browser, Telegram, or HH workflow and never prints a key.

`python3 -m scripts.llm_smoke --provider mistral` is a manual wrapper around the
same safe path. It is outside pytest discovery and reports a readable
configuration error when no provider key is present.

## Extension contract

A future provider adds one adapter implementing `ProviderAdapter.complete()`,
normalizes errors into the existing classes, registers one explicit factory
branch and provider-specific validated settings, and adds adapter and manual
smoke tests. OpenAI Responses and Anthropic Messages must remain separate
adapters because their request/response and structured-output semantics differ
from `/chat/completions`. They must not reuse another provider's key or add an
automatic fallback.

## Verification and limitations

- Unit tests use `FakeProvider` and injected fake SDK/HTTP clients only.
- The official Mistral SDK is pinned at `2.7.0`; its wheel is platform-neutral
  and declares Python `>=3.10`.
- Installation and import are verified on the current macOS ARM64 Python
  3.14.5 environment and in a Python 3.13 environment when available.
- A real Mistral smoke-test is optional and must not run without a user-owned
  local API key.
- Mock-only verification is reported as such; it is not evidence that a remote
  custom endpoint is compatible.
- Existing approval, dry-run, browser, and Telegram safety tests must remain
  green.
