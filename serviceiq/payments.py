"""Payment provider (Phase 4): a hosted payment page per invoice, and its signed "paid" events.

Card and bank details are entered on the provider's page (Stripe Checkout) and never reach ServiceIQ, so the chat
guard's rule "payments are never taken here" stays true. Every page is created with the invoice as idempotency key,
so a retried request can't create a second one. Without STRIPE_SECRET_KEY an in-memory provider stands in (local
dev, tests and evaluation): same interface, no network.
"""
import uuid
from dataclasses import dataclass
from typing import Protocol

from serviceiq.config import Settings

# Stripe payment-method type -> the method stored on the invoice
METHODS = {"card": "card", "us_bank_account": "ach", "cashapp": "cashapp"}


@dataclass
class PaymentEvent:
    """A provider event, reduced to what the agent needs."""
    event_id: str
    kind: str                 # paid | processing | failed
    invoice_id: str | None
    session_id: str | None
    method: str | None = None  # card | ach | cashapp | online (when the provider doesn't say)


class PaymentProvider(Protocol):
    def create_page(self, invoice, description: str, methods: list[str], email: str | None, success_url: str,
                    cancel_url: str, idempotency_key: str) -> tuple[str, str]:
        """(session id, payment URL) for a hosted payment page."""
        ...

    def parse_event(self, body: bytes, signature: str) -> PaymentEvent | None:
        """Verify a webhook and reduce it to a PaymentEvent (None for event types the agent ignores).
        Raises ValueError when the signature is wrong."""
        ...


class StripeProvider:
    def __init__(self, secret_key: str, webhook_secret: str | None):
        import stripe
        self.stripe, self.webhook_secret = stripe, webhook_secret
        self.client = stripe.StripeClient(secret_key)

    def create_page(self, invoice, description, methods, email, success_url, cancel_url, idempotency_key):
        params = {
            "mode": "payment", "payment_method_types": methods,
            "line_items": [{"quantity": 1, "price_data": {
                "currency": invoice.currency, "unit_amount": invoice.amount_cents,
                "product_data": {"name": description}}}],
            "client_reference_id": invoice.invoice_id,
            "metadata": {"invoice_id": invoice.invoice_id, "tenant_id": invoice.tenant_id,
                         "request_id": invoice.request_id},
            "success_url": success_url, "cancel_url": cancel_url,
        }
        if email:
            params["customer_email"] = email
        page = self.client.v1.checkout.sessions.create(params=params, options={"idempotency_key": idempotency_key})
        return page.id, page.url

    def parse_event(self, body: bytes, signature: str) -> PaymentEvent | None:
        if not self.webhook_secret:
            raise ValueError("STRIPE_WEBHOOK_SECRET is not set")
        try:
            event = self.stripe.Webhook.construct_event(body, signature, self.webhook_secret)
        except self.stripe.SignatureVerificationError as exc:
            raise ValueError("invalid Stripe signature") from exc
        page = event["data"]["object"]
        kind = {"checkout.session.async_payment_succeeded": "paid",
                "checkout.session.async_payment_failed": "failed"}.get(event["type"])
        if event["type"] == "checkout.session.completed":  # ACH completes the page before the money arrives
            kind = "paid" if page.get("payment_status") == "paid" else "processing"
        if kind is None:
            return None
        return PaymentEvent(event_id=event["id"], kind=kind, session_id=page.get("id"),
                            invoice_id=(page.get("metadata") or {}).get("invoice_id") or page.get("client_reference_id"),
                            method=self._method(page.get("payment_intent")))

    def _method(self, payment_intent_id: str | None) -> str:
        """Which method the customer chose (one extra API call; 'online' if Stripe can't say)."""
        if not payment_intent_id:
            return "online"
        try:
            intent = self.client.v1.payment_intents.retrieve(payment_intent_id, params={"expand": ["payment_method"]})
            return METHODS.get(intent.payment_method.type, "online")
        except Exception:
            return "online"


class MemoryProvider:
    """In-memory stand-in: pages live in a dict, and `event()` builds the webhook a test or evaluation would get."""

    def __init__(self, base_url: str = "http://localhost:8501"):
        self.base_url, self.pages = base_url.rstrip("/"), {}

    def create_page(self, invoice, description, methods, email, success_url, cancel_url, idempotency_key):
        for session_id, page in self.pages.items():
            if page["key"] == idempotency_key:
                return session_id, page["url"]
        session_id = f"cs_test_{uuid.uuid4().hex[:16]}"
        url = f"{self.base_url}/pay/{session_id}"
        self.pages[session_id] = {"key": idempotency_key, "url": url, "invoice_id": invoice.invoice_id,
                                  "amount_cents": invoice.amount_cents, "methods": list(methods), "email": email}
        return session_id, url

    def event(self, session_id: str, kind: str = "paid", method: str = "card", event_id: str | None = None) -> PaymentEvent:
        return PaymentEvent(event_id=event_id or f"evt_{uuid.uuid4().hex[:12]}", kind=kind, session_id=session_id,
                            invoice_id=self.pages[session_id]["invoice_id"], method=method)

    def parse_event(self, body: bytes, signature: str) -> PaymentEvent | None:
        # Nothing can be verified without a real provider, so the webhook endpoint accepts nothing: otherwise anyone
        # could mark an invoice paid. Tests and evaluation hand `event()` results to the agent directly.
        raise ValueError("no payment provider is configured (set STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET)")


def make_payment_provider(settings: Settings) -> PaymentProvider:
    if settings.stripe_secret_key and settings.stripe_secret_key.get_secret_value():
        secret = settings.stripe_webhook_secret.get_secret_value() if settings.stripe_webhook_secret else None
        return StripeProvider(settings.stripe_secret_key.get_secret_value(), secret)
    return MemoryProvider(settings.public_base_url)
