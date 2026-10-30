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


CONTACT_KEYS = {"contact", "contact_in", "name", "phone", "email", "street", "address", "city"}


def redact_payload(value):
    """What an agent trace may contain: contact fields dropped, every string PII-redacted (graph state, node
    inputs and outputs). Used as the LangSmith client's hide_inputs / hide_outputs."""
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if k in CONTACT_KEYS and v else redact_payload(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_payload(v) for v in value]
    if isinstance(value, str):
        return redact(value)
    return value


def agent_callbacks(settings: Settings) -> list | None:
    """A LangSmith tracer for the agent graph whose client redacts every run before upload (LangGraph traces each
    node with the full state, which holds the customer's contact details). Passing it also stops LangChain from
    adding its own unredacted tracer from the environment."""
    if not tracing_enabled(settings):
        return None
    from langchain_core.tracers import LangChainTracer
    from langsmith import Client
    client = Client(api_key=settings.langsmith_api_key.get_secret_value(), hide_inputs=redact_payload,
                    hide_outputs=redact_payload)
    return [LangChainTracer(project_name=settings.langsmith_project, client=client)]


def trace_ask(settings: Settings, ask):
    """Wrap Assistant.ask so each customer turn becomes one LangSmith trace."""
    if not tracing_enabled(settings):
        return ask
    from langsmith import traceable
    return traceable(name="serviceiq.ask", run_type="chain", process_inputs=_redact_inputs,
                     process_outputs=_summarize_output)(ask)
