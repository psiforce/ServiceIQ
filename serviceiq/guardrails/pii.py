"""Detect and redact personal and payment data before anything is logged or traced."""
import re

PATTERNS = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "card": re.compile(r"\b(?:\d[ -]?){12,18}\d\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
    "phone": re.compile(r"(?<!\d)(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?!\d)"),
}
SENSITIVE = {"ssn", "card"}  # never acceptable in chat; customers are told not to share them


def _luhn_ok(number: str) -> bool:
    digits = [int(d) for d in re.sub(r"\D", "", number)][::-1]
    total = sum(d if i % 2 == 0 else (d * 2 - 9 if d * 2 > 9 else d * 2) for i, d in enumerate(digits))
    return 13 <= len(digits) <= 19 and total % 10 == 0


def find_pii(text: str) -> set[str]:
    found = set()
    for kind, pattern in PATTERNS.items():
        for match in pattern.finditer(text):
            if kind != "card" or _luhn_ok(match.group()):
                found.add(kind)
    return found


def redact(text: str) -> str:
    """Replace PII with [TYPE] placeholders. SSNs and cards first so phone matching can't split them."""
    for kind in ("ssn", "card", "email", "phone"):
        def replace(match, kind=kind):
            return f"[{kind.upper()}]" if kind != "card" or _luhn_ok(match.group()) else match.group()
        text = PATTERNS[kind].sub(replace, text)
    return text
