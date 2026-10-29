"""ServiceIQ admin console: CSR ticket inbox, service requests, knowledge management and analytics.

Run:  .venv\\Scripts\\streamlit run apps/admin.py --server.port 8502
Every view is scoped to the signed-in user's tenant. Roles: admin (everything) or csr (ticket inbox only).
"""
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from serviceiq.config import PROJECT_ROOT, get_settings  # noqa: E402
from serviceiq.storage.auth import authenticate  # noqa: E402
from serviceiq.storage.db import make_engine, session_factory  # noqa: E402
from serviceiq.storage.models import (Conversation, DocumentRecord, DocumentVersionRecord, Escalation,  # noqa: E402
                                      Feedback, Invoice, Message, Review, ServiceRequest, Approval, ToolCall)
from serviceiq.storage import registry  # noqa: E402
from serviceiq.agent.schema import EQUIPMENT_LABELS, summary  # noqa: E402

st.set_page_config(page_title="ServiceIQ Admin", page_icon="🛠️", layout="wide")
DOC_TYPES = ["price_book", "policy", "warranty_policy", "promotion", "safety", "faq", "service_catalog",
             "service_area", "maintenance_guide", "manufacturer_doc", "web_page", "blog"]
PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}


@st.cache_resource
def sessions():
    return session_factory(make_engine(get_settings().database_url))


@st.cache_resource
def field_service():
    from serviceiq.agent.fieldservice_client import make_field_service
    return make_field_service(get_settings())


@st.cache_resource
def approver_agent():
    """The agent, for approver decisions: booking and moving the conversation on need no language model."""
    from serviceiq.agent import Agent
    return Agent(get_settings(), client=object(), fsp=field_service())


@st.cache_resource
def ingest_dependencies():
    from serviceiq.ingestion.indexing import get_index, openai_embedder
    settings = get_settings()
    return get_index(settings, create=True), openai_embedder(settings)


def reindex(session, document_id: str):
    from serviceiq.ingestion.pipeline import ingest_document
    index, embed = ingest_dependencies()
    return ingest_document(get_settings(), session, document_id, index=index, embed=embed)


def freshness_badge(version: DocumentVersionRecord, document_type: str) -> str:
    if version is None or version.ingest_seconds is None or version.approved_by == "seed":
        return "-"
    sla = registry.sla_seconds(document_type)
    took = version.ingest_seconds
    shown = f"{took:.1f} s" if took < 120 else f"{took / 60:.1f} min"
    return f"{'✅' if took <= sla else '❌'} {shown} (SLA {sla // 60} min)"


# ---------------------------------------------------------------- sign in
if "user" not in st.session_state:
    st.title("ServiceIQ admin console")
    with st.form("login"):
        username = st.text_input("Username")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in"):
            with sessions()() as s:
                user = authenticate(s, username, password)
            if user is None:
                st.error("Invalid username or password.")
            else:
                st.session_state["user"] = {"username": user.username, "tenant_id": user.tenant_id, "role": user.role}
                st.rerun()
    st.caption("Demo accounts are in var/dev_admin_credentials.json (created by `python -m serviceiq init-db`).")
    st.stop()

user = st.session_state["user"]
tenant_id = user["tenant_id"]
with st.sidebar:
    st.header("ServiceIQ admin")
    st.write(f"**{user['username']}** · {user['role']}")
    st.caption(f"Tenant: {tenant_id}")
    if st.button("Sign out", width="stretch"):
        st.session_state.clear()
        st.rerun()

if st.session_state.get("notice"):  # result of the last action, whichever tab it came from
    st.success(st.session_state.pop("notice"))
tabs = ["Ticket inbox", "Service requests", "Schedule"] + (["Approvals", "Knowledge", "Analytics"]
                                                           if user["role"] == "admin" else [])


def fsp_call(method: str, path: str, **kwargs) -> tuple[int, dict]:
    """Call the field-service platform as this tenant; an outage shows as a message, never a crash."""
    try:
        return field_service().call(tenant_id, method, path, **kwargs)
    except Exception as exc:
        return 503, {"detail": f"Field-service platform unavailable ({type(exc).__name__})."}


def local_time(iso: str, fmt: str = "%a %b %d, %H:%M") -> str:
    return datetime.fromisoformat(iso).strftime(fmt)


