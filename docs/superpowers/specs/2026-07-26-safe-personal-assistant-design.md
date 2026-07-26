# Safe Personal HH Assistant Design

## Goal

Turn the existing automatic HH.ru responder into a personal, semi-automatic
assistant. The default mode may discover, analyse, store, and preview vacancies,
but it cannot submit an application. Every real application requires a fresh,
vacancy-specific Telegram approval from the configured user.

## Safety invariants

1. `APP_MODE=dry_run` is the default and cannot submit an application.
2. A real application requires `APP_MODE=approval`,
   `ENABLE_REAL_APPLY=true`, a non-expired per-vacancy approval from
   `TG_USER_ID`, a non-empty cover letter, an eligible status, and remaining
   daily capacity.
3. `approval.py` is the only component allowed to initiate a real submission.
   `main.py`, `tg_bot.py`, vacancy search, filtering, and scheduling never call
   the browser submission method.
4. The low-level browser submission method independently asks the approval
   guard to validate and atomically claim the one-use permit before it clicks
   any application control. A direct call without that permit is blocked.
5. SQLite `BEGIN IMMEDIATE` transactions, not in-memory locks, serialize the
   approval, claim, and final state transitions. The claim changes `approved`
   to `applying` before browser interaction; only the claimant can complete it
   as `applied` or `apply_failed`.
6. Daily capacity is reserved transactionally in SQLite when a row enters
   `applying`; successful applications and final-button attempts are also
   persisted. Concurrent claims therefore cannot both pass the limit, and the
   limit survives process restarts.
7. A crash that leaves a vacancy in `applying` is fail-closed: it is never
   retried automatically and requires manual inspection.

## Targeted architecture

### Configuration

`config.py` loads `.env` and `profile.yaml` into validated dataclasses. It
accepts only `dry_run` and `approval`, parses booleans and positive limits
strictly, resolves paths relative to the project, and reports all validation
errors as one readable `Configuration error` message without a traceback.

`.env.example` contains safe defaults and no credentials. `profile.example.yaml`
contains only empty/example fields. `.env`, `profile.yaml`, `agent.db`, legacy
session state, `.browser-profile/`, screenshots, and logs are ignored.

### Browser adapter

`browser_backend.py` defines the minimal `BrowserBackend` protocol used by the
HH adapter and two implementations:

- `CloakBrowserBackend`: the experimental default selected by
  `BROWSER_BACKEND=cloakbrowser`. It uses
  `cloakbrowser.launch_persistent_context_async(profile_dir,
  headless=headless)` so the same cookies, local storage, cache, and profile are
  reused across runs. It does not use proxy, GeoIP, IP rotation, external
  CAPTCHA services, or `playwright-stealth`.
- `PlaywrightBrowserBackend`: explicit fallback selected by
  `BROWSER_BACKEND=playwright`. It uses Playwright's persistent Chromium
  context with the same configured profile directory contract.

The backend is selected once during startup. A CloakBrowser launch failure
ends startup with a readable diagnostic and tells the user to explicitly set
`BROWSER_BACKEND=playwright`; there is no mid-session or automatic fallback.
The browser adapter owns only context lifecycle. It has no configuration,
method, callback, or database access related to approval decisions.

`hh_client.py` receives a started backend context and contains HH-specific
navigation and selectors. Search, vacancy reading, chat reading, and physical
submission are separate methods. Page reading returns one of:
`vacancy_loaded`, `captcha_detected`, `access_denied`, `vacancy_removed`,
`page_structure_changed`, or `network_error`. Missing description alone is
`page_structure_changed`, not proof of CAPTCHA.

CAPTCHA handling is manual-only, bounded by a timeout and attempt limit, can be
cancelled, and never loops forever. Selector failures are logged and processing
moves to the next vacancy.

### Approval and physical submission

`approval.py` owns the approval workflow and one-use permit type:

1. A suitable vacancy is stored as `pending_approval` with an expiry timestamp
   and sent to Telegram.
2. The authorized callback atomically records `approved` or `skipped`.
3. On approval, `ApprovalService` creates a random one-use permit in SQLite and
   calls the injected physical sender.
4. The physical sender's first operation calls the guard to atomically consume
   the permit. The guard repeats every safety check: mode, real-apply switch,
   expected pending approval, Telegram owner, processed status, daily limit,
   TTL, and non-empty letter.
5. Only a successful claim changes the row to `applying`; immediately before
   the final click SQLite rechecks TTL and current-day capacity, and only the
   same hashed permit can mark or complete that attempt.
6. Immediately before the final click, the attempt is persisted. After the
   click, an explicit HH success marker is required before status becomes
   `applied`; missing markers and Playwright/CloakBrowser failures become
   terminal `apply_failed` diagnostics and are never retried automatically.

