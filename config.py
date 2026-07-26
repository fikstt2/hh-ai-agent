from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

import yaml
from dotenv import dotenv_values


BASE_DIR = Path(__file__).resolve().parent


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class CandidateProfile:
    name: str
    location: str
    desired_positions: tuple[str, ...]
    experience_summary: str
    education: str
    technologies: tuple[str, ...]
    projects: tuple[str, ...]
    github_url: str
    salary_expectation: str
    work_format: tuple[str, ...]
    excluded_positions: tuple[str, ...]
    additional_information: str


@dataclass(frozen=True)
class HHProfile:
    resume_name: str
    search_queries: tuple[str, ...]
    areas: tuple[str, ...]
    experience_filters: tuple[str, ...]


@dataclass(frozen=True)
class CoverLetterProfile:
    language: str
    max_length: int
    style: str


@dataclass(frozen=True)
class Profile:
    candidate: CandidateProfile
    hh: HHProfile
    cover_letter: CoverLetterProfile


@dataclass(frozen=True)
class Settings:
    tg_bot_token: str
    tg_user_id: int
    ollama_url: str
    ollama_model: str
    app_mode: str
    enable_real_apply: bool
    browser_backend: str
    browser_headless: bool
    browser_profile_dir: Path
    database_path: Path
    log_path: Path
    check_interval_minutes: int
    max_applications_per_day: int
    max_vacancies_per_query: int
    max_pages_per_query: int
    min_seconds_between_actions: int
    approval_ttl_minutes: int
    captcha_timeout_seconds: int
    captcha_max_attempts: int
    profile: Profile