def book_visit(r: ServiceRequest) -> None:
    """Staff books a slot for a request: availability from the platform, then the job (Phase 2: a person books)."""
    if not st.toggle("Book a visit", key=f"book-toggle-{r.request_id}"):
        return
    code, body = fsp_call("GET", "/api/service-types")
    if code != 200:
        st.warning(body.get("detail"))
        return
    types = body["service_types"]
    default = next((i for i, t in enumerate(types) if "DIAG" in t["code"]), 0)
    chosen = st.selectbox("Service", types, index=default, format_func=lambda t: f"{t['name']} ({t['default_duration']} min)",
                          key=f"book-type-{r.request_id}")
    code, body = fsp_call("GET", "/api/availability", params={
        "date_from": date.today().isoformat(), "date_to": (date.today() + timedelta(days=13)).isoformat(),
        "service_type_id": chosen["id"], "zip": r.payload.get("zip") or "", "limit": 15})
    if code != 200 or not body.get("slots"):
        st.warning(body.get("detail") or "No free slots in the next two weeks.")
        return
    slot = st.radio("Free slots", body["slots"], key=f"book-slot-{r.request_id}",
                    format_func=lambda x: f"{local_time(x['local_start'])} · {x['technician_name']}")
    if st.button("Book this slot", key=f"book-go-{r.request_id}", type="primary"):
        code, job = fsp_call("POST", "/api/jobs", idempotency_key=f"book-{r.request_id}-{slot['start']}", json={
            "customer_id": r.customer_ref, "service_type_id": chosen["id"], "technician_id": slot["technician_id"],
            "scheduled_start": slot["start"], "asset_id": r.payload.get("asset_ref"),
            "priority": "high" if r.payload.get("urgency") == "urgent" else "normal",
            "notes": summary(r.payload), "external_ref": r.request_id})
        if code == 201:
            r.job_ref, r.status = job["id"], "scheduled"
            st.session_state["notice"] = (f"Booked {job['identifier']} for {local_time(job['local_start'])} with "
                                          f"{job['technician_name']}.")
            st.rerun()
        st.warning(f"Couldn't book that slot: {job.get('detail')}. Pick another one.")


def bill_completed_job(job_id: str) -> str:
    """Phase 4: invoice a job just marked completed (idempotent: a repeated event bills once)."""
    try:
        with sessions()() as s:
            result = approver_agent().on_job_completed(s, tenant_id, job_id)
            s.commit()
    except Exception as exc:
        return f"Invoice not created ({type(exc).__name__}); the hourly sweep will retry."
    return {"sent": "Invoice and secure payment link sent to the customer.",
            "needs_approval": "The final price is outside the published range: approve the invoice in Approvals.",
            "manual": "Automatic invoicing is off: a ticket asks the office to bill the customer.",
            "duplicate": "Already invoiced."}.get(result["status"], "Not a ServiceIQ booking: bill as usual.")


INVOICE_ICONS = {"pending_approval": "⏳", "sent": "📨", "processing": "🏦", "paid": "✅", "failed": "⚠️", "void": "🚫"}


def invoice_panel(session, r: ServiceRequest) -> None:
    """Phase 4: the request's invoice, an off-line payment form, and the customer's review."""
    invoice = session.scalar(select(Invoice).where(Invoice.request_id == r.request_id))
    if invoice is None:
        return
    st.markdown(f"**Invoice** {INVOICE_ICONS.get(invoice.status, '')} {invoice.status} · "
                + f"${invoice.amount_cents / 100:,.2f}".replace("$", chr(92) + "$")
                + (f" · paid by {invoice.method}" if invoice.status == "paid" else "")
                + (f" (recorded by {invoice.recorded_by})" if invoice.recorded_by else ""))
    if invoice.status in ("sent", "failed", "processing"):
        with st.form(f"mark-paid-{invoice.invoice_id}"):
            method = st.selectbox("Paid off-line by", ["zelle", "check", "cash"],
                                  format_func={"zelle": "Zelle", "check": "Check", "cash": "Cash"}.get)
            if st.form_submit_button("Mark paid"):
                approver_agent().record_manual_payment(session, invoice.invoice_id, method, user["username"])
                session.commit()
                st.session_state["notice"] = "Payment recorded; a receipt and a review request were sent."
                st.rerun()
    review = session.scalar(select(Review).where(Review.request_id == r.request_id))
    if review is not None and review.status == "submitted":
        stars = "★" * review.rating + "☆" * (5 - review.rating)
        st.markdown(f"**Review** {stars}" + (f" · “{review.comment}”" if review.comment else ""))
        if review.recovery_escalation_id:
            st.caption("Low rating: a recovery ticket is in the inbox.")
    elif review is not None:
        st.caption(f"Review {review.status}.")


def parse_hours(text: str) -> dict:
    """'mon 07:00-18:00' per line -> {'mon': ['07:00', '18:00']}"""
    hours = {}
    for line in text.strip().splitlines():
        day, _, span = line.strip().partition(" ")
        start, _, end = span.strip().partition("-")
        hours[day.lower()[:3]] = [start.strip(), end.strip()]
    return hours
views = dict(zip(tabs, st.tabs(tabs)))

