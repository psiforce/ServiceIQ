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

    # Chat models (answer model chosen by evaluation in M10)
    guard_model: str = "gpt-4.1-mini"
    answer_model: str = "gpt-5.4-mini"  # chosen in M10: best quality on the eval set at $0.0023/answer
    judge_model: str = "gpt-4.1"  # evaluation grader: stronger than the model it grades

    # Pinecone
    pinecone_api_key: SecretStr
    pinecone_index_name: str = "serviceiq-dev"
    # Free Starter plan supports serverless on AWS us-east-1; switch to gcp/us-central1 on a paid plan
    pinecone_cloud: str = "aws"
    pinecone_region: str = "us-east-1"
    embedding_dimension: int = 1536  # text-embedding-3-small

    # LangSmith (optional until Sprint 2)
    langsmith_api_key: SecretStr | None = None
    langsmith_tracing: bool = False
    langsmith_project: str = "serviceiq-dev"

    # Chunking (requirements: ~500 tokens with 75-token overlap; tuned in M10)
    chunk_target_tokens: int = 500
    chunk_overlap_tokens: int = 75

    # Retrieval -> evidence selection (tuned in M10)
    retrieval_top_k: int = 20        # candidates fetched from Pinecone (M10: 20 + rerank beat 10)
    evidence_k: int = 6              # chunks given to the answer model (M10: 6 with max 2 per document)
    max_chunks_per_doc: int = 2      # 0 = no cap; 2 keeps evidence diverse without dropping a second section (M10)
    # "none" or a Pinecone-hosted reranker. M10: bge-reranker-v2-m3 gave +10 pts Recall@5, but the free plan allows
    # only 500 rerank requests/month; off by decision (2026-10-27) until a paid plan or self-hosted reranker exists.
    reranker: str = "none"

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
