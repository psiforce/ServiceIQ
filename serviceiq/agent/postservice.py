"""Post-service operations (Phase 4): invoice, payment and review once the technician has completed the job.

Events drive these steps, not customer messages, but they belong to the same agent and the same conversation:

  job.completed (scheduling-service webhook, or the hourly sweep)  -> on_job_completed
      price inside the published range        -> invoice + secure payment link emailed     (stage awaiting_payment)
      outside it, or no published price        -> Approval row, admins emailed; billed once approved
  payment event (Stripe webhook) or an admin's "Mark paid" (Zelle, check, cash)  -> on_payment / record_manual_payment
      processing (ACH)  -> wait
      failed            -> one new link, then a ticket
      paid              -> receipt, then a review request                                     (stage review_requested)
  rating submitted (chat form -> /v1/reviews)  -> record_review
      every reviewer sees the public review link; a rating of 3 or less also opens a recovery ticket
                                                                                              (stage reviewed | recovery)

Money rules are code (policy.check_invoice); no language model is involved. Every write is an audited tool call
with an idempotency key, the invoice table allows one invoice per request, and a repeated event changes nothing.
Card and bank details are entered on the provider's page and never reach ServiceIQ.
"""
from datetime import datetime, timezone
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from serviceiq.agent import operations, policy
from serviceiq.agent.tools import audited
from serviceiq.escalation import notify, open_ticket, send
from serviceiq.guardrails.pii import redact
from serviceiq.payments import PaymentEvent, PaymentProvider, make_payment_provider
from serviceiq.storage.models import Approval, Conversation, Invoice, Review, ServiceRequest, ToolCall
from serviceiq.tenants import TenantProfile, load_profile

MANUAL_METHODS = ("zelle", "check", "cash")
METHOD_NAMES = {"card": "card, Apple Pay or Google Pay", "us_bank_account": "bank transfer (ACH)",
                "cashapp": "Cash App Pay"}
LOW_RATING = 3  # this rating or lower opens a recovery ticket


def _now() -> datetime:
    return datetime.now(timezone.utc)


def dollars(cents: int) -> str:
    return f"${cents / 100:,.2f}"