# ---------------------------------------------------------------- ticket inbox (BP-4)
with views["Ticket inbox"], sessions()() as session:
    status_filter = st.radio("Show", ["open", "contacted", "resolved", "all"], horizontal=True)
    query = select(Escalation).where(Escalation.tenant_id == tenant_id)
    if status_filter != "all":
        query = query.where(Escalation.status == status_filter)
    tickets = sorted(session.scalars(query), key=lambda t: (PRIORITY_ORDER.get(t.priority, 9), -t.created_at.timestamp()))
    if not tickets:
        st.info("No tickets in this view.")
    for t in tickets:
        icon = {"P1": "🚨", "P2": "🟠", "P3": "🔵"}.get(t.priority, "")
        with st.expander(f"{icon} {t.priority} · {t.reason} · {t.created_at:%b %d %H:%M} · {t.status}",
                         expanded=t.priority == "P1" and t.status == "open"):
            left, right = st.columns([3, 2])
            with left:
                st.markdown("**Conversation** (personal details redacted)")
                for turn in t.handoff_packet.get("transcript", []):
                    who = "🧑 Customer" if turn["role"] == "customer" else f"🤖 Assistant ({turn.get('status')})"
                    st.markdown(f"{who}: {turn['text'].replace('$', chr(92) + '$')}")
                if t.handoff_packet.get("customer_note"):
                    st.markdown(f"**Customer note:** {t.handoff_packet['customer_note']}")
                sources = t.handoff_packet.get("last_answer_sources") or []
                if sources:
                    st.caption("Sources shown: " + "; ".join(f"[{x['n']}] {x['title']}" for x in sources))
            with right:
                st.markdown("**Contact**")
                if t.contact_phone:
                    st.write(f"{t.contact_name} · {t.contact_phone}" + (f" · {t.contact_email}" if t.contact_email else ""))
                    if t.preferred_time:
                        st.caption(f"Best time: {t.preferred_time}")
                else:
                    st.caption("Not provided yet (customer was offered a callback).")
                with st.form(f"ticket-{t.escalation_id}"):
                    new_status = st.selectbox("Status", ["open", "contacted", "resolved"],
                                              index=["open", "contacted", "resolved"].index(t.status))
                    resolution = st.text_area("Resolution notes", value=t.resolution or "")
                    gap = st.checkbox("Knowledge gap: the assistant lacked or had wrong information",
                                      value=bool(t.handoff_packet.get("knowledge_gap")))
                    if st.form_submit_button("Save"):
                        t.status, t.resolution = new_status, resolution or None
                        t.handoff_packet = {**t.handoff_packet, "knowledge_gap": gap}
                        session.commit()
                        st.success("Saved.")
                        st.rerun()
                st.caption(f"Ticket {t.escalation_id} · trace {t.handoff_packet.get('trace_id') or '-'}")

# ---------------------------------------------------------------- service requests (BP-6, Phase 2 intake)
with views["Service requests"], sessions()() as session:
    st.caption("Requests the assistant collected. Contact details come live from the field-service platform. "
               "After a visit: invoice, payment and review.")
    request_statuses = ["new", "needs_approval", "contacted", "scheduled", "completed", "invoiced", "paid", "reviewed",
                        "closed"]
    request_filter = st.radio("Show", request_statuses + ["all"], horizontal=True, key="request-filter")
    query = select(ServiceRequest).where(ServiceRequest.tenant_id == tenant_id).order_by(ServiceRequest.created_at.desc())
    if request_filter != "all":
        query = query.where(ServiceRequest.status == request_filter)
    requests = list(session.scalars(query.limit(100)))
    if not requests:
        st.info("No service requests in this view.")
    for r in requests:
        p = r.payload
        urgent = "🔴 " if p.get("urgency") == "urgent" else ""
        with st.expander(f"{urgent}{EQUIPMENT_LABELS.get(p.get('equipment_type'), 'HVAC')} · {p.get('symptoms', '')[:60]} "
                         f"· {r.created_at:%b %d %H:%M} · {r.status}"):
            left, right = st.columns([3, 2])
            with left:
                st.markdown(summary(p).replace("$", chr(92) + "$"))
                st.caption(f"Request {r.request_id} · schema v{r.schema_version}")
                if r.job_ref:
                    code, job = fsp_call("GET", f"/api/jobs/{r.job_ref}")
                    if code == 200:
                        st.success(f"Booked: {job['identifier']} · {local_time(job['local_start'])} · "
                                   f"{job['technician_name']} · {job['status']}", icon="📅")
                elif r.status != "closed":
                    book_visit(r)
                    session.commit()
                invoice_panel(session, r)
            with right:
                st.markdown("**Customer**")
                try:
                    record = field_service().get_customer(tenant_id, r.customer_ref)
                except Exception as exc:  # the platform being down must not break the console
                    record = None
                    st.warning(f"Field-service platform unavailable ({type(exc).__name__}).")
                if record:
                    c = record["customer"]
                    st.write(f"{c['name']} · {c['phone']}" + (f" · {c['email']}" if c.get("email") else ""))
                    st.caption(", ".join(x for x in (c.get("address"), c.get("city"), c.get("zip")) if x))
                with st.form(f"request-{r.request_id}"):
                    new_status = st.selectbox("Status", request_statuses, index=request_statuses.index(r.status))
                    notes = st.text_area("Staff notes", value=r.staff_notes or "")
                    if st.form_submit_button("Save"):
                        r.status, r.staff_notes = new_status, notes or None
                        session.commit()
                        st.success("Saved.")
                        st.rerun()

