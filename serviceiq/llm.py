"""Shared OpenAI chat helpers: client, structured (JSON-schema) calls and cost accounting."""
import json
from dataclasses import dataclass
from functools import lru_cache

from openai import BadRequestError, OpenAI

from serviceiq.config import Settings

# USD per 1M tokens (input, output) - OpenAI list prices; verify on the pricing page before relying on them
PRICES = {
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1-nano": (0.10, 0.40),
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-5.4-mini": (0.75, 4.50),  # from third-party aggregators citing OpenAI (Oct 2026); verify on OpenAI's page
}


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def cost(self) -> float | None:
        price = PRICES.get(self.model)
        if price is None:
            return None
        return (self.input_tokens * price[0] + self.output_tokens * price[1]) / 1_000_000

    def add(self, other: "Usage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens


@lru_cache
def _client(api_key: str) -> OpenAI:
    return OpenAI(api_key=api_key, max_retries=3, timeout=30)


def openai_client(settings: Settings) -> OpenAI:
    return _client(settings.openai_api_key.get_secret_value())


_NO_TEMPERATURE: set[str] = set()  # models that reject temperature=0 (e.g. reasoning models)


def chat_json(client: OpenAI, model: str, system: str, user: str, schema: dict, name: str) -> tuple[dict, Usage]:
    """One chat call whose reply is guaranteed to match `schema` (strict structured output)."""
    request = dict(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}},
    )
    if model not in _NO_TEMPERATURE:
        request["temperature"] = 0
    try:
        response = client.chat.completions.create(**request)
    except BadRequestError as exc:
        if "temperature" not in str(exc) or "temperature" not in request:
            raise
        _NO_TEMPERATURE.add(model)  # remember, and retry with the model's default sampling
        request.pop("temperature")
        response = client.chat.completions.create(**request)
    usage = Usage(model, response.usage.prompt_tokens, response.usage.completion_tokens)
    return json.loads(response.choices[0].message.content), usage
