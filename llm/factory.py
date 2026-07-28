from config import Settings
from database import Database
from llm.errors import LLMConfigurationError
from llm.managed import ManagedLLMProvider
from llm.providers.mistral import MistralProvider
from llm.providers.ollama import OllamaProvider
from llm.providers.openai_compatible import OpenAICompatibleProvider


def create_llm_provider(settings: Settings, database: Database) -> ManagedLLMProvider:
    config = settings.llm
    if config.provider == "ollama":
        adapter = OllamaProvider(config.ollama_url)
    elif config.provider == "mistral":
        adapter = MistralProvider(config.mistral_api_key, config.mistral_base_url)
    elif config.provider == "openai_compatible":
        adapter = OpenAICompatibleProvider(
            config.openai_compatible_base_url,
            config.openai_compatible_api_key,
            json_mode=config.openai_compatible_json_mode,
        )
    else:
        raise LLMConfigurationError()
    return ManagedLLMProvider(
        adapter,
        database,
        max_retries=config.max_retries,
        max_requests_per_day=config.max_requests_per_day,
    )