# ---------------------------------------------------------------- schedule (M16: field-service platform)
with views["Schedule"]:
    day = st.date_input("Day", value=date.today(), key="schedule-day")
    code, sched = fsp_call("GET", "/api/schedule", params={"date_from": day.isoformat(), "date_to": day.isoformat()})
    code_techs, techs = fsp_call("GET", "/api/technicians", params={"include_inactive": True})
    if code != 200 or code_techs != 200:
        st.warning((sched if code != 200 else techs).get("detail"))
    else:
        techs = techs["technicians"]
        st.caption(f"Times in {sched['timezone']}. Bookings are checked against working hours and existing jobs.")
        for tech in [t for t in techs if t["active"]]:
            jobs = [j for j in sched["jobs"] if j["technician_id"] == tech["id"]]
            st.markdown(f"**{tech['name']}** · {len(jobs)} job(s)")
            if jobs:
                st.dataframe([{"Time": local_time(j["local_start"], "%H:%M"), "Job": j["identifier"],
                               "Customer": j["customer_name"], "Service": j["service_name"] or "-",
                               "Minutes": j["duration"], "Status": j["status"], "Priority": j["priority"]}
                              for j in jobs], hide_index=True, width="stretch")
        if sched["jobs"]:
            st.markdown("**Change a job**")
            job = st.selectbox("Job", sched["jobs"], key="schedule-job", format_func=lambda j: (
                f"{j['identifier']} · {local_time(j['local_start'], '%H:%M')} · {j['customer_name']} · "
                f"{j['technician_name']}"))
            active = [t for t in techs if t["active"]]
            with st.form("job-change"):
                c1, c2, c3, c4 = st.columns(4)
                tech_index = next((i for i, t in enumerate(active) if t["id"] == job["technician_id"]), 0)
                new_tech = c1.selectbox("Technician", active, index=tech_index, format_func=lambda t: t["name"])
                start_local = datetime.fromisoformat(job["local_start"])
                new_day = c2.date_input("Date", value=start_local.date())
                new_time = c3.time_input("Start", value=start_local.time(), step=timedelta(minutes=30))
                statuses = ["scheduled", "confirmed", "in_progress", "completed", "cancelled"]
                new_status = c4.selectbox("Status", statuses, index=statuses.index(job["status"]))
                p1, p2 = st.columns([1, 3])
                final_price = p1.number_input("Final price, $ (when completed)", min_value=0.0, step=1.0,
                                              value=float(job.get("price") or 0))
                completion = p2.text_input("Completion notes (when completed)", value=job.get("completion_notes") or "")
                override = st.checkbox("Allow outside working hours (dispatcher override)")
                if st.form_submit_button("Save job"):
                    start = datetime.combine(new_day, new_time, start_local.tzinfo)
                    change = {"technician_id": new_tech["id"], "scheduled_start": start.isoformat(),
                              "status": new_status, "allow_outside_hours": override}
                    completing = new_status == "completed" and job["status"] != "completed"
                    if completing:
                        change |= {"price": final_price, "completion_notes": completion}
                    code, body = fsp_call("PUT", f"/api/jobs/{job['id']}", json=change)
                    if code == 200:
                        notice = f"{job['identifier']} updated."
                        if completing:  # bill now (the platform's event and the hourly sweep would also do it)
                            notice += " " + bill_completed_job(job["id"])
                        st.session_state["notice"] = notice
                        st.rerun()
                    st.error(f"Not saved: {body.get('detail')}")
        else:
            st.info("No jobs on this day.")

        if user["role"] == "admin":
            st.divider()
            st.subheader("Technicians")
            st.caption("Working hours are local time, one day per line, e.g. `mon 07:00-18:00`. Leave service ZIPs "
                       "empty to cover the whole service area.")
            for tech in techs + [None]:
                new = tech is None
                with st.form(f"tech-{'new' if new else tech['id']}"):
                    st.markdown("**Add a technician**" if new else f"**{tech['name']}**")
                    c1, c2 = st.columns(2)
                    name = c1.text_input("Name", value="" if new else tech["name"])
                    active_flag = c2.checkbox("Active", value=True if new else tech["active"])
                    hours_text = st.text_area("Working hours", height=120, value="mon 08:00-17:00\ntue 08:00-17:00\n"
                                              "wed 08:00-17:00\nthu 08:00-17:00\nfri 08:00-17:00" if new else
                                              "\n".join(f"{d} {h[0]}-{h[1]}" for d, h in tech["working_hours"].items()))
                    c3, c4 = st.columns(2)
                    skills = c3.text_input("Skills (comma separated)", value="" if new else ", ".join(tech["skills"]))
                    zips = c4.text_input("Service ZIPs (comma separated)",
                                         value="" if new else ", ".join(tech["service_zips"]))
                    if st.form_submit_button("Add technician" if new else "Save technician") and name:
                        try:
                            hours = parse_hours(hours_text)
                        except ValueError:
                            hours = None
                        body = {"name": name, "working_hours": hours,
                                "skills": [x.strip() for x in skills.split(",") if x.strip()],
                                "service_zips": [x.strip() for x in zips.split(",") if x.strip()]}
                        code, result = (fsp_call("POST", "/api/technicians", json=body) if new else
                                        fsp_call("PUT", f"/api/technicians/{tech['id']}",
                                                 json=body | {"active": active_flag}))
                        if code in (200, 201):
                            st.session_state["notice"] = f"Technician {name} saved."
                            st.rerun()
                        st.error(f"Not saved: {result.get('detail')}")

