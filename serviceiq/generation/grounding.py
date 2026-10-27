"""Deterministic post-generation checks. A failed check means the answer is not shown (fail closed).

The model returns its answer as sentences, each with the evidence ids that support it. Checks:
1. An answered response has at least one sourced sentence, and every source id refers to a provided block.
2. Every sentence that contains a number has at least one source.
3. Every number in a sentence appears in that sentence's own sources, in the question, in the company
   contact details, or is the verified result of a declared calculation whose inputs come from the
   evidence. This is what stops invented prices, fees, percentages and warranty lengths.
"""
import ast
import operator
import re
from decimal import Decimal, InvalidOperation

MARKER = re.compile(r"\[(\d+)\]")
NUMBER = re.compile(r"(?<![\w.])\$?\d{1,3}(?:,\d{3})+(?:\.\d+)?|(?<![\w.])\$?\d+(?:\.\d+)?")
CALC_CONSTANTS = {Decimal(1), Decimal(100)}
OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv}


def normalize_number(raw: str) -> Decimal | None:
    try:
        return Decimal(raw.replace("$", "").replace(",", "")).normalize()
    except InvalidOperation:
        return None


def numbers_in(text: str) -> set[Decimal]:
    text = MARKER.sub(" ", text)
    return {n for n in (normalize_number(m.group()) for m in NUMBER.finditer(text)) if n is not None}


def assemble(sentences: list[dict]) -> tuple[str, list[int]]:
    """Join sentences into answer text with [n] markers; return the text and evidence ids in citation order."""
    order: list[int] = []
    parts = []
    for s in sentences:
        text = MARKER.sub("", s["text"]).strip()
        ids = list(dict.fromkeys(s["sources"]))
        for n in ids:
            if n not in order:
                order.append(n)
        markers = "".join(f"[{n}]" for n in ids)
        if markers and text[-1:] in ".!?":
            text = f"{text[:-1]} {markers}{text[-1]}"  # "costs $89 [1]." rather than "costs $89.[1]"
        elif markers:
            text = f"{text} {markers}"
        parts.append(text)
    return " ".join(p for p in parts if p), order


def _evaluate(expression: str) -> tuple[Decimal, set[Decimal]]:
    """Safely evaluate + - * / arithmetic; return the result and the operands used."""
    operands: set[Decimal] = set()

    def walk(node):
        if isinstance(node, ast.Expression):
            return walk(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            value = Decimal(str(node.value)).normalize()
            operands.add(value)
            return value
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            return OPS[type(node.op)](walk(node.left), walk(node.right))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return -walk(node.operand)
        raise ValueError(f"unsupported expression: {expression}")

    cleaned = expression.replace("$", "").replace(",", "").replace("%", "")
    return walk(ast.parse(cleaned, mode="eval")).normalize(), operands


def check_answer(sentences: list[dict], status: str, evidence_texts: list[str], question: str, contact_text: str,
                 calculations: list[dict]) -> list[str]:
    """Return a list of problems; an empty list means the answer passed."""
    problems = []
    valid = range(1, len(evidence_texts) + 1)
    if status == "answered" and not any(s["sources"] for s in sentences):
        problems.append("answer has no sourced sentences")

    always_allowed = numbers_in(question) | numbers_in(contact_text)
    all_evidence_numbers = numbers_in(" ".join(evidence_texts))
    calc_results: set[Decimal] = set()
    for calc in calculations:
        try:
            result, operands = _evaluate(calc["expression"])
        except (ValueError, SyntaxError, ZeroDivisionError, InvalidOperation):
            problems.append(f"calculation could not be checked: {calc['expression']}")
            continue
        unknown = operands - all_evidence_numbers - always_allowed - CALC_CONSTANTS
        claimed = normalize_number(calc["result"])
        if unknown:
            problems.append(f"calculation uses numbers not in the evidence: {sorted(map(str, unknown))}")
        elif claimed is None or abs(result - claimed) > Decimal("0.01"):
            problems.append(f"calculation is wrong: {calc['expression']} = {result}, not {calc['result']}")
        else:
            calc_results |= {claimed, result.quantize(Decimal("0.01")).normalize()}

    for s in sentences:
        text, sources = s["text"], s["sources"]
        invalid = [n for n in sources if n not in valid]
        if invalid:
            problems.append(f"sources refer to missing evidence {invalid}: '{text}'")
        numbers = numbers_in(text)
        if numbers and not sources:
            problems.append(f"sentence with numbers has no source: '{text}'")
            continue
        own = numbers_in(" ".join(evidence_texts[n - 1] for n in sources if n in valid))
        unsupported = sorted(str(n) for n in numbers - own - always_allowed - calc_results)
        if unsupported:
            problems.append(f"numbers {unsupported} are not in the sources of: '{text}'")
    return problems
