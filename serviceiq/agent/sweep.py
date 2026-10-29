"""Approval timeouts (Phase 3): run hourly (Cloud Scheduler -> Cloud Run job, or the compose `sweeper`).

An approval still pending after its policy's timeout (24 hours by default) is escalated once:
  - an urgent reminder email to the company's admins (the office escalation address)
  - a P2 ticket in the inbox, so a person calls the customer
  - an email to the customer, when they gave an address, saying the office will call
The approval stays pending: an admin can still approve or decline it afterwards.
"""
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

from sqlalchemy import select
from sqlalchemy.orm import Session

from serviceiq.agent.fieldservice_client import FieldService
from serviceiq.agent.schema import summary
from serviceiq.config import Settings
from serviceiq.escalation import notify, open_ticket, send
from serviceiq.storage.models import Approval, ApprovalPolicy, Conversation, ServiceRequest
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
