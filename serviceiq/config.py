"""Application settings, read from environment variables and the project .env file.

Secrets are held as SecretStr so they never appear in logs, reprs or tracebacks.
"""
from datetime import date
from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    """Raised when required settings are missing or invalid."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    # OpenAI
    openai_api_key: SecretStr
    openai_embedding_model: str = "text-embedding-3-small"

    # Pinecone
    pinecone_api_key: SecretStr
    pinecone_index_name: str = "serviceiq-dev"

    # LangSmith (optional until Sprint 2)
    langsmith_api_key: SecretStr | None = None
    langsmith_tracing: bool = False
    langsmith_project: str = "serviceiq-dev"

    # Clock used for effective/expiry filtering; pinned so evaluations are repeatable
    serviceiq_as_of_date: date = date(2026, 10, 26)

    data_dir: Path = PROJECT_ROOT / "data"


def load_settings(**overrides) -> Settings:
    """Build Settings, turning validation errors into a readable message without secret values."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            name = str(err["loc"][0]).upper()
            problems.append(f"{name} is missing" if err["type"] == "missing" else f"{name}: {err['msg']}")
        raise ConfigError("Invalid configuration (check your .env file): " + "; ".join(problems)) from None


@lru_cache
def get_settings() -> Settings:
    return load_settings()
