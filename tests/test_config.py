from pathlib import Path

import pytest
import yaml

from config import ConfigError, load_settings


VALID_ENV = {
    "TG_BOT_TOKEN": "test-token",
    "TG_USER_ID": "123456",
    "OLLAMA_URL": "http://localhost:11434/api/generate",
    "OLLAMA_MODEL": "llama3",
    "ENABLE_REAL_APPLY": "false",
    "BROWSER_HEADLESS": "false",
    "CHECK_INTERVAL_MINUTES": "30",
    "MAX_APPLICATIONS_PER_DAY": "5",
    "MAX_VACANCIES_PER_QUERY": "20",
    "MAX_PAGES_PER_QUERY": "2",
    "MIN_SECONDS_BETWEEN_ACTIONS": "5",
}

VALID_PROFILE = {
    "candidate": {
        "name": "Test Candidate",
        "location": "Moscow",
        "desired_positions": ["Python developer"],
        "experience_summary": "Built internal Python services.",
        "education": "",
        "technologies": ["Python", "SQLite"],
        "projects": [],
        "github_url": "",
        "salary_expectation": "",
        "work_format": ["remote"],
        "excluded_positions": ["sales"],
        "additional_information": "",
    },
    "hh": {
        "resume_name": "Python developer",
        "search_queries": ["Python developer"],
        "areas": ["1"],
        "experience_filters": ["between1And3"],
    },
    "cover_letter": {
        "language": "ru",
        "max_length": 1800,
        "style": "professional",
    },
}


def write_profile(tmp_path: Path, data: dict | None = None) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(
        yaml.safe_dump(data or VALID_PROFILE, allow_unicode=True), encoding="utf-8"
    )
    return path


def test_load_settings_uses_safe_defaults(tmp_path: Path) -> None:
    settings = load_settings(
        profile_path=write_profile(tmp_path), environ=VALID_ENV
    )

    assert settings.app_mode == "dry_run"
    assert settings.enable_real_apply is False
    assert settings.browser_backend == "cloakbrowser"
    assert settings.browser_headless is False
    assert settings.browser_profile_dir.name == ".browser-profile"
    assert settings.tg_user_id == 123456
    assert settings.profile.candidate.name == "Test Candidate"
    assert settings.profile.hh.search_queries == ("Python developer",)


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("APP_MODE", "automatic", "APP_MODE must be dry_run or approval"),
        (
            "BROWSER_BACKEND",
            "unknown",
            "BROWSER_BACKEND must be cloakbrowser or playwright",
        ),
        (
            "ENABLE_REAL_APPLY",
            "yes",
            "ENABLE_REAL_APPLY must be true or false",
        ),
        ("TG_USER_ID", "abc", "TG_USER_ID must be an integer"),
        (
            "MAX_APPLICATIONS_PER_DAY",
            "0",
            "MAX_APPLICATIONS_PER_DAY must be a positive integer",
        ),
    ],
)
def test_load_settings_rejects_invalid_environment(
    tmp_path: Path, key: str, value: str, message: str
) -> None:
    environ = {**VALID_ENV, key: value}

    with pytest.raises(ConfigError, match=message):
        load_settings(profile_path=write_profile(tmp_path), environ=environ)


def test_load_settings_reports_missing_secret_without_traceback_text(
    tmp_path: Path,
) -> None:
    environ = {**VALID_ENV, "TG_BOT_TOKEN": ""}

    with pytest.raises(ConfigError) as raised:
        load_settings(profile_path=write_profile(tmp_path), environ=environ)

    assert str(raised.value).startswith("Configuration error:")
    assert "TG_BOT_TOKEN is required" in str(raised.value)
    assert "Traceback" not in str(raised.value)


def test_load_settings_aggregates_required_profile_errors(tmp_path: Path) -> None:
    profile = {
        **VALID_PROFILE,
        "candidate": {
            **VALID_PROFILE["candidate"],
            "name": "",
            "desired_positions": [],
            "experience_summary": "",
        },
        "hh": {**VALID_PROFILE["hh"], "resume_name": "", "search_queries": []},
    }

    with pytest.raises(ConfigError) as raised:
        load_settings(profile_path=write_profile(tmp_path, profile), environ=VALID_ENV)

    message = str(raised.value)
    assert "candidate.name is required" in message
    assert "candidate.desired_positions must not be empty" in message
    assert "candidate.experience_summary is required" in message
    assert "hh.resume_name is required" in message
    assert "hh.search_queries must not be empty" in message


def test_load_settings_rejects_missing_profile_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="profile file not found"):
        load_settings(profile_path=tmp_path / "missing.yaml", environ=VALID_ENV)
