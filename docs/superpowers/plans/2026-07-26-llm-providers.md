# Extensible LLM Providers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:subagent-driven-development` (recommended) or
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Mistral, Ollama, and custom OpenAI-compatible LLM providers behind
a provider-neutral domain contract with bounded retries, persistent SQLite
request limits, metadata-only usage, and no automatic fallback.

**Architecture:** Provider adapters normalize one transport call. A managed
provider owns quota reservations, retries, local Pydantic validation, and usage
recording. `VacancyAnalyzer` receives that provider and contains only prompt and
domain behavior.

**Tech Stack:** Python 3.13 baseline, Python 3.14.5 compatibility check,
`mistralai==2.7.0`, Pydantic 2, aiohttp, httpx, SQLite, pytest.

## Global Constraints

- Keep `APP_MODE=dry_run` and all approval/browser safety invariants unchanged.
- Select exactly one `LLM_PROVIDER`; never retry through a different provider.
- Never log or persist API keys, prompts, response bodies, candidate/vacancy
  text, Telegram data, browser state, or local paths.
- Every actual provider attempt consumes one atomically reserved SQLite daily
  request slot, including retries and crashes.
- Use the Mistral official SDK only; do not add LiteLLM, OpenAI SDK, or
  Anthropic SDK.
- Do not call real LLM APIs in pytest.
- Do not commit, push, or create a second PR without a separate user request.

---

### Task 1: Validated provider configuration and common contracts

**Files:**

- Modify: `config.py`
- Modify: `.env.example`
- Create: `llm/__init__.py`
- Create: `llm/base.py`
- Create: `llm/errors.py`
- Create: `llm/types.py`
- Create: `tests/test_llm_config.py`

**Interfaces:**

- Produces `LLMSettings` fields on `Settings`: provider, model, timeout,
  retries, temperature, output limit, daily request limit, and provider-specific
  credentials/endpoints.
- Produces `LLMRequest`, `LLMResponse`, `ProviderAdapter`, `LLMProvider`, and the
  normalized error classes from the design.

- [ ] **Step 1: Write configuration failures first**

Add literal table-driven tests that prove:

```python
@pytest.mark.parametrize(("env", "message"), [
    ({"LLM_PROVIDER": "unknown"}, "LLM_PROVIDER must be"),
    ({"LLM_PROVIDER": "mistral", "MISTRAL_API_KEY": ""},
     "MISTRAL_API_KEY is required"),
    ({"LLM_PROVIDER": "openai_compatible",
      "OPENAI_COMPATIBLE_BASE_URL": "http://remote.test/v1"},
     "must use HTTPS"),
])
def test_invalid_llm_configuration_is_rejected(...): ...
```

Also test remote HTTPS acceptance, loopback HTTP acceptance, non-negative
retries, finite temperature in `[0, 2]`, the `OLLAMA_MODEL` compatibility
fallback, and absence of a literal secret from `ConfigError` text.

- [ ] **Step 2: Run RED**

Run `pytest -q tests/test_llm_config.py`. Expected: failures because the LLM
settings and validation do not exist.

- [ ] **Step 3: Implement minimum validated settings**

Use `urllib.parse.urlparse`, `ipaddress.ip_address`, and `math.isfinite` from the
standard library. Accept HTTP only when the hostname is `localhost` or an IP
whose `is_loopback` is true. Require only the selected provider's key.

- [ ] **Step 4: Add immutable request/response types and protocols**

Implement the signatures from the design. `ProviderAdapter.complete()` consumes
one `LLMRequest`; it does not expose settings, database, or alternate providers.

- [ ] **Step 5: Run GREEN**

Run `pytest -q tests/test_config.py tests/test_llm_config.py` and
`git diff --check`.

### Task 2: Atomic SQLite LLM request quota and metadata usage

**Files:**

- Modify: `database.py`
- Create: `tests/test_llm_database.py`

**Interfaces:**

