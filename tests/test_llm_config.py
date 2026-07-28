from pathlib import Path

import pytest

from config import ConfigError, load_settings
from tests.test_config import VALID_ENV, write_profile


def load(tmp_path: Path, **overrides: str):
    return load_settings(
        profile_path=write_profile(tmp_path),
        environ={**VALID_ENV, **overrides},
    )


def test_llm_defaults_preserve_existing_ollama_configuration(tmp_path: Path) -> None:
    settings = load(tmp_path)

    assert settings.llm.provider == "ollama"
    assert settings.llm.model == "llama3"
    assert settings.llm.timeout_seconds == 30
    assert settings.llm.max_retries == 1
    assert settings.llm.temperature == 0
    assert settings.llm.max_output_tokens == 1200
    assert settings.llm.max_requests_per_day == 100
    assert settings.ollama_url == "http://localhost:11434/api/generate"
    assert settings.ollama_model == "llama3"


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"LLM_PROVIDER": "unknown"}, "LLM_PROVIDER must be"),
        (
            {"LLM_PROVIDER": "mistral", "LLM_MODEL": "mistral-small-latest"},
            "MISTRAL_API_KEY is required",
        ),
        (
            {
                "LLM_PROVIDER": "mistral",
                "LLM_MODEL": "",
                "MISTRAL_API_KEY": "secret",
            },
            "LLM_MODEL is required",
        ),
        (
            {
                "LLM_PROVIDER": "mistral",
                "LLM_MODEL": "mistral-small-latest",
                "MISTRAL_API_KEY": "secret",
                "MISTRAL_BASE_URL": "http://localhost:9000",
            },
            "MISTRAL_BASE_URL must use HTTPS",
        ),
        (
            {"OLLAMA_URL": "http://ollama.example.test/api/generate"},
            "OLLAMA_URL must use HTTPS for non-loopback hosts",
        ),
        (
            {
                "LLM_PROVIDER": "openai_compatible",
                "LLM_MODEL": "custom",
                "OPENAI_COMPATIBLE_API_KEY": "secret",
                "OPENAI_COMPATIBLE_BASE_URL": "http://remote.example.test/v1",
            },
            "OPENAI_COMPATIBLE_BASE_URL must use HTTPS for non-loopback hosts",
        ),
        (
            {
                "LLM_PROVIDER": "openai_compatible",
                "LLM_MODEL": "custom",
                "OPENAI_COMPATIBLE_BASE_URL": "https://provider.example.test/v1",
            },
            "OPENAI_COMPATIBLE_API_KEY is required",
        ),
        ({"LLM_MAX_RETRIES": "-1"}, "LLM_MAX_RETRIES must be zero or greater"),
        ({"LLM_TEMPERATURE": "nan"}, "LLM_TEMPERATURE must be between 0 and 2"),
        ({"LLM_TEMPERATURE": "2.1"}, "LLM_TEMPERATURE must be between 0 and 2"),
        (
            {"LLM_MODEL": "", "OLLAMA_MODEL": ""},
            "LLM_MODEL is required",
        ),
    ],
)
def test_invalid_provider_configuration_is_rejected(
    tmp_path: Path, overrides: dict[str, str], message: str
) -> None:
    with pytest.raises(ConfigError, match=message):
        load(tmp_path, **overrides)


@pytest.mark.parametrize(
    "base_url",
    [
        "http://localhost:8000/v1",
        "http://127.0.0.1:8000/v1",
        "http://[::1]:8000/v1",
        "https://provider.example.test/v1",
    ],
)
def test_openai_compatible_accepts_https_or_loopback_http(
    tmp_path: Path, base_url: str
) -> None:
    settings = load(
        tmp_path,
        LLM_PROVIDER="openai_compatible",
        LLM_MODEL="custom-model",
        OPENAI_COMPATIBLE_BASE_URL=base_url,
        OPENAI_COMPATIBLE_API_KEY="custom-secret",
    )

    assert settings.llm.openai_compatible_base_url == base_url


def test_mistral_uses_its_own_key_and_never_exposes_it_in_errors(
    tmp_path: Path,
) -> None:
    key = "mistral-super-secret"
    settings = load(
        tmp_path,
        LLM_PROVIDER="mistral",
        LLM_MODEL="mistral-small-latest",
        MISTRAL_API_KEY=key,
    )
    assert settings.llm.mistral_api_key == key
    assert settings.llm.openai_compatible_api_key == ""

    with pytest.raises(ConfigError) as raised:
        load(
            tmp_path,
            LLM_PROVIDER="mistral",
            LLM_MODEL="mistral-small-latest",
            MISTRAL_API_KEY=key,
            MISTRAL_BASE_URL=f"http://remote.example.test/{key}",
        )

    assert key not in str(raised.value)


def test_llm_numeric_settings_are_parsed(tmp_path: Path) -> None:
    settings = load(
        tmp_path,
        LLM_TIMEOUT_SECONDS="45",
        LLM_MAX_RETRIES="0",
        LLM_TEMPERATURE="0.25",
        LLM_MAX_OUTPUT_TOKENS="900",
        LLM_MAX_REQUESTS_PER_DAY="7",
        OPENAI_COMPATIBLE_JSON_MODE="false",
    )

    assert settings.llm.timeout_seconds == 45
    assert settings.llm.max_retries == 0
    assert settings.llm.temperature == 0.25
    assert settings.llm.max_output_tokens == 900
    assert settings.llm.max_requests_per_day == 7
    assert settings.llm.openai_compatible_json_mode is False
