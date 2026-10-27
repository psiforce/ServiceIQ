"""Small HTTP client for the ServiceIQ API, used by the Streamlit chat (and usable by tests or scripts)."""
from types import SimpleNamespace

import httpx


class ServiceIQClient:
    def __init__(self, base_url: str, widget_key: str, timeout: float = 30.0):
        self.http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                                 headers={"X-Widget-Key": widget_key})

    def chat(self, message: str, conversation_id: str | None = None) -> SimpleNamespace:
        r = self.http.post("/v1/chat", json={"message": message, "conversation_id": conversation_id})
        r.raise_for_status()
        data = r.json()
        # same shape the chat UI renders for direct (in-process) answers
        return SimpleNamespace(
            status=data["status"], answer=data["answer"], trace_id=data["trace_id"],
            conversation_id=data["conversation_id"], message_id=data["message_id"],
            citations=[SimpleNamespace(**c) for c in data["citations"]],
            escalation=data["escalation"], cost_usd=0.0, latency_ms={"total": data["latency_ms"]}, debug={})

    def callback(self, conversation_id: str, message_id: str | None, name: str, phone: str,
                 preferred_time: str | None = None) -> dict:
        r = self.http.post("/v1/escalations", json={"conversation_id": conversation_id, "message_id": message_id,
                                                    "name": name, "phone": phone, "preferred_time": preferred_time})
        r.raise_for_status()
        return r.json()

    def feedback(self, message_id: str, rating: str) -> None:
        self.http.post("/v1/feedback", json={"message_id": message_id, "rating": rating}).raise_for_status()