- Produces `reserve_llm_request(provider, model, operation, now, daily_limit) -> int | None`.
- Produces `complete_llm_request(request_row_id, *, success, finished_at,
  input_tokens=None, output_tokens=None, latency_ms=None,
  provider_request_id=None, error_type="") -> bool`.
- Produces `llm_requests_today(now) -> int` and `llm_usage_stats() -> dict`.

- [ ] **Step 1: Write persisted and concurrent quota tests**

Test a reservation, reopen the same SQLite file, and prove the count remains.
With limit `1`, race two different operations through a `ThreadPoolExecutor`
and assert exactly one receives an integer ID. Seed an in-progress reservation
and prove it still counts after restart.

- [ ] **Step 2: Run RED**

Run `pytest -q tests/test_llm_database.py`. Expected: missing table/method
failures.

- [ ] **Step 3: Implement additive schema and transactions**

Create `llm_requests` with metadata-only columns and indexes on `started_at` and
`provider`. In `reserve_llm_request`, use `BEGIN IMMEDIATE`, calculate local-day
UTC bounds using the existing `_day_bounds`, count all rows, then insert in the
same transaction. Do not store request or response text.

- [ ] **Step 4: Test completion metadata and privacy**

Complete success and failure rows, then inspect `PRAGMA table_info` and queried
values. Assert there are no columns named prompt, response, api_key, profile,
vacancy, or path and that optional usage remains nullable.

- [ ] **Step 5: Run GREEN**

Run `pytest -q tests/test_database.py tests/test_llm_database.py`.

### Task 3: Managed retries, validation, usage, and FakeProvider

**Files:**

- Create: `llm/managed.py`
- Create: `llm/providers/__init__.py`
- Create: `llm/providers/fake.py`
- Create: `tests/test_llm_managed.py`

**Interfaces:**

- Produces `ManagedLLMProvider(adapter, database, max_retries,
  max_requests_per_day, sleep=asyncio.sleep, now_factory=...)`.
- Produces `FakeProvider(outcomes, delay_seconds=0)` where each outcome is an
  `LLMResponse` or normalized `LLMError`.

- [ ] **Step 1: Write retry classification tests**

Use a real temporary `Database` and `FakeProvider` outcomes. Prove timeout and
429 retry once, delays are bounded literals (`[0.5]` for the first retry), 401
does not retry, and no path changes the fake provider identity.

- [ ] **Step 2: Run RED**

Run `pytest -q tests/test_llm_managed.py`. Expected: missing managed/fake
classes.

- [ ] **Step 3: Implement bounded execution**

For each attempt: reserve a SQLite row, call only the injected adapter, record
metadata or error category, and either return or apply the normalized retry
policy. Raise `LLMDailyLimitError` before the adapter call when reservation
fails. Never include exception bodies in logs.

- [ ] **Step 4: Add Pydantic structured validation tests**

Define a strict test model and queue malformed JSON, wrong types, extra fields,
and out-of-range values. Prove one correction retry is possible but total calls
never exceed `max_retries + 1`; final failure is
`LLMInvalidResponseError`.

- [ ] **Step 5: Add usage privacy test**

Return token usage and request ID, capture logs, and query SQLite. Assert those
metadata are present while a sentinel API key, prompt, and response text are
absent from both.

- [ ] **Step 6: Run GREEN**

Run `pytest -q tests/test_llm_managed.py tests/test_llm_database.py`.

### Task 4: Ollama and OpenAI-compatible adapters

**Files:**

- Create: `llm/providers/ollama.py`
- Create: `llm/providers/openai_compatible.py`
- Create: `tests/test_llm_http_providers.py`

**Interfaces:**

- Produces `OllamaProvider(url, session=None)`.
- Produces `OpenAICompatibleProvider(base_url, api_key, json_mode, session=None)`.
- Both implement `ProviderAdapter.complete()` and `close()`.

- [ ] **Step 1: Write exact outbound contract tests**

Inject a fake aiohttp-style session that records complete request data. Assert
Ollama uses `system`, `prompt`, configured model/options, and JSON format only
for structured requests. Assert custom API posts only to
`<base>/chat/completions`, uses its dedicated bearer key, separate messages,
and optional `json_object` response mode.