class PostService:
    """Mixed into Agent: needs self.settings, self.fsp, self.graph and self._payments."""

    @property
    def payments(self) -> PaymentProvider:
        if self._payments is None:
            self._payments = make_payment_provider(self.settings)
        return self._payments

    # ------------------------------------------------------------ helpers
    def _move(self, conversation_id: str | None, stage: str) -> None:
        """Move the customer's conversation to `stage` (the thread may be gone after a retention purge)."""
        if not conversation_id:
            return
        try:
            self.graph.update_state({"configurable": {"thread_id": conversation_id}}, {"stage": stage})
        except Exception:
            pass

    def _customer_email(self, tenant_id: str, request: ServiceRequest) -> str | None:
        try:
            record = self.fsp.get_customer(tenant_id, request.customer_ref) or {}
        except Exception:  # the platform being down must not stop billing: the office is told instead
            return None
        return (record.get("customer") or {}).get("email") or None

    def _ticket(self, session: Session, request: ServiceRequest, priority: str, reason: str) -> str | None:
        conversation = session.get(Conversation, request.conversation_id) if request.conversation_id else None
        if conversation is None:
            return None
        ticket = open_ticket(session, request.tenant_id, conversation, None, priority, reason)
        notify(self.settings, load_profile(self.settings.data_dir, request.tenant_id), ticket)
        return ticket.escalation_id

    def _link(self, **params) -> str:
        query = "&".join(f"{k}={v}" for k, v in params.items())
        return f"{self.settings.public_base_url.rstrip('/')}/?{query}"

    @staticmethod
    def _seen(session: Session, key: str) -> bool:
        """Has this event already been handled? (provider events are retried and can arrive twice)"""
        session.flush()
        return session.scalar(select(ToolCall.call_id).where(ToolCall.idempotency_key == key, ToolCall.ok.is_(True))
                              .limit(1)) is not None

    def _allowed_range(self, session: Session, tenant_id: str, request: ServiceRequest, job: dict):
        code = next((t["code"] for t in self.fsp.service_types(tenant_id) if t["id"] == job.get("service_type_id")),
                    None)
        published = operations.published_price(session, self.settings, tenant_id, code) if code else None
        return policy.price_range(published, request.payload.get("trip_fee_usd"))

    # ------------------------------------------------------------ BP-8: job completed -> invoice
    def on_job_completed(self, session: Session, tenant_id: str, job_id: str) -> dict:
        """The technician marked the job completed. The price is read back from the platform, never from the event."""
        request = session.scalar(select(ServiceRequest).where(ServiceRequest.tenant_id == tenant_id,
                                                              ServiceRequest.job_ref == job_id))
        if request is None:
            return {"status": "ignored", "reason": "not a job booked through ServiceIQ"}
        existing = session.scalar(select(Invoice).where(Invoice.request_id == request.request_id))
        if existing is not None:
            return {"status": "duplicate", "invoice_id": existing.invoice_id}
        job = self.fsp.get_job(tenant_id, job_id)
        if not job or job.get("status") != "completed":
            return {"status": "ignored", "reason": "the job is not completed"}
        amount = float(job.get("price") or 0)
        request.status = "completed"
        conv = request.conversation_id
        if not self.settings.agent_payments_enabled or not policy.allowed(self.settings, tenant_id, "create_invoice"):
            ticket_id = self._ticket(session, request, "P3", f"job {job.get('identifier')} completed: bill the "
                                                             f"customer (${amount:,.2f}); automatic invoicing is off")
            return {"status": "manual", "ticket_id": ticket_id}

        allowed_range = self._allowed_range(session, tenant_id, request, job)
        decision = policy.evaluate_invoice(session, tenant_id, amount, allowed_range)

        def create() -> dict:
            invoice = Invoice(tenant_id=tenant_id, request_id=request.request_id, job_ref=job_id,
                              amount_cents=round(amount * 100))
            session.add(invoice)
            session.flush()
            return {"invoice_id": invoice.invoice_id, "amount": amount, "auto": decision.auto,
                    "range": list(allowed_range) if allowed_range else None}
        try:
            created = audited(session, tenant_id, conv, "create_invoice", {"job": job_id, "amount": amount}, create,
                              idempotency_key=f"job:{job_id}")
        except IntegrityError:  # the same completion arrived twice at the same moment (webhook and sweep)
            session.rollback()
            existing = session.scalar(select(Invoice).where(Invoice.request_id == request.request_id))
            return {"status": "duplicate", "invoice_id": existing.invoice_id if existing else None}
        invoice = session.get(Invoice, created["invoice_id"])
        result = {"invoice_id": invoice.invoice_id, "amount": amount}
        profile = load_profile(self.settings.data_dir, tenant_id)
        if decision.auto:
            result |= self._send_invoice(session, invoice, request, profile, allowed_range)
        else:
            approval = Approval(tenant_id=tenant_id, request_id=request.request_id,
                                policy_id=decision.policy_ids[0] if decision.policy_ids else None,
                                reason="; ".join(decision.conditions),
                                proposal={"kind": "invoice", "invoice_id": invoice.invoice_id, "amount": amount,
                                          "range": list(allowed_range) if allowed_range else None,
                                          "job": job.get("identifier"), "service": job.get("service_name"),
                                          "completion_notes": job.get("completion_notes") or ""})
            session.add(approval)
            session.flush()
            send(self.settings, _invoice_approval_email(self.settings, profile, request, approval),
                 f"APPROVAL-{approval.approval_id}")
            result |= {"status": "needs_approval", "approval_id": approval.approval_id}
        self._move(conv, "awaiting_payment")
        return result

    def _send_invoice(self, session: Session, invoice: Invoice, request: ServiceRequest, profile: TenantProfile,
                      allowed_range, approver: str | None = None, note: str = "") -> dict:
        """Hard limits, then a payment page and the invoice email. Raises PolicyViolation."""
        policy.check_invoice(self.settings, invoice.tenant_id, invoice.amount_cents / 100, allowed_range, approver)
        email = self._customer_email(invoice.tenant_id, request)
        reference = request.request_id[:8].upper()
        attempt = f"{invoice.invoice_id}:{invoice.failures}"  # a new page only after a failed payment

        def page() -> dict:
            session_id, url = self.payments.create_page(
                invoice, f"{profile.company_name}: service visit {reference}", list(profile.payment_methods), email,
                success_url=self._link(tenant=invoice.tenant_id, paid=invoice.invoice_id),
                cancel_url=self._link(tenant=invoice.tenant_id), idempotency_key=f"invoice-{attempt}")
            return {"session_id": session_id, "url": url}
        link = audited(session, invoice.tenant_id, request.conversation_id, "send_payment_link",
                       {"invoice": invoice.invoice_id, "amount": invoice.amount_cents / 100,
                        "methods": list(profile.payment_methods)},
                       page, idempotency_key=f"invoice:{attempt}", approver=approver)
        invoice.provider_session_id, invoice.payment_url = link["session_id"], link["url"]
        invoice.status, invoice.sent_at = "sent", _now()
        request.status = "invoiced"
        ticket_id = None
        if email:
            audited(session, invoice.tenant_id, request.conversation_id, "notify_customer",
                    {"invoice": invoice.invoice_id, "channel": "email"},
                    lambda: send(self.settings, invoice_email(self.settings, profile, email, invoice, reference, note),
                                 f"INVOICE-{invoice.invoice_id}-{invoice.failures}"))
        else:  # no address on file: the office passes the link on
            ticket_id = self._ticket(session, request, "P3", f"no email on file: send the customer the payment link "
                                                             f"for invoice {reference} ({dollars(invoice.amount_cents)})")
        return {"status": "sent", "payment_url": invoice.payment_url, "emailed": email, "ticket_id": ticket_id}

    def decide_invoice(self, session: Session, approval: Approval, approver: str, approve: bool,
                       price: float | None = None, note: str = "") -> dict:
        """An admin approves an out-of-range invoice (optionally at an adjusted price) or declines it."""
        invoice = session.get(Invoice, approval.proposal["invoice_id"])
        request = session.get(ServiceRequest, approval.request_id)
        approval.decided_by, approval.decided_at = approver, _now()
        if approve:
            if price is not None:
                invoice.amount_cents = round(price * 100)
            range_ = approval.proposal.get("range")
            result = self._send_invoice(session, invoice, request, load_profile(self.settings.data_dir,
                                                                                 approval.tenant_id),
                                        tuple(range_) if range_ else None, approver=approver)
            approval.status = "approved"
            return {"approved": True} | result
        approval.status, invoice.status = "rejected", "void"
        request.staff_notes = "\n".join(x for x in (request.staff_notes, f"Invoice declined by {approver}: {note}") if x)
        ticket_id = self._ticket(session, request, "P3", "invoice declined by an approver: settle the bill with the "
                                                         "customer" + (f" ({note})" if note else ""))
        self._move(request.conversation_id, "closed")
        return {"approved": False, "ticket_id": ticket_id}

    # ------------------------------------------------------------ BP-9: payment
    def on_payment(self, session: Session, event: PaymentEvent) -> dict:
        """A verified provider event. Replays and events for unknown invoices change nothing."""
        invoice = session.get(Invoice, event.invoice_id) if event.invoice_id else None
        if invoice is None and event.session_id:
            invoice = session.scalar(select(Invoice).where(Invoice.provider_session_id == event.session_id))
        if invoice is None:
            return {"status": "ignored", "reason": "unknown invoice"}
        key = f"event:{event.event_id}"
        if invoice.status in ("paid", "void") or self._seen(session, key):
            return {"status": "duplicate", "invoice_id": invoice.invoice_id}
        request = session.get(ServiceRequest, invoice.request_id)
        if event.kind == "processing":  # ACH: the bank takes a few days
            audited(session, invoice.tenant_id, request.conversation_id, "record_payment",
                    {"invoice": invoice.invoice_id, "kind": "processing"}, lambda: {"status": "processing"},
                    idempotency_key=key)
            invoice.status, invoice.method = "processing", event.method if event.method != "online" else "ach"
            return {"status": "processing", "invoice_id": invoice.invoice_id}
        if event.kind == "failed":
            audited(session, invoice.tenant_id, request.conversation_id, "record_payment",
                    {"invoice": invoice.invoice_id, "kind": "failed"}, lambda: {"status": "failed"}, idempotency_key=key)
            invoice.failures += 1
            invoice.status = "failed"
            if invoice.failures == 1:  # one new link; the customer may try another method
                profile = load_profile(self.settings.data_dir, invoice.tenant_id)
                amount = invoice.amount_cents / 100  # already in range or approved when first sent: same amount
                sent = self._send_invoice(session, invoice, request, profile, (amount, amount),
                                          note="Your last payment didn't go through. You can try again with this "
                                               "new link, using any of the methods below.")
                return {"status": "failed", "retry": sent}
            ticket_id = self._ticket(session, request, "P3", f"payment failed twice for invoice "
                                                             f"{request.request_id[:8].upper()}: call the customer")
            return {"status": "failed", "ticket_id": ticket_id}
        known = event.method if event.method and event.method != "online" else invoice.method  # ACH: set earlier
        return self._mark_paid(session, invoice, request, known or "online", None, key)

    def record_manual_payment(self, session: Session, invoice_id: str, method: str, admin: str) -> dict:
        """An admin records a Zelle, check or cash payment (the agent can't confirm these itself)."""
        if method not in MANUAL_METHODS:
            raise ValueError(f"method must be one of {', '.join(MANUAL_METHODS)}")
        invoice = session.get(Invoice, invoice_id)
        if invoice is None or invoice.status not in ("sent", "failed", "processing"):
            raise ValueError("only an unpaid invoice that was sent can be marked paid")
        request = session.get(ServiceRequest, invoice.request_id)
        return self._mark_paid(session, invoice, request, method, admin, f"manual:{invoice_id}")

    def _mark_paid(self, session: Session, invoice: Invoice, request: ServiceRequest, method: str,
                   recorded_by: str | None, key: str) -> dict:
        audited(session, invoice.tenant_id, request.conversation_id, "record_payment",
                {"invoice": invoice.invoice_id, "kind": "paid", "method": method, "amount": invoice.amount_cents / 100},
                lambda: {"status": "paid"}, idempotency_key=key, approver=recorded_by)
        invoice.status, invoice.method, invoice.paid_at, invoice.recorded_by = "paid", method, _now(), recorded_by
        request.status = "paid"
        profile = load_profile(self.settings.data_dir, invoice.tenant_id)
        email = self._customer_email(invoice.tenant_id, request)
        if email:
            audited(session, invoice.tenant_id, request.conversation_id, "notify_customer",
                    {"invoice": invoice.invoice_id, "channel": "email", "kind": "receipt"},
                    lambda: send(self.settings, receipt_email(self.settings, profile, email, invoice,
                                                              request.request_id[:8].upper()),
                                 f"RECEIPT-{invoice.invoice_id}"))
        self._move(request.conversation_id, "paid")
        review = self.request_review(session, request, profile, email)
        return {"status": "paid", "invoice_id": invoice.invoice_id, "method": method, "receipt_to": email,
                "review": review}

    # ------------------------------------------------------------ BP-10: review and recovery
    def request_review(self, session: Session, request: ServiceRequest, profile: TenantProfile,
                       email: str | None = None) -> dict:
        existing = session.scalar(select(Review).where(Review.request_id == request.request_id))
        if existing is not None:
            return {"status": "duplicate", "review_id": existing.review_id}
        review = Review(tenant_id=request.tenant_id, request_id=request.request_id)
        session.add(review)
        session.flush()
        link = self._link(tenant=request.tenant_id, conversation=request.conversation_id, review=1) \
            if request.conversation_id else None
        if email and link:
            audited(session, request.tenant_id, request.conversation_id, "request_review",
                    {"request": request.request_id, "channel": "email"},
                    lambda: send(self.settings, review_email(self.settings, profile, email, link),
                                 f"REVIEW-{review.review_id}"), idempotency_key=f"review:{request.request_id}")
        self._move(request.conversation_id, "review_requested")
        return {"status": "requested", "review_id": review.review_id, "emailed": bool(email and link)}

    def record_review(self, session: Session, tenant_id: str, conversation_id: str, rating: int,
                      comment: str = "") -> dict:
        """The customer's rating. Nothing in the comment is acted on: it is stored redacted and shown to staff."""
        if not 1 <= rating <= 5:
            raise ValueError("rating must be 1 to 5")
        review = session.scalar(select(Review).join(ServiceRequest, ServiceRequest.request_id == Review.request_id)
                                .where(Review.tenant_id == tenant_id, ServiceRequest.conversation_id == conversation_id)
                                .order_by(Review.requested_at.desc()))
        if review is None:
            raise LookupError("no review was requested in this conversation")
        request = session.get(ServiceRequest, review.request_id)
        profile = load_profile(self.settings.data_dir, tenant_id)
        if review.status == "submitted":
            return {"status": "duplicate", "message": review_thanks(profile, review.rating),
                    "public_link": profile.review_url}
        clean = redact(comment or "")[:2000]
        audited(session, tenant_id, conversation_id, "record_review", {"rating": rating, "comment_chars": len(clean)},
                lambda: {"status": "recorded"}, idempotency_key=f"review-submitted:{request.request_id}")
        review.rating, review.comment, review.status, review.submitted_at = rating, clean or None, "submitted", _now()
        review.public_link_shown = bool(profile.review_url)  # every reviewer, whatever the rating (no review gating)
        request.status = "reviewed"
        ticket_id = None
        if rating <= LOW_RATING:
            ticket_id = self._ticket(session, request, "P2", f"service recovery: {rating}-star review for request "
                                                             f"{request.request_id[:8].upper()}: {clean[:200] or 'no comment'}")
            review.recovery_escalation_id = ticket_id
        self._move(conversation_id, "recovery" if ticket_id else "reviewed")
        return {"status": "recovery" if ticket_id else "reviewed", "message": review_thanks(profile, rating),
                "public_link": profile.review_url, "ticket_id": ticket_id}

    def payment_status(self, session: Session, tenant_id: str, conversation_id: str) -> Invoice | None:
        """The open invoice in a conversation, for the customer asking how to pay."""
        return session.scalar(select(Invoice).join(ServiceRequest, ServiceRequest.request_id == Invoice.request_id)
                              .where(Invoice.tenant_id == tenant_id, ServiceRequest.conversation_id == conversation_id)
                              .order_by(Invoice.created_at.desc()))


