# Safe Personal HH Assistant Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace automatic HH.ru submissions with a fail-closed personal
assistant that defaults to dry-run and requires one authorized Telegram approval
for every real application.

**Architecture:** Keep the repository's root-module layout, add only the missing
browser and approval boundaries, and make SQLite the authoritative state. The HH
adapter owns selectors, the browser adapter owns browser lifecycle, and the
approval guard owns the only capability that can unlock a physical submit.

**Tech Stack:** Python 3.13 baseline with a verified local Python 3.14.5 run,
SQLite, aiogram 3, aiohttp, PyYAML, python-dotenv, Playwright, CloakBrowser 0.5.2,
pytest, pytest-asyncio.

## Global Constraints

- Default `APP_MODE=dry_run`; no test or smoke command may open HH.ru.
- Real submit requires `APP_MODE=approval`, `ENABLE_REAL_APPLY=true`, a fresh
  approval by `TG_USER_ID`, remaining SQLite daily capacity, eligible status,
  and a non-empty letter.
- `approval.py` alone initiates submission; the low-level sender independently
  claims a one-use SQLite permit before browser interaction.
- Preserve `applied_jobs` unchanged and never interpret it automatically.
- Use `CloakBrowserBackend` only when explicitly configured; never fall back
  automatically to Playwright during startup or a running session.
- Do not use proxy, GeoIP, IP rotation, CAPTCHA services, or
  `playwright-stealth`.
- Do not add secrets, cookies, profiles, SQLite databases, session state,
  screenshots, or logs to Git.
- Do not commit, push, open a PR, or merge without a separate user request.

---

### Task 1: Safe configuration, templates, and logging

**Files:**

- Modify: `config.py`
- Modify: `.gitignore`
- Create: `.env.example`
- Create: `profile.example.yaml`
- Create: `logging_setup.py`
- Create: `tests/test_config.py`

**Interfaces:**

- Produces: `ConfigError`, `Settings`, `CandidateProfile`, `HHProfile`,
  `CoverLetterProfile`, `load_settings(env_path, profile_path, environ)`.
- Produces: `configure_logging(log_path: Path) -> None`.

- [ ] **Step 1: Write failing configuration tests**

Cover valid parsing, default dry-run, unknown app/browser modes, missing token,
missing numeric/boolean values, missing required profile fields, and the exact
readable `Configuration error:` prefix. Use a temporary YAML file and a literal
environment mapping; do not mutate the process environment.

- [ ] **Step 2: Verify red**

Run `pytest -q tests/test_config.py` and confirm collection fails because the
new dataclasses and loader do not exist.

- [ ] **Step 3: Implement the minimal loader**

Use frozen dataclasses and `yaml.safe_load`. Required fields are
`TG_BOT_TOKEN`, integer `TG_USER_ID`, `candidate.name`, non-empty
`candidate.desired_positions`, `candidate.experience_summary`, `hh.resume_name`,
and non-empty `hh.search_queries`. Parse all limits as positive integers,
`ENABLE_REAL_APPLY` and `BROWSER_HEADLESS` from only `true`/`false`, and modes
from fixed sets. Aggregate errors and raise one `ConfigError`.

- [ ] **Step 4: Add safe files and rotating logging**

Ignore `.env`, `profile.yaml`, `agent.db*`, `state.json`, `.browser-profile/`,
`*.log*`, `captcha*.png`, and Python/IDE artifacts. Build the requested example
files without personal data. Configure console INFO and a 2 MiB, 3-backup
`RotatingFileHandler` using only the standard library.

- [ ] **Step 5: Verify green and inspect scope**

Run `pytest -q tests/test_config.py`, `git diff --check`, and
`git status --short`. Confirm no secret-valued file is present.

### Task 2: Transactional vacancy state and legacy-safe migration

**Files:**

- Replace: `database.py`
- Create: `tests/test_database.py`
- Create: `tests/test_approval.py`

**Interfaces:**