def _path(value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else BASE_DIR / path


def _strings(section: Mapping[str, object], key: str) -> tuple[str, ...]:
    value = section.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise TypeError(f"{key} must be a list of strings")
    return tuple(item.strip() for item in value if item.strip())


def _text(section: Mapping[str, object], key: str, default: str = "") -> str:
    value = section.get(key, default)
    if not isinstance(value, str):
        raise TypeError(f"{key} must be a string")
    return value.strip()


def load_settings(
    env_path: Path | str = BASE_DIR / ".env",
    profile_path: Path | str | None = None,
    environ: Mapping[str, str] | None = None,
) -> Settings:
    values = {key: value for key, value in dotenv_values(env_path).items() if value is not None}
    values.update(os.environ if environ is None else environ)
    errors: list[str] = []

    def required(key: str) -> str:
        value = values.get(key, "").strip()
        if not value:
            errors.append(f"{key} is required")
        return value

    def boolean(key: str, default: str) -> bool:
        value = values.get(key, default).strip().lower()
        if value not in {"true", "false"}:
            errors.append(f"{key} must be true or false")
            return False
        return value == "true"

    def positive_integer(key: str, default: str) -> int:
        value = values.get(key, default).strip()
        try:
            parsed = int(value)
        except ValueError:
            errors.append(f"{key} must be an integer")
            return 1
        if parsed <= 0:
            errors.append(f"{key} must be a positive integer")
            return 1
        return parsed

    app_mode = values.get("APP_MODE", "dry_run").strip().lower()
    if app_mode not in {"dry_run", "approval"}:
        errors.append("APP_MODE must be dry_run or approval")

    browser_backend = values.get("BROWSER_BACKEND", "cloakbrowser").strip().lower()
    if browser_backend not in {"cloakbrowser", "playwright"}:
        errors.append("BROWSER_BACKEND must be cloakbrowser or playwright")

    tg_user_id_text = required("TG_USER_ID")
    try:
        tg_user_id = int(tg_user_id_text)
    except ValueError:
        errors.append("TG_USER_ID must be an integer")
        tg_user_id = 0

    selected_profile = Path(profile_path) if profile_path else _path(values.get("PROFILE_PATH", "profile.yaml"))
    if not selected_profile.exists():
        errors.append(f"profile file not found: {selected_profile}")
        raw_profile: object = {}
    else:
        try:
            raw_profile = yaml.safe_load(selected_profile.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            errors.append(f"cannot read profile: {exc}")
            raw_profile = {}

    if not isinstance(raw_profile, dict):
        errors.append("profile root must be a mapping")
        raw_profile = {}

    def section(name: str) -> Mapping[str, object]:
        value = raw_profile.get(name, {})
        if not isinstance(value, dict):
            errors.append(f"{name} must be a mapping")
            return {}
        return value

    candidate_data = section("candidate")
    hh_data = section("hh")
    cover_data = section("cover_letter")

    try:
        candidate = CandidateProfile(
            name=_text(candidate_data, "name"),
            location=_text(candidate_data, "location"),
            desired_positions=_strings(candidate_data, "desired_positions"),
            experience_summary=_text(candidate_data, "experience_summary"),
            education=_text(candidate_data, "education"),
            technologies=_strings(candidate_data, "technologies"),
            projects=_strings(candidate_data, "projects"),
            github_url=_text(candidate_data, "github_url"),
            salary_expectation=_text(candidate_data, "salary_expectation"),
            work_format=_strings(candidate_data, "work_format"),
            excluded_positions=_strings(candidate_data, "excluded_positions"),
            additional_information=_text(candidate_data, "additional_information"),
        )
        hh = HHProfile(
            resume_name=_text(hh_data, "resume_name"),
            search_queries=_strings(hh_data, "search_queries"),
            areas=_strings(hh_data, "areas"),
            experience_filters=_strings(hh_data, "experience_filters"),
        )
        max_length_value = cover_data.get("max_length", 1800)
        if isinstance(max_length_value, bool) or not isinstance(max_length_value, int) or max_length_value <= 0:
            errors.append("cover_letter.max_length must be a positive integer")
            max_length_value = 1800
        cover_letter = CoverLetterProfile(
            language=_text(cover_data, "language", "ru"),
            max_length=max_length_value,
            style=_text(cover_data, "style", "professional"),
        )
    except TypeError as exc:
        errors.append(str(exc))
        candidate = CandidateProfile("", "", (), "", "", (), (), "", "", (), (), "")
        hh = HHProfile("", (), (), ())
        cover_letter = CoverLetterProfile("ru", 1800, "professional")

    if not candidate.name:
        errors.append("candidate.name is required")
    if not candidate.desired_positions:
        errors.append("candidate.desired_positions must not be empty")
    if not candidate.experience_summary:
        errors.append("candidate.experience_summary is required")
    if not hh.resume_name:
        errors.append("hh.resume_name is required")
    if not hh.search_queries:
        errors.append("hh.search_queries must not be empty")

    settings = Settings(
        tg_bot_token=required("TG_BOT_TOKEN"),
        tg_user_id=tg_user_id,
        ollama_url=values.get("OLLAMA_URL", "http://localhost:11434/api/generate").strip(),
        ollama_model=values.get("OLLAMA_MODEL", "llama3").strip(),
        app_mode=app_mode,
        enable_real_apply=boolean("ENABLE_REAL_APPLY", "false"),
        browser_backend=browser_backend,
        browser_headless=boolean("BROWSER_HEADLESS", "false"),
        browser_profile_dir=_path(values.get("BROWSER_PROFILE_DIR", ".browser-profile")),
        database_path=_path(values.get("DATABASE_PATH", "agent.db")),
        log_path=_path(values.get("LOG_PATH", "agent.log")),
        check_interval_minutes=positive_integer("CHECK_INTERVAL_MINUTES", "30"),
        max_applications_per_day=positive_integer("MAX_APPLICATIONS_PER_DAY", "5"),
        max_vacancies_per_query=positive_integer("MAX_VACANCIES_PER_QUERY", "20"),
        max_pages_per_query=positive_integer("MAX_PAGES_PER_QUERY", "2"),
        min_seconds_between_actions=positive_integer("MIN_SECONDS_BETWEEN_ACTIONS", "5"),
        approval_ttl_minutes=positive_integer("APPROVAL_TTL_MINUTES", "30"),
        captcha_timeout_seconds=positive_integer("CAPTCHA_TIMEOUT_SECONDS", "120"),
        captcha_max_attempts=positive_integer("CAPTCHA_MAX_ATTEMPTS", "2"),
        profile=Profile(candidate, hh, cover_letter),
    )
    if errors:
        raise ConfigError("Configuration error:\n- " + "\n- ".join(dict.fromkeys(errors)))
    return settings
