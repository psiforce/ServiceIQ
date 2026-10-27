"""The ServiceIQ assistant: guard -> retrieve -> generate -> check, returning the response contract.

Response contract (architecture section 4.3):
  status      answered | fallback | emergency | escalated | refused | off_topic | sensitive_data
  answer      text shown to the customer, with citation markers renumbered [1], [2], ...
  citations   the sources behind those markers
  escalation  {priority, reason} when a person should follow up, else None
  trace_id    links logs and traces for this turn
The guard and the retrieval run in parallel; if the guard decides not to answer, the retrieval result
is simply discarded (it costs a fraction of a cent).
"""
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from serviceiq.config import Settings
from serviceiq.conversation import rewrite_question
from serviceiq.generation.answer import generate_answer
from serviceiq.guardrails.guard import check_message
from serviceiq.llm import Usage, openai_client
from serviceiq.retrieval.evidence import make_reranker, retrieve_evidence
from serviceiq.retrieval.retriever import RetrievedChunk, Retriever
from serviceiq.tenants import load_profile

GUARD_STATUS = {"emergency": "emergency", "escalate": "escalated", "refuse": "refused", "refuse_unsafe": "refused",
                "off_topic": "off_topic", "sensitive_data": "sensitive_data"}


@dataclass
class Citation:
    n: int
    document_id: str
    version: int
    title: str
    source: str
    section: str
    document_type: str
    snippet: str
    text: str = field(default="", repr=False)  # full chunk text, used by evaluation


@dataclass
class AssistantResponse:
    status: str
    answer: str
    citations: list[Citation] = field(default_factory=list)
    escalation: dict | None = None
    trace_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    cost_usd: float = 0.0
    latency_ms: dict = field(default_factory=dict)
    debug: dict = field(default_factory=dict)  # guard reasons, retrieved chunk IDs, grounding problems
    context: list[str] = field(default_factory=list, repr=False)  # evidence texts given to the model


def _renumber(answer: str, cited: list[int]) -> str:
    mapping = {old: new for new, old in enumerate(cited, 1)}
    return re.sub(r"\[(\d+)\]", lambda m: f"[{mapping.get(int(m.group(1)), m.group(1))}]", answer)


def _citation(n: int, chunk: RetrievedChunk) -> Citation:
    m = chunk.metadata
    body = chunk.text.split("\n\n", 1)[-1]
    return Citation(n, m["document_id"], int(m["version"]), m["title"], m["source"], m.get("section", ""),
                    m["document_type"], " ".join(body.split())[:300], chunk.text)


class Assistant:
    def __init__(self, settings: Settings, retriever: Retriever | None = None, client=None):
        self.settings = settings
        self.retriever = retriever or Retriever(settings)
        self.client = client or openai_client(settings)
        self.reranker = make_reranker(settings)
        self.pool = ThreadPoolExecutor(max_workers=4)

    def _resolve_and_retrieve(self, question: str, tenant_id: str, history: list[tuple[str, str]]):
        standalone, follow_up, usage = rewrite_question(self.client, self.settings.guard_model, history, question)
        ordered, evidence, _ = retrieve_evidence(self.retriever, self.reranker, self.settings, standalone, tenant_id)
        return standalone, follow_up, usage, ordered, evidence

    def ask(self, question: str, tenant_id: str, history: list[tuple[str, str]] | None = None) -> AssistantResponse:
        """Answer `question`. `history` holds earlier (question, answer) turns of the same conversation."""
        s = self.settings
        started = time.perf_counter()
        profile = load_profile(s.data_dir, tenant_id)  # tenant comes from the caller's authentication
        tokens_before = self.retriever.embedding_tokens
        # The guard checks the customer's own words; the rewrite + retrieval run alongside it
        retrieval = self.pool.submit(self._resolve_and_retrieve, question, tenant_id, history or [])
        verdict = check_message(question, profile, self.client, s.guard_model)
        t_guard = time.perf_counter()
        cost = (verdict.usage.cost or 0.0) if verdict.usage else 0.0

        if verdict.action != "answer":
            retrieval.cancel()
            return AssistantResponse(
                status=GUARD_STATUS[verdict.action], answer=verdict.message or "",
                escalation=({"priority": verdict.escalation_priority, "reason": verdict.emergency_type or "customer request"}
                            if verdict.escalation_priority else None),
                cost_usd=cost, latency_ms={"guard": round((t_guard - started) * 1000),
                                           "total": round((t_guard - started) * 1000)},
                debug={"guard": verdict.reasons, "logged_question": verdict.redacted_text})

        standalone, follow_up, rewrite_usage, chunks, evidence = retrieval.result()
        t_retrieval = time.perf_counter()
        gen = generate_answer(self.client, s.answer_model, profile, standalone, evidence, s.serviceiq_as_of_date)
        t_end = time.perf_counter()
        cost += (gen.usage.cost or 0.0) if gen.usage else 0.0
        cost += (rewrite_usage.cost or 0.0) if rewrite_usage else 0.0
        cost += (self.retriever.embedding_tokens - tokens_before) / 1_000_000 * 0.02

        escalation = None
        if gen.needs_human:
            escalation = {"priority": "P2" if gen.status == "answered" else "P3", "reason": gen.escalation_reason}
        return AssistantResponse(
            status=gen.status,
            answer=_renumber(gen.answer, gen.cited) if gen.status == "answered" else gen.answer,
            citations=[_citation(new, evidence[old - 1]) for new, old in enumerate(gen.cited, 1)],
            escalation=escalation,
            cost_usd=cost,
            latency_ms={"guard": round((t_guard - started) * 1000),
                        "retrieval_wait": round((t_retrieval - t_guard) * 1000),
                        "generation": round((t_end - t_retrieval) * 1000),
                        "total": round((t_end - started) * 1000)},
            context=[c.text for c in evidence],
            debug={"standalone_question": standalone if follow_up else None,
                   "guard": verdict.reasons, "injection_suspected": verdict.injection_suspected,
                   "retrieved": [c.chunk_id for c in chunks], "evidence": [c.chunk_id for c in evidence],
                   "attempts": gen.attempts, "grounding_problems": gen.problems,
                   "logged_question": verdict.redacted_text},
        )