- Produces: `VacancyStatus`, `Vacancy`, and `Database(path)`.
- Produces: `Database.init()`, `discover(...)`, `get(job_id)`,
  `transition(job_id, expected, target, **fields)`, `request_approval(...)`,
  `approve(...) -> ApplicationPermission | None`, `skip(...)`,
  `claim_application(...) -> ClaimResult`, `complete_application(...)`,
  `expire_pending(...)`, `pending()`, `stats()`, and `applied_today(now)`.
- Preserves: `chat_messages` and any pre-existing `applied_jobs` table.

- [ ] **Step 1: Write failing state-transition tests**

Test legal status changes, rejected illegal changes, duplicate discovery,
pending expiry, skip, applied-today persistence across two `Database` instances,
and preservation/non-import of a seeded legacy `applied_jobs` row.

- [ ] **Step 2: Verify red**

Run `pytest -q tests/test_database.py` and confirm failures are due to missing
new database interfaces.

- [ ] **Step 3: Implement schema and atomic transitions**

Create `vacancies` and indexes with a status `CHECK`. Use context-managed
connections, `PRAGMA foreign_keys=ON`, UTC ISO timestamps, conditional `UPDATE`,
and `BEGIN IMMEDIATE` for approval, claim, completion, and limit checks. Never
drop, rename, or read `applied_jobs` into `vacancies`.

- [ ] **Step 4: Write failing permit and concurrency tests**

Test dry-run, disabled real apply, foreign Telegram ID, expired approval, empty
letter, daily limit, repeated apply, invalid direct permit, and two concurrent
SQLite claims where exactly one succeeds. Each assertion targets returned state
and persisted status, not mock call counts.

- [ ] **Step 5: Implement one-use permit storage**

Store only SHA-256 of a `secrets.token_urlsafe()` permit. Approval records the
authorized user and expiry. Claim repeats every safety condition in one
`BEGIN IMMEDIATE` transaction and changes `approved` to `applying`. Completion
requires `applying` and writes either `applied`/`applied_at` or
`apply_failed`/`error_text`.

- [ ] **Step 6: Verify green**

Run `pytest -q tests/test_database.py tests/test_approval.py` and
`git diff --check`.

### Task 3: Strict Ollama analysis and profile-only letters

**Files:**

- Replace: `ai_analyzer.py`
- Create: `tests/test_ai_analyzer.py`

**Interfaces:**

- Produces: `SuitabilityResult(suitable: bool, confidence: float, reason: str)`.
- Produces: `parse_suitability(raw: str) -> SuitabilityResult`.
- Produces: `OllamaAnalyzer(settings).assess(title, description)` and
  `generate_cover_letter(title, description)`.

- [ ] **Step 1: Write failing parser tests**

Test valid JSON, malformed JSON, missing/extra keys, non-boolean suitable,
boolean confidence, non-finite/out-of-range confidence, empty reason, and
`NOT YES`. The latter must raise a parse error and can never return suitable.

- [ ] **Step 2: Verify red**

Run `pytest -q tests/test_ai_analyzer.py` and confirm the old substring parser
cannot meet the structured contract.

- [ ] **Step 3: Implement strict parser and one retry**

Use only `json`, `math`, and aiohttp. Request Ollama's JSON format, accept exact
keys/types, retry once on transport or parse failure, and then return a safe
`SuitabilityResult(False, 0.0, "Invalid model response")`.

- [ ] **Step 4: Add letter behavior tests and implementation**

Capture the fake Ollama payload and prove the prompt contains only the supplied
profile and vacancy, includes anti-invention rules, contains none of the removed
author data, rejects empty output, and limits the result to
`cover_letter.max_length` characters.

- [ ] **Step 5: Verify green**

Run `pytest -q tests/test_ai_analyzer.py` and search the working tree for the
removed author name, repository URL, project, salary, stack, and resume title.

### Task 4: Browser backend adapter and isolated smoke script

**Files:**

- Create: `browser_backend.py`
- Create: `scripts/browser_smoke.py`
- Create: `tests/test_browser_backend.py`
- Modify: `requirements.txt`
- Create: `requirements-dev.txt`

**Interfaces:**

