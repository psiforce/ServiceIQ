"""M0: configuration loading."""
from datetime import date

import pytest

from serviceiq.config import ConfigError, load_settings

KEYS = ["OPENAI_API_KEY", "PINECONE_API_KEY", "LANGSMITH_API_KEY", "SERVICEIQ_AS_OF_DATE"]


@pytest.fixture
def clean_env(monkeypatch):
    for key in KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_loads_from_env_file(clean_env, tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-test\nPINECONE_API_KEY=pc-test\n", encoding="utf-8")
    settings = load_settings(_env_file=env)
    assert settings.openai_api_key.get_secret_value() == "sk-test"
    assert settings.openai_embedding_model == "text-embedding-3-small"
    assert settings.serviceiq_as_of_date == date(2026, 10, 26)
    assert settings.langsmith_api_key is None


def test_missing_keys_give_clear_error(clean_env, tmp_path):
    empty = tmp_path / ".env"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ConfigError) as exc:
        load_settings(_env_file=empty)
    assert "OPENAI_API_KEY is missing" in str(exc.value)
    assert "PINECONE_API_KEY is missing" in str(exc.value)


def test_secrets_are_not_exposed_in_repr(clean_env, tmp_path):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=sk-very-secret\nPINECONE_API_KEY=pc-very-secret\n", encoding="utf-8")
    settings = load_settings(_env_file=env)
    assert "very-secret" not in repr(settings)
