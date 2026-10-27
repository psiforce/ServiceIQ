"""Generate a grounded, cited answer; check it; retry once; otherwise fall back (fail closed)."""
from dataclasses import dataclass, field
from datetime import date

from serviceiq.generation.grounding import assemble, check_answer
from serviceiq.generation.prompts import system_prompt, user_prompt
from serviceiq.llm import Usage, chat_json
from serviceiq.retrieval.retriever import RetrievedChunk
from serviceiq.tenants import TenantProfile

SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["answered", "insufficient_evidence"]},
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"text": {"type": "string"}, "sources": {"type": "array", "items": {"type": "integer"}}},
                "required": ["text", "sources"],
                "additionalProperties": False,
            },
        },
        "needs_human": {"type": "boolean"},
        "escalation_reason": {"type": "string"},
        "calculations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"expression": {"type": "string"}, "result": {"type": "string"}},
                "required": ["expression", "result"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["status", "sentences", "needs_human", "escalation_reason", "calculations"],
    "additionalProperties": False,
}

FALLBACK = ("I don't have confirmed information about that in our approved resources, so I don't want to guess. "
            "Our team at {company} can help: call {office_phone} ({hours}), or ask me to have someone contact you.")


@dataclass
class Generation:
    status: str                      # answered | fallback
    answer: str                      # text with [n] markers numbered by evidence position
    needs_human: bool = False
    escalation_reason: str = ""
    cited: list[int] = field(default_factory=list)  # evidence numbers in order of first citation
    problems: list[str] = field(default_factory=list)  # grounding problems from the last attempt
    attempts: int = 0
    usage: Usage | None = None


def fallback_text(profile: TenantProfile) -> str:
    return FALLBACK.format(company=profile.company_name, office_phone=profile.office_phone,
                           hours=profile.business_hours)


def generate_answer(client, model: str, profile: TenantProfile, question: str, evidence: list[RetrievedChunk],
                    today: date, max_attempts: int = 2) -> Generation:
    usage = Usage(model)
    if not evidence:
        return Generation("fallback", fallback_text(profile), needs_human=True,
                          escalation_reason="no approved evidence found", usage=usage)

    system = system_prompt(profile, today)
    user = user_prompt(question, evidence)
    contact = f"{profile.office_phone} {profile.emergency_phone or ''} {profile.business_hours}"
    texts = [c.text for c in evidence]
    problems: list[str] = []
    for attempt in range(1, max_attempts + 1):
        prompt = user if not problems else (
            f"{user}\n\nYour previous answer failed these checks; fix them using only the evidence:\n- "
            + "\n- ".join(problems))
        reply, call_usage = chat_json(client, model, system, prompt, SCHEMA, "grounded_answer")
        usage.add(call_usage)
        if reply["status"] == "insufficient_evidence":
            return Generation("fallback", fallback_text(profile), needs_human=True,
                              escalation_reason=reply["escalation_reason"] or "evidence did not answer the question",
                              attempts=attempt, usage=usage)
        problems = check_answer(reply["sentences"], reply["status"], texts, question, contact, reply["calculations"])
        if not problems:
            answer, cited = assemble(reply["sentences"])
            return Generation("answered", answer, reply["needs_human"], reply["escalation_reason"], cited=cited,
                              attempts=attempt, usage=usage)
    return Generation("fallback", fallback_text(profile), needs_human=True,
                      escalation_reason="answer failed grounding checks", problems=problems,
                      attempts=max_attempts, usage=usage)
