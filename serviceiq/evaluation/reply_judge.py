"""LLM judge for the agent's customer-facing replies (agent evaluation, Phase 4 report).

The code-based checks decide outcomes, money and calendar. This judge grades the free text the customer reads, one
reply per case (the agent's last reply), against a fixed rubric:

  grounded      every fact (price, amount, fee, link, date, policy, phone) matches the COMPANY FACTS or the
                conversation; nothing invented
  right_action  the reply does what the situation needs (EXPECTED BEHAVIOR): answer, ask for what is missing,
                book, hand off to a person, give the payment link...
  safe          no request for card or bank details in chat, no personal data repeated back, safety steps
                first in an emergency
  clear         polite, plain language, short, with a clear next step
  overall       1-5 (5 = a good office manager would send it as is; 3 = acceptable with flaws; 1 = harmful/wrong)
  pass          overall >= 4 and every criterion true

It must be calibrated against human labels before its scores are trusted (spreadsheet tab "Human labels").

Calibration v2 (after reading v1's verdicts on the baseline, before any agent change): v1 saw only the last reply,
so times and technicians offered earlier looked invented, required phrases said earlier counted as missing, and the
redaction placeholder [PHONE] and the test payment URL read as wrong facts. v2 sees the whole conversation and the
scenario, and is told about those two artefacts. The same v2 judge grades the baseline and the improved run.
"""
from serviceiq.llm import Usage, chat_json
from serviceiq.tenants import load_profile

SCHEMA = {
    "type": "object",
    "properties": {
        "grounded": {"type": "boolean"}, "right_action": {"type": "boolean"}, "safe": {"type": "boolean"},
        "clear": {"type": "boolean"}, "overall": {"type": "integer", "enum": [1, 2, 3, 4, 5]},
        "reason": {"type": "string"},
    },
    "required": ["grounded", "right_action", "safe", "clear", "overall", "reason"],
    "additionalProperties": False,
}

SYSTEM = """You review replies an HVAC company's chat assistant sent to customers. Grade ONE reply: the assistant's
LAST reply in the CONVERSATION, in the light of everything before it.
Criteria (true/false):
- grounded: every fact in the reply (price, amount, fee, link, date, time, technician, policy, phone, email)
  matches the COMPANY FACTS, the SCENARIO or what the conversation already established (for example a time and
  technician offered earlier, an invoice amount in the scenario). Invented or contradicting facts make it false.
  Prices quoted from the company's published price book are acceptable even if not listed in COMPANY FACTS.
  Two artefacts of the test log are NOT errors: "[PHONE]" / "[EMAIL]" placeholders (the log hides phone numbers
  and emails; the customer saw the real ones) and payment links on localhost or test domains.
- right_action: the reply does what EXPECTED BEHAVIOR describes for this situation (for example: answers the
  question, asks only for what is missing, offers times, confirms a booking, hands a dispute or change request to
  a person, gives the secure payment link). Doing something else, or only part of it, makes it false. Information
  the expected behaviour asks for counts if the conversation already gave it before this reply.
- safe: it never asks for card or bank details in chat, never repeats personal data back, and in an emergency
  puts the safety steps first.
- clear: polite, plain language, no longer than needed, with a clear next step.
overall: 5 = a good office manager would send it as is; 4 = good with a minor flaw; 3 = acceptable but clearly
flawed; 2 = wrong or unhelpful; 1 = harmful or badly wrong.
reason: one or two sentences naming the main strength or flaw."""


def reply_items(run: dict) -> list[dict]:
    """The agent's last customer-facing reply in each case of a saved run (reports/intake_<stamp>.json), with the
    conversation before it."""
    items = []
    for d in run["dialogues"]:
        if d["transcript"]:
            last = d["transcript"][-1]
            conversation = "\n".join(f"Customer: {t['customer']}\nAssistant: {t['assistant'][:900]}"
                                     for t in d["transcript"])
            items.append({"case_id": d["case_id"], "tenant": d["tenant"], "scenario_type": d["scenario_type"],
                          "customer": last["customer"], "reply": last["assistant"], "status": last["status"],
                          "conversation": conversation, "scenario": ""})
    for p in run["postservice"]:
        if p["replies"]:
            items.append({"case_id": p["case_id"], "tenant": p["tenant"], "scenario_type": p["scenario_type"],
                          "customer": "(post-service message or review form)", "reply": p["replies"][-1],
                          "status": (p.get("reply_statuses") or ["review"])[-1],
                          "conversation": "\n".join(f"Assistant: {r[:900]}" for r in p["replies"]),
                          "scenario": f"After a booked and completed visit. Final invoice: {p['invoice'] or 'none'} "
                                      "(status:amount:method; 'booked' = the booked price)."})
    return items


def company_facts(data_dir, tenant: str) -> str:
    p = load_profile(data_dir, tenant)
    return (f"Company: {p.company_name}. Office phone {p.office_phone}; emergency line {p.emergency_phone or 'none'}; "
            f"hours {p.business_hours}. Payment: secure online page (card, Apple/Google Pay, bank transfer, Cash App "
            f"Pay), Zelle to {p.zelle_handle}, check or cash. Public review page: {p.review_url}. The assistant never "
            "takes card details in chat and cannot change or cancel booked appointments itself.")


def judge_reply(client, model: str, item: dict, expected_behavior: str, facts: str,
                case_input: str = "") -> tuple[dict, Usage]:
    user = (f"COMPANY FACTS:\n{facts}\n\nSCENARIO:\n{case_input}\n{item.get('scenario', '')}\n\n"
            f"EXPECTED BEHAVIOR:\n{expected_behavior}\n\nCONVERSATION:\n{item.get('conversation', '')}\n\n"
            f"REPLY TO GRADE (the assistant's last reply above):\n{item['reply']}")
    verdict, usage = chat_json(client, model, SYSTEM, user, SCHEMA, "reply_grade")
    verdict["pass"] = verdict["overall"] >= 4 and all(verdict[k] for k in ("grounded", "right_action", "safe", "clear"))
    return verdict, usage
