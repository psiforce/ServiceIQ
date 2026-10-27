"""ServiceIQ customer chat (Sprint 1 pilot UI).

Run:  .venv\\Scripts\\streamlit run apps/chat.py
Calls the serviceiq package directly; from M11 it calls the FastAPI service instead.
"""
import sys
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # make `serviceiq` importable when run by streamlit

from serviceiq import feedback  # noqa: E402
from serviceiq.config import get_settings  # noqa: E402
from serviceiq.ingestion.manifest import tenant_ids  # noqa: E402
from serviceiq.tenants import load_profile  # noqa: E402

st.set_page_config(page_title="ServiceIQ Chat", page_icon="💬", layout="centered")

STATUS_BANNER = {
    "emergency": ("error", "🚨", "Safety first: please follow these steps now."),
    "fallback": ("info", "ℹ️", "I couldn't confirm this from approved information."),
    "escalated": ("warning", "🙋", "Connecting you with our team."),
    "sensitive_data": ("warning", "🔒", "Please don't share sensitive details in chat."),
}
SUGGESTIONS = ["How much is a diagnostic visit?", "Is my compressor under warranty?",
               "Do you repair Carrier AC units?", "What does the Comfort Club include?"]


@st.cache_resource(show_spinner="Starting the assistant...")
def get_assistant():
    from serviceiq.assistant import Assistant
    return Assistant(get_settings())


def md(text: str) -> str:
    """Streamlit renders $...$ as math; escape dollar signs so prices display as text."""
    return text.replace("$", r"\$")


def render_assistant(turn: dict, index: int, tenant: str, show_debug: bool) -> None:
    r = turn["response"]
    kind, icon, title = STATUS_BANNER.get(r.status, (None, None, None))
    if kind:
        getattr(st, kind)(title, icon=icon)
    st.markdown(md(r.answer))

    if r.citations:
        with st.expander(f"Sources ({len(r.citations)})"):
            for c in r.citations:
                where = f" › {c.section}" if c.section and c.section != c.title else ""
                link = f" · [open]({c.source})" if c.source.startswith("http") else ""
                st.markdown(f"**[{c.n}] {md(c.title)}**{md(where)}{link}")
                st.caption(md(c.snippet[:240] + ("…" if len(c.snippet) > 240 else "")))

    if r.escalation and r.status != "emergency":
        st.caption(f"Our team can follow up on this ({r.escalation['priority']}: {r.escalation['reason']}).")
        if turn.get("callback_requested"):
            st.success("Callback requested. Our team will contact you.", icon="📞")
        elif st.button("Request a callback", key=f"callback-{index}"):
            feedback.record("callback_request", tenant, r.trace_id, turn["question"], r.status,
                            priority=r.escalation["priority"], reason=r.escalation["reason"])
            turn["callback_requested"] = True
            st.rerun()

    rating = st.feedback("thumbs", key=f"rating-{r.trace_id}")
    if rating is not None and turn.get("rated") != rating:
        feedback.record("rating", tenant, r.trace_id, turn["question"], r.status, rating="up" if rating else "down")
        turn["rated"] = rating

    if show_debug:
        with st.expander("Debug"):
            st.write({"status": r.status, "cost_usd": round(r.cost_usd, 5), "latency_ms": r.latency_ms,
                      "trace_id": r.trace_id, **r.debug})


# ---------------------------------------------------------------- sidebar
settings = get_settings()
tenants = tenant_ids(settings.data_dir)
profiles = {t: load_profile(settings.data_dir, t) for t in tenants}
with st.sidebar:
    st.header("ServiceIQ")
    tenant = st.selectbox("Company (tenant)", tenants, format_func=lambda t: profiles[t].company_name)
    show_debug = st.toggle("Show debug details", value=False)
    if st.button("New conversation", use_container_width=True):
        st.session_state.pop(f"history-{tenant}", None)
        st.rerun()
    st.divider()
    st.caption(f"Pilot demo with synthetic data. Answers use approved documents as of "
               f"{settings.serviceiq_as_of_date:%B %d, %Y}.")

profile = profiles[tenant]
history: list[dict] = st.session_state.setdefault(f"history-{tenant}", [])
spent = sum(turn["response"].cost_usd for turn in history)
st.sidebar.caption(f"Session cost: ${spent:.4f} · {len(history)} question(s)")

# ---------------------------------------------------------------- main
st.title(profile.company_name)
st.caption("Ask about our services, pricing, warranties and maintenance. "
           f"For emergencies, call {profile.emergency_phone or '911'}.")

for i, turn in enumerate(history):
    with st.chat_message("user"):
        st.markdown(md(turn["question"]))
    with st.chat_message("assistant"):
        render_assistant(turn, i, tenant, show_debug)

question = st.chat_input("Type your question...")
if not history and not question:
    st.write("Try one of these:")
    cols = st.columns(2)
    for i, suggestion in enumerate(SUGGESTIONS):
        if cols[i % 2].button(suggestion, key=f"suggest-{i}", use_container_width=True):
            question = suggestion

if question:
    with st.chat_message("user"):
        st.markdown(md(question))
    with st.chat_message("assistant"):
        with st.spinner("Checking our approved information..."):
            try:
                response = get_assistant().ask(question, tenant)
            except Exception as exc:  # keep the chat usable if a service is down
                st.error("Sorry, something went wrong on our side. Please try again, or call "
                         f"{profile.office_phone}.", icon="⚠️")
                st.caption(f"{type(exc).__name__}")
                st.stop()
    history.append({"question": question, "response": response})
    st.rerun()