All blocked attempts emit `application_blocked` without secrets. The permit is
vacancy-specific, random, persisted only in SQLite, single-use, and never sent
to Telegram or logs.

### Database

`database.py` keeps the existing `applied_jobs` table unchanged as legacy data.
It is not imported into the new status model because the old code stored
rejected and skipped vacancies there as if they were applications.

The new `vacancies` table stores identity, title, company, URL, description
hash, query, LLM decision/reason/confidence, letter, status, discovery,
approval-request, approval, expiry, applying, final-attempt, and applied
timestamps, approver ID, permit hash, and error text. Statuses are:

`discovered`, `rejected_by_filter`, `rejected_by_llm`, `pending_approval`,
`approved`, `applying`, `skipped`, `applied`, `apply_failed`, and `expired`.

SQLite constraints reject unknown statuses. Conditional updates and
`BEGIN IMMEDIATE` enforce legal transitions, duplicate protection, daily
reservations, claimant-bound completion, and one active claimant after
restarts. Final attempts that cannot be confirmed remain conservatively counted
toward that day's limit. The application never deletes or rewrites an existing
database automatically; additive columns are migrated with `ALTER TABLE`.

### LLM

`ai_analyzer.py` uses Ollama only. Suitability requests JSON with exactly
`suitable`, `confidence`, and `reason`. The parser validates JSON, exact types,
finite confidence in `[0, 1]`, and non-empty reason. Malformed output gets at
most one retry, then returns a safe rejection. Legacy text such as `NOT YES`
cannot be accepted.

The cover-letter prompt is built only from the local candidate profile and
vacancy text. It forbids invented experience, projects, education, skills,
names, and links. Empty output fails safely; output longer than the configured
maximum is truncated at the hard character limit before approval preview.

### Telegram and orchestration

`tg_bot.py` implements `/start`, `/status`, `/pause`, `/resume`, `/pending`, and
`/stats`, plus `Apply` and `Skip` inline callbacks. Editing is deliberately
deferred. Every command, text handler, and callback checks `TG_USER_ID`; other
users receive only a neutral refusal.

`main.py` initializes logging, configuration, SQLite, browser backend, Telegram,
and the agent loop. Pause stops new vacancy searches but keeps Telegram polling
alive. Search page/vacancy limits, minimum plus random delay, and processed
vacancy checks are enforced using configuration and SQLite state.

Logging uses the standard library and a rotating file handler. Structured event
names include `vacancy_discovered`, `vacancy_rejected`, `approval_requested`,
`approval_received`, `application_blocked`, `application_sent`,
`application_failed`, `captcha_detected`, and `agent_paused`. Tokens, cookies,
profiles, storage state, `.env`, and full secrets are never logged.

## CloakBrowser version and local probe

The pinned wrapper version is `cloakbrowser==0.5.2`. Official metadata requires
Python `>=3.9` and lists classifiers through Python 3.13. On this machine the
package and dependencies installed under Python 3.14.5 on Darwin arm64, and
introspection confirmed:

- `launch_async(...)`;
- `launch_context_async(...)`;
- `launch_persistent_context_async(user_data_dir, ...)`.

An isolated headed probe ran
`launch_persistent_context_async(profile, headless=False)`, opened
`https://example.com`, received HTTP 200 and title `Example Domain`, created the
persistent profile, and closed cleanly. The launched free binary reported
Chromium `145.0.7632.109.2` for `darwin-arm64`. Gatekeeper did not block this
machine, but setup documentation must include the official first-launch
right-click/Open recovery for other Macs.

This only verifies installation, API shape, and a neutral navigation on this
Mac. CloakBrowser remains experimental and does not guarantee avoidance of
detection or CAPTCHA.

## Testing and verification

Unit tests use temporary SQLite files and fakes; they never open HH.ru, send a
Telegram message, or call Ollama. They cover the requested LLM, configuration,
status transition, authorization, TTL, daily-limit, duplicate, direct low-level
call, backend selection/profile, startup-error, and dry-run cases.

A manual script under `scripts/` opens only `https://example.com` with the
selected backend and is excluded from pytest discovery. Final verification is:

```bash
python3 -m compileall .
pytest -q
python3 -c "import config, database, ai_analyzer, approval, browser_backend, hh_client, tg_bot, main"
```

A startup validation smoke test uses placeholder local configuration and must
stop before browser, Telegram, Ollama, or HH network access. No real application
is sent during development or verification.

## Non-goals and limitations

- No automatic mass applications.
- No automatic fallback between browser backends.
- No proxy, GeoIP, IP rotation, CAPTCHA-solving service, or bypass guarantee.
- No Telegram letter editor in the first safe version.
- No automatic interpretation or import of legacy `applied_jobs` rows.
- HH selectors and policies can change; failures are recorded and fail closed.
