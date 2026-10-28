"""Small HTTP client for the ServiceIQ API, used by the Streamlit chat (and usable by tests or scripts).

When the API is a private Cloud Run service, set CHAT_API_ID_TOKEN=true: each request then carries a Google
identity token for the API's URL, fetched from the Cloud Run metadata server (no key files involved).
"""
import threading
import time
from types import SimpleNamespace

import httpx

TOKEN_LIFETIME = 50 * 60  # Google ID tokens last 60 minutes; refresh a little early


class _IdTokenAuth(httpx.Auth):
    def __init__(self, audience: str):
        self.audience, self.token, self.expires, self.lock = audience, None, 0.0, threading.Lock()

    def auth_flow(self, request):
        with self.lock:
            if time.monotonic() > self.expires:
                import google.auth.transport.requests
                from google.oauth2 import id_token
                self.token = id_token.fetch_id_token(google.auth.transport.requests.Request(), self.audience)
                self.expires = time.monotonic() + TOKEN_LIFETIME
        request.headers["Authorization"] = f"Bearer {self.token}"
        yield request


class ServiceIQClient:
    def __init__(self, base_url: str, widget_key: str, timeout: float = 30.0, id_token: bool = False):
        base_url = base_url.rstrip("/")
        self.http = httpx.Client(base_url=base_url, timeout=timeout, headers={"X-Widget-Key": widget_key},
                                 auth=_IdTokenAuth(base_url) if id_token else None)

    def chat(self, message: str, conversation_id: str | None = None, contact: dict | None = None,
             confirm: bool | None = None) -> SimpleNamespace:
        body = {"message": message, "conversation_id": conversation_id}
        body |= ({"contact": contact} if contact else {}) | ({"confirm": confirm} if confirm is not None else {})
        r = self.http.post("/v1/chat", json=body)
        r.raise_for_status()
        data = r.json()
        # same shape the chat UI renders for direct (in-process) answers
        return SimpleNamespace(
            status=data["status"], answer=data["answer"], trace_id=data["trace_id"],
            conversation_id=data["conversation_id"], message_id=data["message_id"],
            citations=[SimpleNamespace(**c) for c in data["citations"]],
            escalation=data["escalation"], cost_usd=0.0, latency_ms={"total": data["latency_ms"]}, debug={},
            stage=data.get("stage"), needs_contact=data.get("needs_contact", False),
            service_request=data.get("service_request"))

    def callback(self, conversation_id: str, message_id: str | None, name: str, phone: str,
                 preferred_time: str | None = None) -> dict:
        r = self.http.post("/v1/escalations", json={"conversation_id": conversation_id, "message_id": message_id,
                                                    "name": name, "phone": phone, "preferred_time": preferred_time})
        r.raise_for_status()
        return r.json()

    def feedback(self, message_id: str, rating: str) -> None:
        self.http.post("/v1/feedback", json={"message_id": message_id, "rating": rating}).raise_for_status()