- [ ] **Step 2: Run RED**

Run `pytest -q tests/test_llm_http_providers.py`. Expected: provider modules
missing.

- [ ] **Step 3: Implement reusable sessions and responses**

Create the session lazily once, measure latency with `time.perf_counter`, map
available usage fields, validate a single string response, and close only owned
sessions.

- [ ] **Step 4: Add error-normalization tests**

For literal status codes 401, 403, 429, 500, and malformed envelopes, assert
the exact normalized class. Check that response bodies and API keys are absent
from `str(error)`.

- [ ] **Step 5: Run GREEN**

Run `pytest -q tests/test_llm_http_providers.py`.

### Task 5: Official Mistral 2.7.0 adapter and factory

**Files:**

- Create: `llm/providers/mistral.py`
- Create: `llm/factory.py`
- Create: `tests/test_llm_mistral.py`
- Create: `tests/test_llm_factory.py`
- Modify: `requirements.txt`

**Interfaces:**

- Produces `MistralProvider(api_key, base_url=None, client=None,
  http_client=None)`.
- Produces `create_llm_provider(settings, database) -> LLMProvider`.

- [ ] **Step 1: Pin and inspect the SDK before production imports**

Pin `mistralai==2.7.0`, `pydantic==2.13.4`, and `httpx==0.28.1`. Install in an
isolated Python environment and verify `Mistral`, `chat.complete_async`, JSON
Schema `response_format`, request `timeout_ms`, and async resource ownership.

- [ ] **Step 2: Write Mistral payload tests**

Inject a fake SDK client returning a complete response-shaped object. For
structured input, assert the exact literal response format:

```python
{
    "type": "json_schema",
    "json_schema": {"name": "vacancy_analysis", "schema": schema,
                    "strict": True},
}
```

Assert two messages, no tools, model, temperature, max tokens, timeout, response
ID, model, usage, and finish reason.

- [ ] **Step 3: Run RED and implement adapter**

Run `pytest -q tests/test_llm_mistral.py`, implement the minimum adapter, then
rerun. Normalize status 401/403/429/5xx and httpx timeout/transport errors. Use
an owned `httpx.AsyncClient`; disable SDK retries and close the owned client.

- [ ] **Step 4: Write and implement factory tests**

For three validated settings objects, assert the managed provider wraps exactly
one corresponding adapter. Unknown provider raises `LLMConfigurationError`.
Constructors must not perform network calls and the factory must expose no
fallback list.

- [ ] **Step 5: Run GREEN**

Run `pytest -q tests/test_llm_mistral.py tests/test_llm_factory.py` and
`pip check`.

### Task 6: Provider-neutral analyzer and prompt-injection boundary

**Files:**

- Replace: `ai_analyzer.py`
- Replace: `tests/test_ai_analyzer.py`
- Modify: `tests/test_main.py`

**Interfaces:**

- Produces strict Pydantic `SuitabilityResult`.
- Produces `VacancyAnalyzer(settings, provider)` with `assess()` and
  `generate_cover_letter()` retaining the current orchestration signatures.

- [ ] **Step 1: Write domain schema tests**

Test a valid result and reject `NOT YES`, malformed JSON, string booleans,
boolean confidence, non-finite/out-of-range confidence, extra fields, and reason
length over 500 through the real managed-provider structured path.

- [ ] **Step 2: Run RED and implement strict model**

Run `pytest -q tests/test_ai_analyzer.py`; implement Pydantic
`ConfigDict(strict=True, extra="forbid", frozen=True)` plus constrained fields.

- [ ] **Step 3: Write prompt-boundary tests**

Pass a vacancy containing all four requested injection sentences. Inspect the
captured `LLMRequest` and assert the system message calls vacancy text untrusted,
the user message is valid JSON with separate profile/vacancy values, no secret
settings are present, and a fake unsuitable response stays unsuitable.

- [ ] **Step 4: Implement minimal prompts and fail-closed behavior**

