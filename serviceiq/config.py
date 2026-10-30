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

    # API service and persistence (M11)
    database_url: str = f"sqlite:///{(PROJECT_ROOT / 'var' / 'serviceiq.db').as_posix()}"
    cors_origins: list[str] = ["http://localhost:8501"]
    rate_limit_per_minute: int = 30   # per widget key
    retention_days: int = 90          # conversation logs are purged after this many days
    history_turns: int = 3            # earlier turns used to understand follow-ups
    chat_api_url: str = ""            # Streamlit chat: call this API (e.g. http://127.0.0.1:8000); empty = in-process
    chat_api_id_token: bool = False   # attach a Google ID token (needed when the API is a private Cloud Run service)
    chat_widget_keys: str = ""        # JSON {"tenant": "key"} for the chat in API mode; else var/dev_widget_keys.json
    admin_passwords: SecretStr | None = None  # JSON {"username": "password"} supplied by init-db in the cloud
    uploads_bucket: str = ""          # Cloud Storage bucket for uploaded documents; empty = local var/uploads

    # Phase 2-3 agent (M14+): intake and operations through one LangGraph agent
    agent_enabled: bool = True        # false = MVP behaviour (questions only)
    intake_max_no_progress: int = 2   # turns without new details before handing off to a person
    agent_booking_enabled: bool = True  # Phase 3: offer times and book; false = Phase 2 (the office books)
    agent_tool_allowlist: str = ""    # JSON {"tenant": ["tool", ...]}; tenants not listed may use every tool
    agent_bookings_per_minute: int = 30  # per tenant
    booking_days_ahead: int = 14      # how far ahead to look for free times
    # Field-service platform: its URL (empty = run it in-process on fsp_database_url, for local dev and tests)
    fsp_url: str = ""
    fsp_api_keys: str = ""            # JSON {"tenant": "key"}; else var/dev_fsp_keys.json
    fsp_database_url: str = f"sqlite:///{(PROJECT_ROOT / 'var' / 'fieldservice.db').as_posix()}"
    fsp_id_token: bool = False        # attach a Google ID token (private Cloud Run service)
    fsp_webhook_secret: SecretStr | None = None  # HMAC key for the platform's job.completed events (/v1/events/fieldservice)

    # Phase 4 (M19+): invoice, payment and review after the job is completed
    agent_payments_enabled: bool = True  # false = record the completion, the office bills by hand
    stripe_secret_key: SecretStr | None = None      # empty = an in-memory payment page (local dev and evaluation)
    stripe_webhook_secret: SecretStr | None = None  # verifies /v1/payments/stripe
    payment_reminder_days: int = 3    # one reminder after this many days unpaid, a ticket after twice that
    review_reminder_days: int = 7     # one reminder, then the review request is closed after twice that
    public_base_url: str = "http://localhost:8501"  # customer chat: payment and review links come back here

    # Email notifications for escalations; without SMTP_HOST, emails are written to var/outbox/
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: SecretStr | None = None
    escalation_from_email: str = "serviceiq@localhost"
    outbox_dir: Path = PROJECT_ROOT / "var" / "outbox"

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
