"""Readers for the evaluation datasets in data/eval."""
import csv
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class GoldenQuestion:
    qa_id: str
    category: str
    question: str
    expected_behavior: str
    reference_answer: str
    relevant_document_ids: tuple[str, ...]
    critical: bool
    split: str
    notes: str


def load_golden(data_dir: Path, split: str = "all") -> list[GoldenQuestion]:
    with (data_dir / "eval" / "golden_qa.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    questions = [
        GoldenQuestion(
            qa_id=r["qa_id"], category=r["category"], question=r["question"],
            expected_behavior=r["expected_behavior"], reference_answer=r["reference_answer"],
            relevant_document_ids=tuple(filter(None, r["relevant_document_ids"].split(";"))),
            critical=r["critical"] == "yes", split=r["split"], notes=r["notes"],
        )
        for r in rows
    ]
    return [q for q in questions if split == "all" or q.split == split]
