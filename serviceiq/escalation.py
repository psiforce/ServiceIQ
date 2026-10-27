"""Escalation tickets: handoff packet (BP-4) and CSR email notification.

Without SMTP settings, notification emails are written to the outbox folder (var/outbox/*.eml) so the full
flow can be tested locally; set SMTP_HOST etc. in .env to send real email.
"""
import smtplib
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

from serviceiq.config import Settings
from serviceiq.storage.models import Conversation, Escalation
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


def notify(settings: Settings, profile: TenantProfile, ticket: Escalation) -> str:
    """Send (or, without SMTP, save) the CSR notification. Returns where it went."""
    message = _email(settings, profile, ticket)
    if settings.smtp_host:
        with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15) as smtp:
            smtp.starttls()
            if settings.smtp_username and settings.smtp_password and settings.smtp_password.get_secret_value():
                smtp.login(settings.smtp_username, settings.smtp_password.get_secret_value())
            smtp.send_message(message)
        destination = f"email to {profile.escalation_email}"
    else:
        outbox = Path(settings.outbox_dir)
        outbox.mkdir(parents=True, exist_ok=True)
        path = outbox / f"{datetime.now(timezone.utc):%Y%m%d-%H%M%S}-{ticket.priority}-{ticket.escalation_id}.eml"
        path.write_bytes(bytes(message))
        destination = str(path)
    ticket.notified_at = datetime.now(timezone.utc)
    return destination
