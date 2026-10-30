"""Input guard: decide what happens to a customer message before any retrieval or generation.

Actions (checked in this order, first match wins):
  emergency       -> fixed safety script + P1 escalation (no retrieval, no LLM answer)
  sensitive_data  -> message contains a card number or SSN: ask the customer not to share it
  refuse_unsafe   -> asks to bypass safety devices or do dangerous DIY work
  refuse          -> prompt extraction or abusive content
  off_topic       -> unrelated to the company and its services
  escalate        -> the customer asks for a person
  answer          -> continue to retrieval + grounded answer (M7); `injection_suspected` is passed
                     along so the answer step stays on policy
Rules run first and decide on their own when they fire; the LLM classifier adds coverage. If the
LLM call fails, the guard still works on rules alone (fail-safe, never fail-open on emergencies).
"""
import logging
from dataclasses import dataclass, field

from serviceiq.guardrails import rules
from serviceiq.guardrails.classifier import classify
from serviceiq.guardrails.pii import SENSITIVE, find_pii, redact
from serviceiq.guardrails.safety_scripts import emergency_message
from serviceiq.llm import Usage
from serviceiq.tenants import TenantProfile

log = logging.getLogger(__name__)

CANNED = {
    "sensitive_data": ("For your security, please don't share card numbers or Social Security numbers in chat. "
                       "Payments are never taken here: after your visit you'll get a secure payment link by email, "
                       "and financing applications go through the lender's secure application. Our team can help by "
                       "phone at {office_phone}."),
    "refuse_unsafe": ("I can't help with bypassing safety devices or other hazardous repairs. Those controls protect "
                      "you from fire and carbon monoxide. A trained technician should diagnose the problem; call "
                      "{company_name} at {office_phone} to schedule a visit."),
    "refuse": "I can't help with that. I'm here to answer questions about {company_name}'s heating and cooling services.",
    "off_topic": ("I can only help with questions about {company_name}'s heating and cooling services, such as "
                  "repairs, maintenance, pricing and policies."),
    "escalate": ("I'll connect you with our team. You can also call {company_name} at {office_phone} "
                 "({business_hours})."),
}


@dataclass
class GuardResult:
    action: str
    message: str | None = None          # response to show when the guard answers directly
    emergency_type: str | None = None
    intent: str = "hvac_question"       # classifier intent (rules fallback); service_request starts intake
    injection_suspected: bool = False
    pii_types: set[str] = field(default_factory=set)
    redacted_text: str = ""             # safe to log
    reasons: list[str] = field(default_factory=list)
    usage: Usage | None = None

    @property
    def escalation_priority(self) -> str | None:
        return {"emergency": "P1", "escalate": "P3"}.get(self.action)


def check_message(text: str, profile: TenantProfile, client=None, model: str = "gpt-4.1-mini") -> GuardResult:
    result = GuardResult(action="answer", pii_types=find_pii(text), redacted_text=redact(text))

    # 1) deterministic rules
    rule_emergency = rules.emergency_type(text)
    if rule_emergency:
        result.reasons.append(f"rule: emergency ({rule_emergency})")
    if rules.looks_like_injection(text):
        result.injection_suspected = True
        result.reasons.append("rule: instruction-override pattern")

    # 2) LLM classifier (skipped only when a rule already found an emergency: no need to wait)
    llm = {}
    if client is not None and not rule_emergency:
        try:
            llm, result.usage = classify(client, model, result.redacted_text)
            result.reasons.append(f"llm: {llm['intent']}/{llm['emergency']} - {llm['reason']}")
            result.injection_suspected |= llm["injection_attempt"]
        except Exception as exc:  # fail-safe: rules still protect emergencies
            log.warning("guard classifier failed, using rules only: %s", exc)
            result.reasons.append(f"llm unavailable ({type(exc).__name__}); rules only")

    # 3) decide
    emergency = rule_emergency or (llm.get("emergency") if llm.get("emergency") not in (None, "none") else None)
    intent = llm.get("intent") or ("service_request" if rules.wants_service_visit(text) else "hvac_question")
    result.intent = intent
    if emergency:
        result.action, result.emergency_type = "emergency", emergency
        result.message = emergency_message(emergency, profile)
    elif result.pii_types & SENSITIVE:
        result.action = "sensitive_data"
    elif rules.is_unsafe_request(text) or intent == "unsafe_request":
        result.action = "refuse_unsafe"
    elif rules.is_prompt_extraction(text) or intent in ("prompt_extraction", "abusive"):
        result.action = "refuse"
    elif intent == "off_topic":
        result.action = "off_topic"
    elif intent == "wants_human":
        result.action = "escalate"
    if result.action in CANNED:
        result.message = CANNED[result.action].format(**profile.__dict__)
    return result
