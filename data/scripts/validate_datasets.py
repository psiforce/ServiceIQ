"""Sanity-check the synthetic corpus and evaluation datasets.

Checks that manifests match files on disk, that every document ID referenced by
the golden set exists and is retrievable (approved, current, public), and
prints the category and split breakdown.

Usage:  python data/scripts/validate_datasets.py [--as-of YYYY-MM-DD]
"""
import argparse
import csv
import sys
from collections import Counter
from datetime import date
from pathlib import Path

DATA = Path(__file__).resolve().parent.parent


def load_manifests():
    docs = {}
    for manifest in sorted(DATA.glob('tenants/*/manifest.csv')):
        tenant_dir = manifest.parent
        with manifest.open(encoding='utf-8', newline='') as f:
            for row in csv.DictReader(f):
                if not (tenant_dir / row['file']).exists():
                    sys.exit(f"missing file for {row['document_id']}: {row['file']}")
                docs[row['document_id']] = row
    return docs


def retrievable(doc, as_of):
    expiry = doc['expiry_date']
    return (doc['approval_status'] == 'approved' and doc['access_scope'] == 'public'
            and date.fromisoformat(doc['effective_date']) <= as_of
            and (not expiry or as_of <= date.fromisoformat(expiry)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--as-of', default='2026-10-26', help='evaluation date used for effective/expiry checks')
    as_of = date.fromisoformat(ap.parse_args().as_of)

    docs = load_manifests()
    print(f'{len(docs)} documents across {len({d["tenant_id"] for d in docs.values()})} tenants')
    print('  by format:', dict(Counter(Path(d['file']).suffix for d in docs.values())))
    print('  not retrievable as of', as_of, ':', sorted(k for k, d in docs.items() if not retrievable(d, as_of)))

    errors = []
    with (DATA / 'eval' / 'golden_qa.csv').open(encoding='utf-8', newline='') as f:
        golden = list(csv.DictReader(f))
    for row in golden:
        for doc_id in filter(None, row['relevant_document_ids'].split(';')):
            if doc_id not in docs:
                errors.append(f"{row['qa_id']}: unknown document {doc_id}")
            elif not retrievable(docs[doc_id], as_of):
                errors.append(f"{row['qa_id']}: {doc_id} is not retrievable as of {as_of}")
            elif docs[doc_id]['tenant_id'] != 'cedar-ridge':
                errors.append(f"{row['qa_id']}: {doc_id} belongs to another tenant")
    ids = [r['qa_id'] for r in golden]
    if len(ids) != len(set(ids)):
        errors.append('duplicate qa_id values')

    print(f'\n{len(golden)} golden questions')
    print('  by category:', dict(Counter(r['category'] for r in golden)))
    print('  by expected behavior:', dict(Counter(r['expected_behavior'] for r in golden)))
    print('  by split:', dict(Counter(r['split'] for r in golden)))
    print('  critical:', sum(r['critical'] == 'yes' for r in golden))

    with (DATA / 'eval' / 'adversarial.csv').open(encoding='utf-8', newline='') as f:
        adversarial = list(csv.DictReader(f))
    print(f'\n{len(adversarial)} adversarial cases by type:', dict(Counter(r['attack_type'] for r in adversarial)))

    if errors:
        print('\nERRORS:\n  ' + '\n  '.join(errors))
        sys.exit(1)
    print('\nAll checks passed.')


if __name__ == '__main__':
    main()
