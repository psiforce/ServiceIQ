"""Detect and redact personal and payment data before anything is logged or traced."""
import re

PATTERNS = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    # not joined to letters or digits, directly or by a dash (UUID groups like "04546345-7962-4013-a71c" pass Luhn)
    "card": re.compile(r"(?<![0-9A-Za-z])(?<![0-9A-Za-z]-)(?:\d[ -]?){12,18}\d(?![0-9A-Za-z]|-[0-9A-Za-z])"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
    # not inside a longer token: a run of digits in an ID (e.g. a UUID "…-2998268088f2") is not a phone number
    "phone": re.compile(r"(?<![0-9A-Za-z])(?:\+?1[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}(?![0-9A-Za-z])"),
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