- Produces: `BrowserBackend` protocol with async `start()` and `close()`.
- Produces: `CloakBrowserBackend`, `PlaywrightBrowserBackend`,
  `BrowserLaunchError`, and `create_browser_backend(settings)`.
- `start()` returns a Playwright-compatible persistent `BrowserContext`.

- [ ] **Step 1: Write failing backend tests**

Test configured selection, unknown backend rejection, profile/headless arguments
passed to injected async launchers, a readable CloakBrowser startup error, no
approval/database attributes on either adapter, and explicit close behavior.

- [ ] **Step 2: Verify red**

Run `pytest -q tests/test_browser_backend.py`; confirm the module is missing.

- [ ] **Step 3: Implement the minimal adapters**

Pin `cloakbrowser==0.5.2` and direct dependencies to versions installed and
verified in a clean venv. Cloak uses
`launch_persistent_context_async(profile_dir, headless=...)`; Playwright uses
`async_playwright().start()` plus
`chromium.launch_persistent_context(profile_dir, headless=...)`. Dynamic imports
make startup errors readable and keep unit tests network-free. Do not import
`playwright-stealth`.

- [ ] **Step 4: Add the manual neutral smoke script**

Accept `--backend`, `--headless`, and `--profile-dir`; open only
`https://example.com`, print HTTP status/title/backend, close in `finally`, and
exit non-zero with a readable launch diagnostic. Name it outside pytest's
discovery pattern and never call it from the unit suite.

- [ ] **Step 5: Verify green and dependency compatibility**

Run backend unit tests. Build clean Python 3.14.5 and, if available, Python 3.13
venvs; install both requirements files; import every direct dependency. Record
whether 3.14 is locally verified versus officially classified. Do not rerun the
manual browser smoke unless needed because the isolated pre-plan probe already
opened `example.com` successfully.

### Task 5: HH adapter, approval service, and physical send defence

**Files:**

- Create: `approval.py`
- Replace: `hh_client.py`
- Create: `vacancy_filter.py`
- Create: `tests/test_hh_client.py`
- Extend: `tests/test_approval.py`

**Interfaces:**

- Produces: `ApprovalService.approve_and_apply(job_id, telegram_user_id)` and
  `skip(job_id, telegram_user_id)`.
- Produces: `HHClient.search_vacancies(...)`, `read_vacancy(...)`,
  `submit_application(permission)`, and `check_messages(...)`.
- Produces: `PageState`, `VacancySummary`, `VacancyDetails`, and
  `title_rejection_reason(title, excluded_positions)`.

- [ ] **Step 1: Write failing direct-send and page-state tests**

Call `HHClient.submit_application` directly with a missing/forged permit and a
fake page whose application controls would fail the test if touched. Test dry
run for both backend labels. Also test classification of loaded vacancy,
explicit CAPTCHA, denied, removed, changed selectors, and navigation error.

- [ ] **Step 2: Verify red**

Run the exact new tests and confirm they fail against the old direct-click
implementation.

- [ ] **Step 3: Implement physical sender defence first**

Make permit claim the first operation in `submit_application`. On block, log
`application_blocked` and return without creating a page. After a valid claim,
perform the existing resume/letter/submit selector flow. Catch browser errors,
atomically mark `apply_failed`, and never retry a physical submit automatically.

- [ ] **Step 4: Implement search/read/filter boundaries**

Build query URLs with `urllib.parse.urlencode`; enforce configured page and
vacancy limits; classify explicit page signals; treat missing description as
`page_structure_changed`; bound manual CAPTCHA attempts/timeouts; and apply
minimum plus random delays. Keep each vacancy in its own page and close it in
`finally`.

- [ ] **Step 5: Implement ApprovalService**

Only `approve_and_apply` obtains a DB permit and invokes the injected physical
sender. Unauthorized, stale, duplicate, over-limit, and mode-disabled requests
return a neutral blocked result. `skip` atomically changes only a live pending
vacancy.

- [ ] **Step 6: Verify green**

Run `pytest -q tests/test_hh_client.py tests/test_approval.py` and inspect all
callers of `submit_application`; only `approval.py` may invoke it.

### Task 6: Telegram controls and semi-automatic agent loop

**Files:**