# ---------------------------------------------------------------- customer-facing text and emails
def payment_options(profile: TenantProfile, reference: str) -> str:
    online = ", ".join(METHOD_NAMES.get(m, m) for m in profile.payment_methods)
    offline = f"Zelle to {profile.zelle_handle}, check or cash" if profile.zelle_handle else "check or cash"
    return (f"Pay online by {online} on the secure payment page, or by {offline} (please include reference "
            f"{reference}; the office marks it paid).")


def review_thanks(profile: TenantProfile, rating: int) -> str:
    text = "Thank you for your feedback."
    if rating <= LOW_RATING:
        text += f" I'm sorry the visit didn't go as it should. Someone from {profile.company_name} will contact you."
    if profile.review_url:
        text += f" If you'd like to share your experience publicly, you can leave a review here: {profile.review_url}"
    return text


def _message(settings, to: str, subject: str, lines: list[str]) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"], msg["To"] = subject, to
    msg["From"] = settings.escalation_from_email or "serviceiq@localhost"
    msg.set_content("\n".join(lines))
    return msg


def invoice_email(settings, profile: TenantProfile, to: str, invoice: Invoice, reference: str, note: str = ""):
    return _message(settings, to, f"{profile.company_name}: your invoice for request {reference}", [
        *([note, ""] if note else []),
        f"Thanks for choosing {profile.company_name}. Your visit is complete.", "",
        f"Amount due: {dollars(invoice.amount_cents)}", f"Reference: {reference}", "",
        f"Pay securely: {invoice.payment_url}", "", payment_options(profile, reference), "",
        "We never ask for card or bank details by email or chat.",
        f"Questions? Call {profile.office_phone} ({profile.business_hours})."])


