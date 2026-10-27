"""ServiceIQ admin console: CSR ticket inbox, knowledge management and analytics.

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
                                      Feedback, Message)
from serviceiq.storage import registry  # noqa: E402

st.set_page_config(page_title="ServiceIQ Admin", page_icon="🛠️", layout="wide")
DOC_TYPES = ["price_book", "policy", "warranty_policy", "promotion", "safety", "faq", "service_catalog",
             "service_area", "maintenance_guide", "manufacturer_doc", "web_page", "blog"]
PRIORITY_ORDER = {"P1": 0, "P2": 1, "P3": 2}


@st.cache_resource
def sessions():
    return session_factory(make_engine(get_settings().database_url))


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

tabs = ["Ticket inbox"] + (["Knowledge", "Analytics"] if user["role"] == "admin" else [])
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

if user["role"] != "admin":
    st.stop()

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
    if st.session_state.get("notice"):
        st.success(st.session_state.pop("notice"))

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
                                                   user["username"], PROJECT_ROOT / "var" / "uploads")
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