- Replace: `tg_bot.py`
- Replace: `main.py`
- Create: `tests/test_tg_bot.py`
- Create: `tests/test_main.py`

**Interfaces:**

- Produces: `AgentControl(paused: bool)` and `TelegramService`.
- Produces: `TelegramService.send_preview(...)`, `notify(...)`,
  `request_captcha(...)`, `start_polling()`, and `stop()`.
- Produces: `process_vacancy(...)`, `agent_loop(...)`, and CLI
  `python3 main.py --check-config`.

- [ ] **Step 1: Write failing authorization and dry-run flow tests**

Use fake messages/callbacks to prove foreign users get a neutral refusal and
cannot mutate state. Test pause/resume, status/pending/stats rendering, approval
and skip callback routing, dry-run preview without apply buttons, approval
preview with buttons, and no sender invocation from the search loop.

- [ ] **Step 2: Verify red**

Run `pytest -q tests/test_tg_bot.py tests/test_main.py` and confirm the old global
bot and automatic loop fail the required contracts.

- [ ] **Step 3: Implement TelegramService**

Construct aiogram objects only after validated configuration. Register commands
and callbacks as service methods, HTML-escape vacancy content, restrict every
handler to `TG_USER_ID`, add bounded CAPTCHA wait/cancel state, and avoid
returning IDs, token hints, or configuration details to strangers.

- [ ] **Step 4: Implement orchestration**

On each discovered vacancy: insert once, read page, persist failure state,
hard-filter, assess with Ollama, generate a bounded letter, then either send a
dry-run preview while retaining a processed record or atomically request
approval and send buttons. Pause suppresses only new searches. SQLite is queried
for counts and duplicate protection after every restart.

- [ ] **Step 5: Implement readable startup failure**

Catch `ConfigError` at the CLI boundary, print only its message to stderr, and
exit 2. `--check-config` validates files and initializes no browser, Telegram,
Ollama, or HH connection.

- [ ] **Step 6: Verify green**

Run the Telegram/main tests, then the complete `pytest -q` suite.

### Task 7: Documentation, clean-install proof, and final safety audit

**Files:**

- Replace: `README.md`
- Create: `SETUP_CHECKLIST.md`
- Modify: requirements files if clean-install evidence requires compatible pins

**Interfaces:**

- Documents the exact behavior implemented by Tasks 1-6; introduces no new
  runtime interface.

- [ ] **Step 1: Write factual setup documentation**

Cover purpose, architecture, macOS ARM64 installation via `python3`, supported
Python, Ollama, BotFather, profile, first HH login, dry-run, approval mode,
pre-submit checklist, Telegram commands, DB backup, profile/session reset,
Gatekeeper, common errors, legacy table, and limitations. State that
CloakBrowser is experimental and cannot guarantee CAPTCHA/detection avoidance.

- [ ] **Step 2: Add the beginner checklist**

Provide copy/paste commands from clone through safe dry-run, manual browser
smoke, first login, inspection of pending cards, and the two explicit config
changes required before approval mode. Never include a real token or profile.

- [ ] **Step 3: Run fresh verification**

Run:

```bash
python3 -m compileall .
pytest -q
python3 -c "import config, database, ai_analyzer, approval, browser_backend, hh_client, tg_bot, main"
```

Create temporary `.env` and profile files outside tracked paths and run the
documented `--check-config` command. Do not run `main.py` normally and do not
open HH.ru.

- [ ] **Step 4: Audit repository safety**

Run `git diff --check`, `git status --short`, `git diff --stat`, and targeted
`rg` searches for the removed personal data, tokens, cookies, profile paths,
`playwright-stealth`, direct submit callers, and ignored runtime artifacts.
Confirm all tests are local fakes and no database/profile/session file is
tracked.

- [ ] **Step 5: Prepare the requested final report**

Report the original audit, exact changed files, architecture, command outputs,
macOS ARM64 install/run commands, manual user actions, required `.env` and YAML
fields, safe dry-run command, approval transition, remaining limitations, and
any unverified behavior. Do not claim HH selector or real-submit success because
neither is exercised against the live service.
