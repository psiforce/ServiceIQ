"""Deterministic, high-recall rules. They run first, cost nothing and never depend on the LLM.

The LLM classifier (classifier.py) adds coverage for paraphrases; the rules guarantee that the
obvious life-safety phrasings are always caught even if the LLM call fails.
"""
import re

I = re.IGNORECASE

# Order matters: the first matching emergency type wins (burning smell before smoke/fire so
# "burning smell but no smoke" gets the turn-it-off script, not the evacuate script).
EMERGENCY_RULES: list[tuple[str, re.Pattern]] = [
    ("gas", re.compile(r"\b(smell(s|ing|ed)?\s+(of\s+|like\s+)?(natural\s+)?gas|gas\s+(smell|leak|odou?r)|rotten[- ]eggs?)\b", I)),
    ("carbon_monoxide", re.compile(r"\b(carbon\s+monoxide|monoxide|co\s+(alarm|detector|monitor))\b", I)),
    ("unsafe_heating", re.compile(r"\b(oven|stove|grill|bbq|generator|charcoal)\b.{0,40}\b(heat|warm)", I)),
    ("burning_smell", re.compile(r"\b(burning\s+(smell|odou?r)|smells?\s+(like\s+)?(something\s+)?(is\s+)?burn(ing|t))", I)),
    ("fire_smoke", re.compile(r"\b(smoke|smoking|fire|flames?|sparks?|sparking)\b", I)),
    ("electrical_water", re.compile(
        r"\b(water|leak\w*|pour\w*|flood\w*|wet)\b.{0,50}\b(outlet|electric\w*|wiring|breaker|panel)\b"
        r"|\b(outlet|electric\w*|wiring|breaker|panel)\b.{0,50}\b(water|wet|flood\w*)\b", I)),
]
NO_HEAT = re.compile(r"\b(no\s+heat|heat(ing)?\s+(is\s+)?(out|off|not\s+working|stopped)|furnace\s+(is\s+)?(out|dead|died|not\s+working))\b", I)
NO_COOL = re.compile(r"\b(no\s+(ac|a/c|air|cooling)|ac\s+(is\s+)?(out|dead|not\s+working)|air\s+conditioning\s+(is\s+)?out)\b", I)
VULNERABLE = re.compile(r"\b(elderly|baby|infant|newborn|oxygen|pregnant|medical|disabled|senior)\b", I)
TEMPERATURE = re.compile(r"(-?\d{1,3})\s*(degrees|°|deg\b|f\b)", I)
EXTREME_WORDS_COLD = re.compile(r"\b(freezing|below\s+zero|sub-?zero)\b", I)
EXTREME_WORDS_HOT = re.compile(r"\b(heat\s+wave|scorching|over\s+100)\b", I)

UNSAFE_REQUEST = re.compile(
    r"\b(bypass|jump(er)?|disable|defeat|override|disconnect)\b.{0,40}"
    r"\b(limit\s+switch|high[- ]limit|safety|pressure\s+switch|flame\s+(sensor|rollout)|rollout|interlock|float\s+switch)\b", I)

# Wants a visit (Phase 2 intake); used when the LLM classifier is unavailable
SERVICE_REQUEST = re.compile(
    r"\b(send|get|need|want|have)\s+(a\s+|someone|somebody|a\s+tech\w*|someone\s+to)\b.{0,30}"
    r"\b(out|over|come|look|check|fix|repair|tech\w*)\b"
    r"|\b(schedule|book|set\s+up|make)\s+(a\s+|an\s+)?(service|repair|appointment|visit|tune[- ]?up|maintenance|call)\b"
    r"|\b(can|could)\s+(you|someone|somebody)\s+come\b|\bcome\s+out\b|\bservice\s+call\b", I)

PROMPT_EXTRACTION = re.compile(
    r"\b(system\s+prompt|developer\s+mode|(print|show|reveal|repeat|translate|tell\s+me)\s+(me\s+)?your\s+"
    r"(instructions|prompt|rules|system\s+message))\b", I)
INJECTION = re.compile(
    r"\b(ignore|disregard|forget)\s+(all\s+|any\s+|the\s+|your\s+)*(previous|prior|above|earlier|your)?\s*"
    r"(instructions|rules|prompts?|guidelines)"
    r"|\byou\s+are\s+now\b|\bjailbreak\b|\bpretend\s+(you'?re|you\s+are|to\s+be)\b|^\s*system\s*:"
    r"|\b(respond|reply|answer)\s+only\s+with\b|\bfrom\s+now\s+on\s+(respond|reply|answer)\b"
    r"|\bwithout\s+(any\s+)?citations\b", I | re.M)


def _extreme_temperature(text: str, cold: bool) -> bool:
    if (EXTREME_WORDS_COLD if cold else EXTREME_WORDS_HOT).search(text) or VULNERABLE.search(text):
        return True
    temps = [int(m.group(1)) for m in TEMPERATURE.finditer(text)]
    return any(t <= 40 for t in temps) if cold else any(t >= 90 for t in temps)


def emergency_type(text: str) -> str | None:
    for kind, pattern in EMERGENCY_RULES:
        if pattern.search(text):
            return kind
    if NO_HEAT.search(text) and _extreme_temperature(text, cold=True):
        return "no_heat_extreme"
    if NO_COOL.search(text) and _extreme_temperature(text, cold=False):
        return "no_cool_extreme"
    return None


def is_unsafe_request(text: str) -> bool:
    return bool(UNSAFE_REQUEST.search(text))


def wants_service_visit(text: str) -> bool:
    return bool(SERVICE_REQUEST.search(text))


def is_prompt_extraction(text: str) -> bool:
    return bool(PROMPT_EXTRACTION.search(text))


def looks_like_injection(text: str) -> bool:
    return bool(INJECTION.search(text)) or is_prompt_extraction(text)