def receipt_email(settings, profile: TenantProfile, to: str, invoice: Invoice, reference: str):
    return _message(settings, to, f"{profile.company_name}: payment received for request {reference}", [
        f"We received your payment of {dollars(invoice.amount_cents)} for request {reference}. Thank you!", "",
        f"Questions? Call {profile.office_phone} ({profile.business_hours})."])


def review_email(settings, profile: TenantProfile, to: str, link: str):
    return _message(settings, to, f"How did we do? {profile.company_name}", [
        f"Thanks again for choosing {profile.company_name}. How was your visit?", "",
        f"Rate it in one click: {link}", "",
        "Your feedback goes straight to the team, and if anything wasn't right we'll make it right."])


def _invoice_approval_email(settings, profile: TenantProfile, request: ServiceRequest, approval: Approval):
    p = approval.proposal
    range_ = (f"${p['range'][0]:,.2f}-${p['range'][1]:,.2f}" if p.get("range") else
              "no published price for this service")
    return _message(settings, profile.escalation_email,
                    f"[APPROVAL] Invoice for request {request.request_id[:8].upper()} needs a decision", [
                        f"Final price ${p['amount']:,.2f} for job {p.get('job')} ({p.get('service') or 'service'}) is "
                        f"outside the range billed automatically ({range_}).", "",
                        f"Technician notes: {p.get('completion_notes') or '-'}", "",
                        "Approve (optionally adjust the amount) or decline in the admin console, Approvals tab. "
                        "Nothing is sent to the customer until you decide."])
