from dataclasses import replace
from pathlib import Path

import pytest

from config import load_settings
from database import Database
from llm.errors import LLMConfigurationError
from llm.factory import create_llm_provider
from llm.providers.mistral import MistralProvider
from llm.providers.ollama import OllamaProvider
from llm.providers.openai_compatible import OpenAICompatibleProvider
from tests.test_config import VALID_ENV, write_profile


def configured(tmp_path: Path, **overrides: str):
    return load_settings(
        profile_path=write_profile(tmp_path),
        environ={**VALID_ENV, **overrides},
    )


@pytest.mark.parametrize(
    ("overrides", "adapter_type"),
    [
        ({}, OllamaProvider),
        (
            {
                "LLM_PROVIDER": "mistral",
                "LLM_MODEL": "mistral-small-latest",
                "MISTRAL_API_KEY": "mistral-key",
            },
            MistralProvider,
        ),
        (
            {
                "LLM_PROVIDER": "openai_compatible",
                "LLM_MODEL": "custom-model",
                "OPENAI_COMPATIBLE_BASE_URL": "https://provider.example/v1",
                "OPENAI_COMPATIBLE_API_KEY": "compatible-key",
            },
            OpenAICompatibleProvider,
        ),
    ],
)
def test_factory_builds_exactly_one_selected_adapter(
    tmp_path: Path, overrides: dict[str, str], adapter_type: type
) -> None:
    settings = configured(tmp_path, **overrides)
    database = Database(tmp_path / "agent.db")
    database.init()

    provider = create_llm_provider(settings, database)

    assert isinstance(provider.adapter, adapter_type)
    assert not hasattr(provider, "fallbacks")


def test_factory_rejects_unknown_provider_even_if_validation_is_bypassed(
    tmp_path: Path,
) -> None:
    settings = configured(tmp_path)
    settings = replace(settings, llm=replace(settings.llm, provider="unknown"))
    database = Database(tmp_path / "agent.db")
    database.init()

    with pytest.raises(LLMConfigurationError):
        create_llm_provider(settings, database)