Serialize only non-empty candidate profile fields and title/description. Catch
normalized provider errors, log category/provider/operation only, and return a
safe unsuitable result. Do not import aiohttp, Mistral, URLs, keys, or factory.

- [ ] **Step 5: Write and implement letter postprocessing tests**

Test empty output, Markdown fences, service prefaces, an echoed injection
phrase, an unconfigured URL, ordinary quotes/apostrophes, and length overflow.
Reject unsafe forms; preserve normal punctuation; truncate safely to the
configured character maximum.

- [ ] **Step 6: Prove provider failure cannot reach approval**

Run `process_vacancy` with a failing fake provider and real temporary database.
Assert `rejected_by_llm`, no cover letter, and no Telegram action preview.

- [ ] **Step 7: Run GREEN**

Run `pytest -q tests/test_ai_analyzer.py tests/test_main.py`.

### Task 7: Main lifecycle, diagnostics, and manual smoke

**Files:**

- Modify: `main.py`
- Create: `scripts/llm_smoke.py`
- Create: `tests/test_llm_cli.py`

**Interfaces:**

- Produces `check_llm(settings) -> int` or an equivalent async helper.
- Adds CLI flag `--check-llm`.

- [ ] **Step 1: Write no-browser CLI tests**

Inject a fake provider factory and prove `--check-llm` performs only the
healthcheck, prints provider/model/latency/success, closes the provider, and
does not instantiate browser or Telegram components. A missing Mistral key must
return code 2 without traceback or secret values.

- [ ] **Step 2: Run RED and implement dependency injection**

Run `pytest -q tests/test_llm_cli.py`. In normal `run`, initialize SQLite,
create one provider, inject `VacancyAnalyzer`, and close the provider in
`finally`. Do not modify approval, Telegram callback, or browser classes.

- [ ] **Step 3: Add manual smoke script**

Implement `python3 -m scripts.llm_smoke --provider <name>` by applying a process
environment override and invoking the same check helper. It must not be named
`test_*`, must never start HH/Telegram, and must report missing keys cleanly.

- [ ] **Step 4: Run GREEN**

Run `pytest -q tests/test_llm_cli.py tests/test_browser_backend.py
tests/test_approval.py`.

### Task 8: Documentation, compatibility, and final verification

**Files:**

- Modify: `README.md`
- Modify: `SETUP_CHECKLIST.md`
- Modify: `.env.example`
- Modify: `docs/superpowers/specs/2026-07-26-llm-providers-design.md`

**Interfaces:** Documentation must match tested code and contain exact setup for
all three provider values plus the extension contract.

- [ ] **Step 1: Update user configuration instructions**

Document Ollama, Mistral, and custom `/chat/completions` examples; safe
`--check-llm`; request quota semantics; privacy; no fallback; no cost estimate;
and that custom compatibility is not guaranteed.

- [ ] **Step 2: Document future provider additions**

For OpenAI Responses and Anthropic Messages, describe separate adapter, key,
factory registration, normalized errors, structured-output mapping, fake tests,
and manual smoke-test. Do not add production placeholders.

- [ ] **Step 3: Verify Python environments**

Install exact runtime and dev requirements in clean Python 3.13 and current
macOS ARM64 Python 3.14.5 environments. Import all direct dependencies and run
`pip check`. If Python 3.13 is unavailable, install the documented Homebrew
formula before claiming compatibility.

- [ ] **Step 4: Run required final checks**

With placeholder dry-run Ollama configuration, run:

```bash
python3 -m compileall .
pytest -q
python3 main.py --check-config
git diff --check
python3 -m pip check
```

Also search tracked files for secret-valued local files and direct provider
construction outside `llm/factory.py`. Do not run a real Mistral smoke without
a pre-existing user-owned `MISTRAL_API_KEY`.

- [ ] **Step 5: Independent review**

Review the final diff for provider fallback, secret leakage, unbounded retry,
non-atomic quota, prompt persistence, and changes outside the LLM/main boundary.
Fix only reproduced issues with a failing test first.
