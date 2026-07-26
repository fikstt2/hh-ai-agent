from __future__ import annotations

import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

import aiohttp

from config import Settings


logger = logging.getLogger(__name__)
Requester = Callable[[dict], Awaitable[str]]


class ModelResponseError(ValueError):
    pass


@dataclass(frozen=True)
class SuitabilityResult:
    suitable: bool
    confidence: float
    reason: str


def parse_suitability(raw: str) -> SuitabilityResult:
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ModelResponseError("response is not valid JSON") from exc
    if not isinstance(data, dict) or set(data) != {"suitable", "confidence", "reason"}:
        raise ModelResponseError("response must contain suitable, confidence and reason")
    if type(data["suitable"]) is not bool:
        raise ModelResponseError("suitable must be a boolean")
    confidence = data["confidence"]
    if type(confidence) not in {int, float} or not math.isfinite(confidence):
        raise ModelResponseError("confidence must be a finite number")
    if not 0 <= confidence <= 1:
        raise ModelResponseError("confidence must be between 0 and 1")
    reason = data["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise ModelResponseError("reason must be a non-empty string")
    return SuitabilityResult(data["suitable"], float(confidence), reason.strip())


class OllamaAnalyzer:
    def __init__(self, settings: Settings, requester: Requester | None = None):
        self.settings = settings
        self._requester = requester or self._request_ollama

    async def _request_ollama(self, payload: dict) -> str:
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(self.settings.ollama_url, json=payload) as response:
                response.raise_for_status()
                try:
                    data = await response.json()
                except ValueError as exc:
                    raise ModelResponseError("Ollama returned invalid JSON") from exc
        if not isinstance(data, dict):
            raise ModelResponseError("Ollama response must be a JSON object")
        text = data.get("response")
        if not isinstance(text, str):
            raise ModelResponseError("Ollama response field must be a string")
        return text

    def _profile_json(self) -> str:
        return json.dumps(
            asdict(self.settings.profile.candidate), ensure_ascii=False, indent=2
        )

    async def assess(self, vacancy_title: str, vacancy_description: str) -> SuitabilityResult:
        prompt = f"""Evaluate whether this vacancy fits the candidate profile.
Return only one JSON object with exactly these fields:
{{"suitable": true, "confidence": 0.82, "reason": "Short explanation"}}

Candidate profile (the only source of candidate facts):
{self._profile_json()}

Vacancy title:
{vacancy_title}

Vacancy description:
{vacancy_description}

Do not infer missing experience, skills, education, projects, names, or links.
Confidence must be a number from 0 to 1.
"""
        payload = {
            "model": self.settings.ollama_model,
            "prompt": prompt,
            "stream": False,
            "format": "json",
        }
        for attempt in range(2):
            try:
                return parse_suitability(await self._requester(payload))
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                logger.warning("llm_invalid_response attempt=%s error=%s", attempt + 1, exc)
        return SuitabilityResult(False, 0.0, "Invalid model response")

    async def generate_cover_letter(
        self, vacancy_title: str, vacancy_description: str
    ) -> str:
        cover = self.settings.profile.cover_letter
        prompt = f"""Write a cover letter in {cover.language} using a {cover.style} style.

Candidate profile (the only source of candidate facts):
{self._profile_json()}

Vacancy title:
{vacancy_title}

Vacancy description:
{vacancy_description}

Do not invent experience, projects, education, technologies, names, links,
achievements, availability, or promises that are absent from the profile.
Use only relevant profile facts. Return only the letter text. The hard limit is
{cover.max_length} characters.
"""
        payload = {
            "model": self.settings.ollama_model,
            "prompt": prompt,
            "stream": False,
        }
        for attempt in range(2):
            try:
                letter = (await self._requester(payload)).strip()
                if not letter:
                    raise ModelResponseError("cover letter is empty")
                return letter[: cover.max_length]
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                logger.warning("llm_letter_failed attempt=%s error=%s", attempt + 1, exc)
        return ""
