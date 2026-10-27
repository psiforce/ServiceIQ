"""LLM judges used by the evaluation harness.

faithfulness_judge - follows the Ragas faithfulness definition: split the answer into atomic factual
    claims and decide for each whether it can be inferred from the retrieved context. It also checks,
    per sentence, whether the passages that sentence cites support it (citation accuracy).
correctness_judge  - compares the response with the human reference answer and the expected behavior,
    and flags critical errors (wrong prices/policies/warranty terms, asserted coverage, confirmed
    availability, invented competitor information, unsafe instructions).
Automated judges must be calibrated against human review; critical rows are always listed for a person.
"""
import re

from serviceiq.llm import Usage, chat_json

MARKER = re.compile(r"\[(\d+)\]")

FAITHFULNESS_SCHEMA = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "sentence_id": {"type": "integer"},
                    "claims": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {"claim": {"type": "string"}, "supported": {"type": "boolean"}},
                            "required": ["claim", "supported"],
                            "additionalProperties": False,
                        },
                    },
                    "cited_passages_support_sentence": {"type": "boolean"},
                },
                "required": ["sentence_id", "claims", "cited_passages_support_sentence"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["sentences"],
    "additionalProperties": False,
}

FAITHFULNESS_SYSTEM = """You grade whether a customer-service answer is faithful to its context.
For each numbered answer sentence:
1. Break it into atomic factual claims about the company, its services, prices, policies or equipment.
   Ignore conversational phrases, offers to help, statements about the assistant itself ("I can't check the
   schedule") and generic advice to contact the company or a professional.
   For each claim, supported = true only if it can be directly inferred from the CONTEXT passages or the
   COMPANY CONTACT details. Do not use outside knowledge.
2. cited_passages_support_sentence = true if the passages the sentence cites (listed after it) together
   support everything the sentence states. A sentence that cites nothing gets true only if it states no facts.
Return one entry per sentence, in order."""

CORRECTNESS_SCHEMA = {
    "type": "object",
    "properties": {
        "correct": {"type": "boolean"},
        "behavior_ok": {"type": "boolean"},
        "critical_error": {"type": "boolean"},
        "explanation": {"type": "string"},
    },
    "required": ["correct", "behavior_ok", "critical_error", "explanation"],
    "additionalProperties": False,
}

CORRECTNESS_SYSTEM = """You grade an HVAC company's chat assistant against a human-approved reference.

Expected behaviors:
- answer: answer the question. correct = consistent with the reference's key facts (prices, terms, yes/no);
  extra accurate detail is fine; missing minor details is fine; any wrong key fact is not.
- answer_with_offer: answer and offer verification or human follow-up; must not assert unverifiable facts
  (e.g. that the customer's specific unit is covered).
- fallback: say the information can't be confirmed (true partial information is fine); must not invent facts.
- escalate: give only published information, make no binding commitment, and offer a person or quote.
- emergency: put safety steps first (leave, call 911 / the gas utility as appropriate); no troubleshooting.

behavior_ok = the response follows the expected behavior.
correct = behavior_ok and the facts stated agree with the reference (for fallback: nothing false is stated).
COMPANY SOURCES are the approved documents the assistant cited. A statement supported by them is not an error
even if the reference does not mention it; only statements that contradict the reference or the sources, or
that no source supports, can be errors. Omitting a key fact the reference requires makes the answer incorrect,
but it is a critical error only if the omission is a safety step in an emergency or a safety warning that
goes with do-it-yourself steps.
critical_error = the response states a wrong price, fee, discount, warranty term or company policy, asserts that a
specific customer's equipment is covered, confirms appointment availability, invents competitor information,
gives unsafe instructions, or omits safety steps in an emergency. Wording differences are not errors.
explanation: one or two sentences."""


def split_sentences(answer: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", answer.strip()) if s]


def faithfulness_judge(client, model: str, answer: str, context: list[str], cited_texts: dict[int, str],
                       contact: str) -> tuple[dict, Usage]:
    """Return {"claims": n, "supported": n, "cited_sentences": n, "cited_supported": n, "unsupported": [...]}."""
    sentences = split_sentences(answer)
    context_block = "\n\n".join(f"<passage id=\"C{i}\">\n{t}\n</passage>" for i, t in enumerate(context, 1))
    lines = []
    for i, s in enumerate(sentences, 1):
        cites = [int(n) for n in MARKER.findall(s)]
        cited = "; ".join(f"[{n}] = {' '.join(cited_texts.get(n, '').split())[:900]}" for n in cites) or "nothing"
        lines.append(f"S{i}: {MARKER.sub('', s).strip()}\n    cites: {cited}")
    user = (f"CONTEXT:\n{context_block}\n\nCOMPANY CONTACT: {contact}\n\nANSWER SENTENCES:\n" + "\n".join(lines))
    reply, usage = chat_json(client, model, FAITHFULNESS_SYSTEM, user, FAITHFULNESS_SCHEMA, "faithfulness")
    result = {"claims": 0, "supported": 0, "cited_sentences": 0, "cited_supported": 0, "unsupported": []}
    for entry in reply["sentences"]:
        idx = entry["sentence_id"] - 1
        for claim in entry["claims"]:
            result["claims"] += 1
            result["supported"] += claim["supported"]
            if not claim["supported"]:
                result["unsupported"].append(claim["claim"])
        if 0 <= idx < len(sentences) and MARKER.search(sentences[idx]):
            result["cited_sentences"] += 1
            result["cited_supported"] += entry["cited_passages_support_sentence"]
    return result, usage


def correctness_judge(client, model: str, question: str, expected_behavior: str, reference: str, notes: str,
                      status: str, answer: str, sources: list[str] = ()) -> tuple[dict, Usage]:
    source_block = "\n".join(f"- {' '.join(s.split())[:900]}" for s in sources) or "- none"
    user = (f"QUESTION: {question}\nEXPECTED BEHAVIOR: {expected_behavior}\nREFERENCE ANSWER: {reference}\n"
            f"GRADING NOTES: {notes or '-'}\n\nCOMPANY SOURCES CITED BY THE ASSISTANT:\n{source_block}\n\n"
            f"ASSISTANT STATUS: {status}\nASSISTANT RESPONSE: {MARKER.sub('', answer)}")
    return chat_json(client, model, CORRECTNESS_SYSTEM, user, CORRECTNESS_SCHEMA, "correctness")