if user["role"] != "admin":
    st.stop()

# ---------------------------------------------------------------- approvals (M17: BP-7 approval gate)
with views["Approvals"], sessions()() as session:
    pending = list(session.scalars(select(Approval).where(Approval.tenant_id == tenant_id, Approval.status == "pending")
                                   .order_by(Approval.created_at)))
    st.caption("Requests outside the auto-approval policy (discounts or guaranteed prices, after-hours visits, "
               "warranty decisions, final prices outside the published range). The agent books or bills nothing "
               "until you decide.")
    if not pending:
        st.info("Nothing waiting for approval.")
    for a in pending:
        request = session.get(ServiceRequest, a.request_id)
        waited = (datetime.now(timezone.utc) - a.created_at.replace(tzinfo=a.created_at.tzinfo or timezone.utc))
        if (a.proposal or {}).get("kind") == "invoice":  # Phase 4: a final price outside the published range
            p = a.proposal
            with st.expander(f"🧾 invoice outside the published range · request {a.request_id[:8].upper()} · "
                             f"waiting {waited.total_seconds() / 3600:.1f} h", expanded=True):
                left, right = st.columns([3, 2])
                with left:
                    allowed = (f"${p['range'][0]:,.2f}-${p['range'][1]:,.2f}" if p.get("range")
                               else "no published price for this service")
                    st.markdown(f"Job **{p.get('job')}** · {p.get('service') or 'service'} · final price "
                                f"**${p['amount']:,.2f}** (billed automatically: {allowed})".replace("$", chr(92) + "$"))
                    st.caption(f"Technician notes: {p.get('completion_notes') or '-'}")
                    st.markdown(summary(request.payload).replace("$", chr(92) + "$"))
                with right:
                    amount = st.number_input("Amount to bill, $", min_value=0.01, value=float(p["amount"]), step=1.0,
                                             key=f"amount-{a.approval_id}")
                    note = st.text_input("Note (required to decline)", key=f"note-{a.approval_id}")
                    approve_col, reject_col = st.columns(2)
                    if approve_col.button("Approve and send", key=f"approve-{a.approval_id}", type="primary"):
                        try:
                            result = approver_agent().decide(session, a.approval_id, user["username"], True,
                                                             price=amount)
                            session.commit()
                            st.session_state["notice"] = (
                                f"Invoice for ${amount:,.2f} sent" + (f" to {result['emailed']}." if result.get("emailed")
                                                                      else "; a ticket asks the office to pass on the link."))
                            st.rerun()
                        except Exception as exc:
                            session.rollback()
                            st.error(f"Not sent: {exc}")
                    if reject_col.button("Decline", key=f"reject-{a.approval_id}", disabled=not note):
                        approver_agent().decide(session, a.approval_id, user["username"], False, note=note)
                        session.commit()
                        st.session_state["notice"] = "Invoice declined; a ticket asks the office to settle the bill."
                        st.rerun()
            continue
        service = (a.proposal or {}).get("service") or {}
        with st.expander(f"⏳ {a.reason.replace('_', ' ')} · request {a.request_id[:8].upper()} · waiting "
                         f"{waited.total_seconds() / 3600:.1f} h", expanded=True):
            left, right = st.columns([3, 2])
            with left:
                st.markdown(summary(request.payload).replace("$", chr(92) + "$"))
                code, record = fsp_call("GET", f"/api/customers/{request.customer_ref}")
                if code == 200:
                    c = record["customer"]
                    st.caption(f"{c['name']} · {c['phone']} · " + ", ".join(x for x in (c.get("address"), c.get("zip"))
                                                                               if x))
                st.caption(((a.proposal or {}).get("price_line") or f"Service: {service.get('name', '-')}")
                           .replace("$", chr(92) + "$"))
            with right:
                code, body = fsp_call("GET", "/api/availability", params={
                    "date_from": date.today().isoformat(), "date_to": (date.today() + timedelta(days=13)).isoformat(),
                    "service_type_id": service.get("id"), "zip": request.payload.get("zip") or "", "limit": 12})
                slots = body.get("slots", []) if code == 200 else []
                custom = st.checkbox("Book a custom time (e.g. after hours)", key=f"custom-{a.approval_id}")
                outside, slot = False, None
                if custom:
                    _, org = fsp_call("GET", "/api/org")
                    _, team = fsp_call("GET", "/api/technicians")
                    tech = st.selectbox("Technician", team.get("technicians", []), format_func=lambda t: t["name"],
                                        key=f"custom-tech-{a.approval_id}")
                    day_value = st.date_input("Date", key=f"custom-day-{a.approval_id}")
                    time_value = st.time_input("Start", key=f"custom-time-{a.approval_id}", step=timedelta(minutes=30))
                    outside = st.checkbox("Outside working hours (dispatcher override)", key=f"custom-out-{a.approval_id}")
                    if tech and org.get("timezone"):
                        from zoneinfo import ZoneInfo
                        local = datetime.combine(day_value, time_value, ZoneInfo(org["timezone"]))
                        slot = {"start": local.astimezone(timezone.utc).isoformat(), "local_start": local.isoformat(),
                                "technician_id": tech["id"], "technician_name": tech["name"]}
                elif slots:
                    slot = st.radio("Free slot", slots, key=f"slot-{a.approval_id}",
                                    format_func=lambda x: f"{local_time(x['local_start'])} · {x['technician_name']}")
                else:
                    st.warning(body.get("detail") or "No free slots in the next two weeks; use a custom time.")
                from serviceiq.agent import operations
                published = operations.published_price(session, get_settings(), tenant_id, service.get("code", ""))
                price = None
                if published and published["high"] > published["low"]:
                    price = st.number_input(f"Price (published range ${published['low']:g}-${published['high']:g})",
                                            min_value=published["low"], max_value=published["high"],
                                            value=published["low"], key=f"price-{a.approval_id}")
                note = st.text_input("Note (shown in the request; required to decline)", key=f"note-{a.approval_id}")
                approve_col, reject_col = st.columns(2)
                if approve_col.button("Approve and book", key=f"approve-{a.approval_id}", type="primary",
                                      disabled=slot is None):
                    try:
                        result = approver_agent().decide(
                            session, a.approval_id, user["username"], True, slot=slot, price=price,
                            price_range=(published["low"], published["high"]) if published else None,
                            outside_hours=outside)
                        session.commit()
                        booking = result["booking"]
                        st.session_state["notice"] = (
                            f"Approved: {booking['identifier']} booked for {local_time(booking['local_start'])}. "
                            + (f"Confirmation emailed to {result['emailed']}." if result.get("emailed")
                               else "A ticket asks the office to call the customer."))
                        st.rerun()
                    except Exception as exc:
                        session.rollback()
                        st.error(f"Not booked: {exc}")
                if reject_col.button("Decline", key=f"reject-{a.approval_id}", disabled=not note):
                    approver_agent().decide(session, a.approval_id, user["username"], False, note=note)
                    session.commit()
                    st.session_state["notice"] = "Declined; a ticket asks the office to call the customer."
                    st.rerun()

