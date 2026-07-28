from pathlib import Path

import main
from llm.types import LLMRequest, LLMResponse
from tests.test_config import VALID_ENV, write_profile


class CheckProvider:
    def __init__(self):
        self.requests: list[LLMRequest] = []
        self.closed = False

    async def generate_text(self, request: LLMRequest) -> LLMResponse:
        self.requests.append(request)
        return LLMResponse(
            text="OK",
            provider="fake",
            model=request.model,
            latency_ms=17,
        )

    async def close(self) -> None:
        self.closed = True


def write_env(tmp_path: Path, **overrides: str) -> Path:
    values = {
        **VALID_ENV,
        "LLM_PROVIDER": "ollama",
        "LLM_MODEL": "test-model",
        "DATABASE_PATH": str(tmp_path / "agent.db"),
        **overrides,
    }
    path = tmp_path / ".env"
    path.write_text("\n".join(f"{key}={value}" for key, value in values.items()))
    return path


def test_check_llm_uses_injected_provider_without_browser_or_telegram(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    selected = CheckProvider()
    factory_calls = 0

    def factory(settings, database):
        nonlocal factory_calls
        factory_calls += 1
        return selected

    def browser_must_not_start(settings):
        raise AssertionError("browser must not start")

    monkeypatch.setattr(main, "create_browser_backend", browser_must_not_start)

    result = main.cli(
        [
            "--env-file",
            str(write_env(tmp_path)),
            "--profile",
            str(write_profile(tmp_path)),
            "--check-llm",
        ],
        provider_factory=factory,
    )

    output = capsys.readouterr()
    assert result == 0
    assert output.out.strip() == (
        "LLM check: provider=fake model=test-model latency_ms=17 success=true"
    )
    assert output.err == ""
    assert factory_calls == 1
    assert len(selected.requests) == 1
    assert selected.requests[0].operation == "healthcheck"
    assert selected.closed is True


def test_check_llm_reports_missing_mistral_key_without_traceback_or_secret(
    tmp_path: Path, capsys
) -> None:
    result = main.cli(
        [
            "--env-file",
            str(
                write_env(
                    tmp_path,
                    LLM_PROVIDER="mistral",
                    LLM_MODEL="mistral-small-latest",
                    MISTRAL_API_KEY="",
                )
            ),
            "--profile",
            str(write_profile(tmp_path)),
            "--check-llm",
        ]
    )

    output = capsys.readouterr()
    assert result == 2
    assert "MISTRAL_API_KEY is required" in output.err
    assert "Traceback" not in output.err
    assert "test-token" not in output.err
