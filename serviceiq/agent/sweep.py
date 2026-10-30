"""Hourly sweeps (Cloud Scheduler -> Cloud Run job, or the compose `sweeper`).

Approval timeouts (Phase 3): an approval still pending after its policy's timeout (24 hours by default) is
escalated once:
  - an urgent reminder email to the company's admins (the office escalation address)
  - a P2 ticket in the inbox, so a person calls the customer
  - an email to the customer, when they gave an address, saying the office will call
The approval stays pending: an admin can still approve or decline it afterwards.

Phase 4:
  - completions: a job completed in the platform whose event never arrived is billed as if it had
  - payments: one reminder after PAYMENT_REMINDER_DAYS unpaid, then a P3 ticket after as many days again
  - reviews: one reminder after REVIEW_REMINDER_DAYS, then the request is closed without a review
"""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from serviceiq.agent.fieldservice_client import FieldService
from serviceiq.agent.postservice import invoice_email, review_email
from serviceiq.agent.schema import summary
from serviceiq.config import Settings
from serviceiq.escalation import notify, open_ticket, send
from serviceiq.storage.models import Approval, ApprovalPolicy, Conversation, Invoice, Review, ServiceRequest
from serviceiq.tenants import load_profile

DEFAULT_TIMEOUT_HOURS = 24


def _aware(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def overdue(session: Session, now: datetime) -> list[Approval]:
    pending = session.scalars(select(Approval).where(Approval.status == "pending", Approval.escalated_at.is_(None)))
    found = []
    for approval in pending:
        policy = session.get(ApprovalPolicy, approval.policy_id) if approval.policy_id else None
        hours = policy.timeout_hours if policy and policy.timeout_hours else DEFAULT_TIMEOUT_HOURS
        if now - _aware(approval.created_at) >= timedelta(hours=hours):
            found.append(approval)
    return found


def sweep_approvals(session: Session, settings: Settings, fsp: FieldService, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    results = []
    for approval in overdue(session, now):
        request = session.get(ServiceRequest, approval.request_id)
        profile = load_profile(settings.data_dir, approval.tenant_id)
        waited = (now - _aware(approval.created_at)).total_seconds() / 3600
        reference = request.request_id[:8].upper()

        reminder = EmailMessage()
        reminder["Subject"] = f"[URGENT] Approval waiting {waited:.0f} h: request {reference}"
        reminder["From"] = settings.escalation_from_email or "serviceiq@localhost"
        reminder["To"] = profile.escalation_email
        reminder.set_content("\n".join([
            f"This request has waited {waited:.0f} hours for an approval decision ({approval.reason}).",
            "The customer is being told the office will call them. A ticket is in the inbox.", "",
            summary(request.payload), "", "Decide in the admin console, Approvals tab."]))
        send(settings, reminder, f"URGENT-{approval.approval_id}")

        ticket_id = None
        conversation = session.get(Conversation, request.conversation_id) if request.conversation_id else None
        if conversation is not None:
            ticket = open_ticket(session, approval.tenant_id, conversation, None, "P2",
                                 f"approval overdue ({waited:.0f} h): call the customer about request {reference}")
            notify(settings, profile, ticket)
            ticket_id = ticket.escalation_id

        emailed = None
        try:
            email = ((fsp.get_customer(approval.tenant_id, request.customer_ref) or {}).get("customer") or {}).get("email")
        except Exception:  # the platform being down must not stop the sweep
            email = None
        if email:
            note = EmailMessage()
            note["Subject"] = f"{profile.company_name}: an update on your service request {reference}"
            note["From"] = settings.escalation_from_email or "serviceiq@localhost"
            note["To"] = email
            note.set_content(f"Thanks for your patience. Your service request ({reference}) is still being reviewed, "
                             f"and someone from {profile.company_name} will call you to arrange your visit. If it's "
                             f"urgent, call {profile.office_phone} ({profile.business_hours}).")
            send(settings, note, f"CUSTOMER-{approval.approval_id}")
            emailed = email

        approval.escalated_at = now
        results.append({"approval_id": approval.approval_id, "tenant_id": approval.tenant_id, "waited_hours":
                        round(waited, 1), "ticket_id": ticket_id, "customer_emailed": bool(emailed)})
    session.commit()
    return results


# ---------------------------------------------------------------- Phase 4
def sweep_completions(session: Session, agent) -> list[dict]:
    """Booked jobs the platform shows as completed but ServiceIQ never heard about (webhook lost or API down)."""
    results = []
    for request in list(session.scalars(select(ServiceRequest).where(ServiceRequest.status == "scheduled",
                                                                     ServiceRequest.job_ref.is_not(None)))):
        try:
            job = agent.fsp.get_job(request.tenant_id, request.job_ref)
            if not job or job.get("status") != "completed":
                continue
            result = agent.on_job_completed(session, request.tenant_id, request.job_ref)
            session.commit()  # one request at a time: a failure below never undoes an invoice already sent
        except Exception as exc:  # one bad job must not stop the sweep
            session.rollback()
            result = {"status": "error", "error": type(exc).__name__}
        results.append({"tenant_id": request.tenant_id, "request_id": request.request_id} | result)
    return results


def sweep_payments(session: Session, settings: Settings, agent, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    wait = timedelta(days=settings.payment_reminder_days)
    results = []
    for invoice in session.scalars(select(Invoice).where(Invoice.status.in_(("sent", "failed")),
                                                         Invoice.escalated_at.is_(None))):
        request = session.get(ServiceRequest, invoice.request_id)
        reference = request.request_id[:8].upper()
        if invoice.reminded_at is None and now - _aware(invoice.sent_at) >= wait:
            email = agent._customer_email(invoice.tenant_id, request)
            if email:
                profile = load_profile(settings.data_dir, invoice.tenant_id)
                send(settings, invoice_email(settings, profile, email, invoice, reference,
                                             note="A friendly reminder: this invoice is still open."),
                     f"REMINDER-{invoice.invoice_id}")
            invoice.reminded_at = now
            results.append({"invoice_id": invoice.invoice_id, "action": "reminded", "emailed": bool(email)})
        elif invoice.reminded_at is not None and now - _aware(invoice.reminded_at) >= wait:
            ticket_id = agent._ticket(session, request, "P3", f"invoice {reference} unpaid after a reminder: call "
                                                              "the customer")
            invoice.escalated_at = now
            results.append({"invoice_id": invoice.invoice_id, "action": "ticket", "ticket_id": ticket_id})
    session.commit()
    return results


def sweep_reviews(session: Session, settings: Settings, agent, now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    wait = timedelta(days=settings.review_reminder_days)
    results = []
    for review in session.scalars(select(Review).where(Review.status == "requested")):
        request = session.get(ServiceRequest, review.request_id)
        if review.reminded_at is None and now - _aware(review.requested_at) >= wait:
            email = agent._customer_email(review.tenant_id, request)
            if email and request.conversation_id:
                profile = load_profile(settings.data_dir, review.tenant_id)
                send(settings, review_email(settings, profile, email, agent._link(
                    tenant=review.tenant_id, conversation=request.conversation_id, review=1)),
                     f"REVIEW-REMINDER-{review.review_id}")
            review.reminded_at = now
            results.append({"review_id": review.review_id, "action": "reminded", "emailed": bool(email)})
        elif review.reminded_at is not None and now - _aware(review.reminded_at) >= wait:
            review.status = "closed"
            agent._move(request.conversation_id, "closed")
            results.append({"review_id": review.review_id, "action": "closed"})
    session.commit()
    return results