# ---------------------------------------------------------------- knowledge (BP-2)
with views["Knowledge"], sessions()() as session:
    docs = list(session.scalars(select(DocumentRecord).where(DocumentRecord.tenant_id == tenant_id)
                                .order_by(DocumentRecord.document_id)))
    pending = [(d, v) for d in docs for v in d.versions if v.approval_status == "pending"]

    st.subheader(f"Awaiting approval ({len(pending)})")
    if not pending:
        st.caption("Nothing to approve.")
    for doc, version in pending:
        c1, c2, c3 = st.columns([5, 1, 1])
        c1.markdown(f"**{doc.title}** (`{doc.document_id}` v{version.version}, {doc.document_type}) · effective "
                    f"{version.effective_date}{' · expires ' + version.expiry_date if version.expiry_date else ''} · "
                    f"uploaded by {version.uploaded_by}")
        if c2.button("Approve", key=f"approve-{version.id}", type="primary"):
            registry.approve(session, version, user["username"])
            session.commit()
            with st.spinner("Indexing the approved version..."):
                report = reindex(session, doc.document_id)
            st.session_state["notice"] = (f"{doc.title} v{version.version} is live: {report.embedded} chunk(s) embedded, "
                                          f"{report.deleted} old chunk(s) retired. Freshness: "
                                          f"{freshness_badge(version, doc.document_type)}")
            st.rerun()
        if c3.button("Reject", key=f"reject-{version.id}"):
            registry.reject(version, user["username"])
            session.commit()
            st.rerun()

    st.subheader("Documents")
    rows = []
    for d in docs:
        v = registry.current_version(d)
        rows.append({"document": d.document_id, "title": d.title, "type": d.document_type, "authority": str(d.authority_rank),
                     "scope": d.access_scope, "live version": str(v.version) if v else "-",
                     "effective": v.effective_date if v else "-", "expires": (v.expiry_date or "never") if v else "-",
                     "state": "retired" if d.retired else ("live" if v else "not approved"),
                     "freshness": freshness_badge(v, d.document_type)})
    st.dataframe(rows, width="stretch", hide_index=True)

    left, right = st.columns(2)
    with left, st.form("upload", clear_on_submit=True):
        st.markdown("**Upload a document or a new version**")
        target = st.selectbox("Document", ["➕ New document"] + [d.document_id for d in docs])
        title = st.text_input("Title (new documents)")
        doc_type = st.selectbox("Type (new documents)", DOC_TYPES)
        authority = st.number_input("Authority rank (1 = most authoritative)", 1, 5, 3)
        scope = st.selectbox("Access", ["public", "internal"])
        upload = st.file_uploader("File", type=sorted(registry.ALLOWED_FORMATS))
        effective = st.date_input("Effective date", value=get_settings().serviceiq_as_of_date)
        expires = st.date_input("Expiry date (optional)", value=None)
        if st.form_submit_button("Upload as pending"):
            if upload is None or (target.startswith("➕") and not title):
                st.error("Choose a file (and a title for new documents).")
            else:
                try:
                    doc = (registry.add_document(session, tenant_id, title, doc_type, int(authority), access_scope=scope)
                           if target.startswith("➕") else session.get(DocumentRecord, target))
                    version = registry.add_version(session, doc, upload.name, upload.getvalue(), effective, expires,
                                                   user["username"], PROJECT_ROOT / "var" / "uploads",
                                                   bucket=get_settings().uploads_bucket)
                    session.commit()
                    st.session_state["notice"] = f"Uploaded {doc.document_id} v{version.version}; it is pending approval."
                    st.rerun()
                except ValueError as exc:
                    session.rollback()
                    st.error(str(exc))

    with right:
        st.markdown("**Expire or retire a live document**")
        live = [d for d in docs if registry.current_version(d) is not None]
        chosen = st.selectbox("Document", [d.document_id for d in live], key="manage-doc")
        if chosen:
            doc = session.get(DocumentRecord, chosen)
            new_expiry = st.date_input("New expiry date", value=None, key="new-expiry")
            c1, c2 = st.columns(2)
            if c1.button("Set expiry", disabled=new_expiry is None):
                registry.set_expiry(doc, new_expiry)
                session.commit()
                with st.spinner("Updating search metadata..."):
                    report = reindex(session, chosen)
                st.session_state["notice"] = (f"{chosen} now expires {new_expiry} "
                                              f"({report.metadata_updated} chunk(s) updated in place).")
                st.rerun()
            if c2.button("Retire now", type="secondary"):
                registry.retire(doc)
                session.commit()
                with st.spinner("Removing from search..."):
                    report = reindex(session, chosen)
                st.session_state["notice"] = f"{chosen} retired; {report.deleted} chunk(s) removed from search."
                st.rerun()

