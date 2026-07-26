import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from ai_analyzer import (
    ModelResponseError,
    OllamaAnalyzer,
    SuitabilityResult,
    parse_suitability,
)
from config import load_settings
from tests.test_config import VALID_ENV, VALID_PROFILE, write_profile


def settings(tmp_path: Path, max_length: int = 1800):
    profile_path = write_profile(tmp_path, VALID_PROFILE)
    loaded = load_settings(profile_path=profile_path, environ=VALID_ENV)
    return replace(
        loaded,
        profile=replace(
            loaded.profile,
            cover_letter=replace(loaded.profile.cover_letter, max_length=max_length),
        ),
    )


def test_parse_valid_suitability_json() -> None:
    result = parse_suitability(
        '{"suitable": true, "confidence": 0.82, "reason": "Relevant backend work"}'
    )

    assert result == SuitabilityResult(True, 0.82, "Relevant backend work")


@pytest.mark.parametrize(
    "raw",
    [
        "NOT YES",
        "{bad json}",
        '{"suitable": true, "confidence": 0.8}',
        '{"suitable": true, "confidence": 0.8, "reason": "ok", "extra": 1}',
        '{"suitable": "true", "confidence": 0.8, "reason": "ok"}',
        '{"suitable": true, "confidence": true, "reason": "ok"}',
        '{"suitable": true, "confidence": -0.1, "reason": "ok"}',
        '{"suitable": true, "confidence": 1.1, "reason": "ok"}',
        '{"suitable": true, "confidence": 0.8, "reason": ""}',
    ],
)
def test_parse_rejects_unstructured_or_invalid_responses(raw: str) -> None:
    with pytest.raises(ModelResponseError):
        parse_suitability(raw)


def test_assess_retries_once_then_fails_closed(tmp_path: Path) -> None:
    calls: list[dict] = []

    async def requester(payload: dict) -> str:
        calls.append(payload)
        return "NOT YES"

    analyzer = OllamaAnalyzer(settings(tmp_path), requester=requester)
    result = asyncio.run(analyzer.assess("Developer", "Description"))

    assert result == SuitabilityResult(False, 0.0, "Invalid model response")
    assert len(calls) == 2
    assert all(call["format"] == "json" for call in calls)


def test_assess_accepts_valid_second_response(tmp_path: Path) -> None:
    responses = iter(
        [
            "invalid",
            '{"suitable": false, "confidence": 0.25, "reason": "Stack mismatch"}',
        ]
    )

    async def requester(payload: dict) -> str:
        return next(responses)

    result = asyncio.run(
        OllamaAnalyzer(settings(tmp_path), requester=requester).assess(
            "Developer", "Description"
        )
    )

    assert result == SuitabilityResult(False, 0.25, "Stack mismatch")


def test_malformed_ollama_envelope_retries_once_and_fails_closed(tmp_path: Path) -> None:
    calls = 0

    async def requester(payload: dict) -> str:
        nonlocal calls
        calls += 1
        raise json.JSONDecodeError("invalid envelope", "<html>", 0)

    result = asyncio.run(
        OllamaAnalyzer(settings(tmp_path), requester=requester).assess(
            "Developer", "Description"
        )
    )

    assert result == SuitabilityResult(False, 0.0, "Invalid model response")
    assert calls == 2


def test_cover_letter_prompt_uses_local_profile_and_enforces_length(
    tmp_path: Path,
) -> None:
    calls: list[dict] = []

    async def requester(payload: dict) -> str:
        calls.append(payload)
        return "x" * 30

    letter = asyncio.run(
        OllamaAnalyzer(settings(tmp_path, max_length=10), requester=requester).generate_cover_letter(
            "Backend role", "Build a service"
        )
    )

    prompt = calls[0]["prompt"]
    assert letter == "x" * 10
    assert "Test Candidate" in prompt
    assert "Built internal Python services." in prompt
    assert "Backend role" in prompt
    assert "Build a service" in prompt
    assert "Do not invent" in prompt
    assert "format" not in calls[0]


def test_empty_cover_letter_retries_once_and_returns_empty(tmp_path: Path) -> None:
    calls = 0

    async def requester(payload: dict) -> str:
        nonlocal calls
        calls += 1
        return "   "

    letter = asyncio.run(
        OllamaAnalyzer(settings(tmp_path), requester=requester).generate_cover_letter(
            "Backend role", "Build a service"
        )
    )

    assert letter == ""
    assert calls == 2
