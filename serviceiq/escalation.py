"""Escalation tickets: handoff packet (BP-4) and CSR email notification.

Without SMTP settings, notification emails are written to the outbox folder (var/outbox/*.eml) so the full
flow can be tested locally; set SMTP_HOST etc. in .env to send real email.
"""
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from sqlalchemy.orm import Session

from serviceiq.config import Settings
from serviceiq.storage.models import Conversation, Escalation, ServiceRequest
from serviceiq.tenants import TenantProfile

TRANSCRIPT_MESSAGES = 12


def handoff_packet(conversation: Conversation, trigger: dict | None = None) -> dict:
    """Everything a CSR needs so the customer doesn't have to repeat themselves (text is already redacted)."""
    transcript = [{"role": m.role, "text": m.content, "status": m.status,
                   "at": m.created_at.isoformat(timespec="seconds") if m.created_at else None}
                  for m in conversation.messages[-TRANSCRIPT_MESSAGES:]]
    last_answer = next((m for m in reversed(conversation.messages) if m.role == "assistant"), None)
    return {
        "conversation_id": conversation.conversation_id,
        "transcript": transcript,
        "last_answer_sources": [{"n": c.ordinal, "title": c.title, "document_id": c.document_id, "version": c.version}
                                for c in (last_answer.citations if last_answer else [])],
        "understood_question": (last_answer.details or {}).get("standalone_question") if last_answer else None,
        "trace_id": last_answer.trace_id if last_answer else None,
        **(trigger or {}),
    }


def open_ticket(session: Session, tenant_id: str, conversation: Conversation, message_id: str | None,
                priority: str, reason: str) -> Escalation:
    ticket = Escalation(tenant_id=tenant_id, conversation_id=conversation.conversation_id, message_id=message_id,
                        priority=priority, reason=reason)
    session.add(ticket)
    session.flush()
    ticket.handoff_packet = handoff_packet(conversation, {"trigger": reason})
    return ticket


def _email(settings: Settings, profile: TenantProfile, ticket: Escalation) -> EmailMessage:
    packet = ticket.handoff_packet
    lines = [f"Priority: {ticket.priority}", f"Reason: {ticket.reason}", f"Ticket: {ticket.escalation_id}",
             f"Conversation: {ticket.conversation_id}", ""]
    if ticket.contact_phone or ticket.contact_name:
        lines += ["Customer contact:", f"  Name: {ticket.contact_name or '-'}", f"  Phone: {ticket.contact_phone or '-'}",
                  f"  Email: {ticket.contact_email or '-'}", f"  Best time: {ticket.preferred_time or '-'}", ""]
    else:
        lines += ["Customer contact: not provided yet (customer was offered a callback).", ""]
    lines.append("Conversation (personal details redacted):")
    for turn in packet.get("transcript", []):
        speaker = "Customer" if turn["role"] == "customer" else f"Assistant [{turn.get('status')}]"
        lines.append(f"  {speaker}: {turn['text']}")
    if packet.get("last_answer_sources"):
        lines += ["", "Sources shown to the customer:"]
        lines += [f"  [{s['n']}] {s['title']} ({s['document_id']} v{s['version']})" for s in packet["last_answer_sources"]]
    if packet.get("trace_id"):
        lines += ["", f"Trace ID: {packet['trace_id']}"]

    msg = EmailMessage()
    msg["Subject"] = f"[{ticket.priority}] ServiceIQ handoff: {ticket.reason[:80]}"
    msg["From"] = settings.escalation_from_email or "serviceiq@localhost"  # empty in .env means "not set"
    msg["To"] = profile.escalation_email
    msg.set_content("\n".join(lines))
    return msg


def send(settings: Settings, message: EmailMessage, file_tag: str) -> str:
    """Send through SMTP, or (without SMTP settings) save to the outbox folder. Returns where it went."""
    if settings.smtp_host:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            if settings.smtp_username and settings.smtp_password and settings.smtp_password.get_secret_value():
                smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            smtp.send_message(message)
        return f"email to {message['To']}"
    outbox = Path(settings.outbox_dir)
    outbox.mkdir(parents=True, exist_ok=True)
    path = outbox / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{file_tag}.eml"
    path.write_bytes(bytes(message))
    return str(path)


def notify(settings: Settings, profile: TenantProfile, ticket: Escalation) -> str:
    """Send (or, without SMTP, save) the CSR notification. Returns where it went."""
    destination = send(settings, _email(settings, profile, ticket), f"{ticket.priority}-{ticket.escalation_id}")
    ticket.notified_at = datetime.now(timezone.utc)
    return destination


def notify_service_request(settings: Settings, profile: TenantProfile, request: ServiceRequest, contact: dict,
                           summary: str) -> str:
    """Tell the office a new service request is waiting (Phase 2: a person books the appointment)."""
    lines = [f"Service request: {request.request_id}", f"Conversation: {request.conversation_id}",
             f"Customer record (field-service platform): {request.customer_ref}", "",
             "Customer contact:", f"  Name: {contact.get('name') or '-'}", f"  Phone: {contact.get('phone') or '-'}",
             f"  Email: {contact.get('email') or '-'}",
             f"  Address: {', '.join(x for x in (contact.get('street'), contact.get('city'), contact.get('zip')) if x)}",
             "", "Request:", summary, "", "Reply to the customer to book a time; the assistant does not confirm "
             "appointments."]
    msg = EmailMessage()
    msg["Subject"] = f"[REQUEST] ServiceIQ service request {request.request_id[:8]}: {request.payload.get('equipment_type') or 'HVAC'}"
    msg["From"] = settings.escalation_from_email or "serviceiq@localhost"
    msg["To"] = profile.escalation_email
    msg.set_content("\n".join(lines))
    return send(settings, msg, f"REQ-{request.request_id}")