# ---------------------------------------------------------------- analytics (BP-5 inputs)
with views["Analytics"], sessions()() as session:
    since = datetime.now(timezone.utc) - timedelta(days=30)
    conv_ids = select(Conversation.conversation_id).where(Conversation.tenant_id == tenant_id,
                                                          Conversation.started_at >= since)
    replies = list(session.scalars(select(Message).where(Message.conversation_id.in_(conv_ids),
                                                         Message.role == "assistant")))
    ratings = dict(session.execute(select(Feedback.rating, func.count()).join(Message)
                                   .where(Message.conversation_id.in_(conv_ids)).group_by(Feedback.rating)).all())
    tickets = list(session.scalars(select(Escalation).where(Escalation.tenant_id == tenant_id,
                                                            Escalation.created_at >= since)))
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Conversations (30 days)", session.scalar(select(func.count()).select_from(conv_ids.subquery())))
    c2.metric("Answers", len(replies))
    up, down = ratings.get("up", 0), ratings.get("down", 0)
    c3.metric("Positive feedback", f"{up / (up + down):.0%}" if up + down else "-", help=f"{up} up / {down} down")
    c4.metric("Open tickets", sum(t.status == "open" for t in tickets))
    if replies:
        by_status = {}
        for r in replies:
            by_status[r.status] = by_status.get(r.status, 0) + 1
        self_service = by_status.get("answered", 0) / len(replies)
        latencies = sorted(r.latency_ms for r in replies if r.latency_ms)
        d1, d2, d3 = st.columns(3)
        d1.metric("Answered without a person", f"{self_service:.0%}", help="Pilot KPI hypothesis: ≥ 40%")
        d2.metric("P95 latency", f"{latencies[max(0, round(0.95 * len(latencies)) - 1)] / 1000:.1f} s" if latencies else "-")
        d3.metric("Model cost (30 days)", f"${sum(r.cost_usd or 0 for r in replies):.3f}")
        st.markdown("**Responses by status**")
        st.bar_chart(by_status)
    gaps = [t for t in tickets if t.handoff_packet.get("knowledge_gap")]
    st.markdown(f"**Knowledge gaps flagged by staff:** {len(gaps)}")
    for t in gaps:
        first = next((x["text"] for x in t.handoff_packet.get("transcript", []) if x["role"] == "customer"), "")
        st.caption(f"• {t.created_at:%b %d}: \"{first}\" ({t.resolution or 'no notes'})")

    # Phase 3 success measure: bookings the agent made that dispatchers left as they were
    st.markdown("**Agent bookings (30 days)**")
    booked = list(session.scalars(select(ToolCall).where(ToolCall.tenant_id == tenant_id, ToolCall.tool == "book_job",
                                                         ToolCall.ok.is_(True), ToolCall.created_at >= since)))
    kept = changed = 0
    for call in booked[:200]:
        result = call.result or {}
        code, job = fsp_call("GET", f"/api/jobs/{result.get('job_id')}")
        if code != 200:
            continue
        same = (job["status"] != "cancelled" and job["scheduled_start"] == datetime.fromisoformat(result["start"])
                .astimezone(timezone.utc).isoformat() and job["technician_name"] == result.get("technician_name"))
        kept, changed = kept + same, changed + (not same)
    pending_approvals = session.scalar(select(func.count()).select_from(Approval).where(
        Approval.tenant_id == tenant_id, Approval.status == "pending"))
    e1, e2, e3 = st.columns(3)
    e1.metric("Booked by the agent", len(booked), help="Includes bookings an admin approved")
    e2.metric("Kept unchanged by dispatchers", f"{kept / (kept + changed):.0%}" if kept + changed else "-",
              help=f"{kept} kept, {changed} moved, reassigned or cancelled. Target: 9 out of 10.")
    e3.metric("Approvals waiting", pending_approvals)

    # Phase 4: payment and review
    st.markdown("**Payments and reviews (30 days)**")
    invoices = list(session.scalars(select(Invoice).where(Invoice.tenant_id == tenant_id, Invoice.created_at >= since)))
    paid = [i for i in invoices if i.status == "paid"]
    open_invoices = [i for i in invoices if i.status in ("sent", "failed", "processing", "pending_approval")]
    days_to_pay = sorted((i.paid_at - i.sent_at).total_seconds() / 86400 for i in paid if i.sent_at and i.paid_at)
    reviews = list(session.scalars(select(Review).where(Review.tenant_id == tenant_id, Review.status == "submitted",
                                                        Review.submitted_at >= since)))
    f1, f2, f3, f4 = st.columns(4)
    f1.metric("Collected", f"${sum(i.amount_cents for i in paid) / 100:,.2f}", help=f"{len(paid)} invoice(s) paid")
    f2.metric("Unpaid", f"${sum(i.amount_cents for i in open_invoices) / 100:,.2f}",
              help=f"{len(open_invoices)} invoice(s) sent, processing or awaiting approval")
    f3.metric("Median days to pay", f"{days_to_pay[len(days_to_pay) // 2]:.1f}" if days_to_pay else "-")
    f4.metric("Average rating", f"{sum(r.rating for r in reviews) / len(reviews):.1f} ★" if reviews else "-",
              help=f"{len(reviews)} review(s); {sum(r.rating <= 3 for r in reviews)} rated 3 or lower (recovery)")
