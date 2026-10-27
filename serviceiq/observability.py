"""LangSmith tracing. Enabled with LANGSMITH_TRACING=true; only PII-redacted text is sent."""
import os
from dataclasses import asdict

from serviceiq.config import Settings
from serviceiq.guardrails.pii import redact


def tracing_enabled(settings: Settings) -> bool:
    return bool(getattr(settings, "langsmith_tracing", False) and getattr(settings, "langsmith_api_key", None))


def configure(settings: Settings) -> None:
    """LangSmith reads its configuration from environment variables."""
    if tracing_enabled(settings):
        os.environ["LANGSMITH_TRACING"] = "true"
        os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key.get_secret_value()
        os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project


def wrap_client(settings: Settings, client):
    """Trace each OpenAI call (prompts contain only redacted customer text)."""
    if not tracing_enabled(settings):
        return client
    from langsmith.wrappers import wrap_openai
    return wrap_openai(client)


def _redact_inputs(inputs: dict) -> dict:
    history = inputs.get("history") or []
    return {"question": redact(inputs.get("question", "")), "tenant_id": inputs.get("tenant_id"),
            "history": [(redact(q), redact(a)) for q, a in history]}


def _summarize_output(output) -> dict:
    data = asdict(output) if hasattr(output, "__dataclass_fields__") else {"output": output}
    data.pop("context", None)
    for citation in data.get("citations", []):
        citation.pop("text", None)
    return data


def trace_ask(settings: Settings, ask):
    """Wrap Assistant.ask so each customer turn becomes one LangSmith trace."""
    if not tracing_enabled(settings):
        return ask
    from langsmith import traceable
    return traceable(name="serviceiq.ask", run_type="chain", process_inputs=_redact_inputs,
                     process_outputs=_summarize_output)(ask)
