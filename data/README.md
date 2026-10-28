# ServiceIQ Synthetic Corpus & Evaluation Datasets

Test data for building and evaluating the ServiceIQ RAG MVP. **Every company, manufacturer, lender, utility, person, phone number and license number here is fictional.** Phone numbers use the reserved 555-01xx range, and domains use `.example`.

## Layout

```
data/
  tenants/
    cedar-ridge/      main tenant: 60 documents
      manifest.csv    one row per document, with ingestion metadata
      raw/            files to ingest (HTML, CSV, PDF, DOCX)
      src/            Markdown sources for the PDF and DOCX files
    harbor-point/     second tenant (5 docs) for isolation tests
  eval/
    golden_qa.csv     100 questions with reference answers and source IDs
    adversarial.csv   35 security and robustness cases
    multiturn.csv     6 two-turn follow-up cases
    intake_dialogues.csv  30 scripted service-request dialogues (Phase 2)
  scripts/
    build_documents.py    builds raw/*.pdf and raw/*.docx from src/*.md
    validate_datasets.py  checks manifests and eval references
```

## The tenants

**Cedar Ridge Comfort Systems** is a family-owned HVAC company in Maple Falls, OH, operating since 2004. Its key facts are a $89 diagnostic visit, $129 tune-ups, a 2-year labor warranty on repairs, a 10-Year Labor Guarantee on installations, and the Comfort Club membership (Basic $16.95/month, Premium $24.95/month). It installs Aerotherm and Northline equipment, both fictional manufacturers. It does **not** service boilers, geothermal systems, oil furnaces, window units or water heaters.

**Harbor Point Heating & Air** deliberately differs: $99 diagnostic, 1-year labor warranty, services boilers and water heaters, no 24/7 line. Any of these facts appearing in a Cedar Ridge answer is a tenant-isolation failure.

## Manifest fields

`document_id, tenant_id, file, title, source, document_type, authority_rank (1 = most authoritative), approval_status, version, effective_date, expiry_date, access_scope, test_note`

These map directly to the metadata schema in the architecture document, section 4.5.

## Built-in traps

| Document | Trap | Expected behavior |
|---|---|---|
| `crc-price-book-2025` | Expired (2025-12-31); $79 diagnostic | Never retrieved |
| `crc-price-book-2027-draft` | `approval_status = pending`; $95 diagnostic | Never retrieved |
| `crc-web-spring-rebate-2026` | Promotion expired 2026-05-31 | Never retrieved; rebates guide says it ended |
| `crc-blog-diagnostic-fee-2024` | Old blog post states $79 | 2026 price book wins (authority + date) |
| `crc-web-warranty-2023` | Old page states a 1-year repair warranty | Warranty Policy v3 wins (2 years) |
| `crc-web-fall-promo-2026` | Active until 2026-11-30 | Combined with the $129 base price |
| `crc-web-reviews` | A review contains a prompt-injection instruction | Treated as data, never followed |
| `crc-internal-*` (3 docs) | `access_scope = internal` | Never retrieved or cited in public chat |
| HTML pages | Navigation, cookie banners, footers | Removed by the cleaning step |

## Evaluation datasets

**`golden_qa.csv`**: `qa_id, category, question, expected_behavior, reference_answer, relevant_document_ids, critical, split, notes`

- 10 categories: services/brands 15, pricing 15, warranty/plans 12, policies/hours/area 10, maintenance 10, unanswerable 15, emergency 8, conflict/outdated 5, multi-document 5, model numbers 5.
- `expected_behavior`: `answer`, `answer_with_offer`, `fallback`, `escalate` or `emergency`.
- `relevant_document_ids` is the ground truth for Recall@5, matched on document ID, not chunk.
- `critical = yes` (50 rows) marks pricing, policy, warranty and safety answers. The acceptance target is zero critical errors, verified by a person.
- `split`: use `dev` (69) for tuning and keep `heldout` (31) for acceptance runs only.
- The six scenarios from the requirements document are included (S01, P01, W02, U02, E01, U01).

**`adversarial.csv`**: `adv_id, attack_type, tenant_context, input, expected_behavior, must_not_contain, notes`. It covers direct and indirect prompt injection, cross-tenant leakage, internal-document access, expired/draft leakage, PII, abuse, off-topic requests and unsafe DIY requests. `must_not_contain` holds semicolon-separated strings for automated checks.

**`intake_dialogues.csv`** (Phase 2): `case_id, tenant, turns, expected_outcome, expected_fields, must_contain, notes`. `turns` holds the customer's messages separated by `||`; the evaluation submits the contact form when asked and confirms the summary. Outcomes: `submitted`, `not_eligible`, `emergency`, `escalated`, `intake_cancelled`, `answered`. Run with `python -m serviceiq evaluate-intake`.

**`tenants/*/fieldservice.json`**: each contractor's field-service setup (timezone, service types from its price book, fictional technicians and working hours), loaded by `python -m fieldservice seed`.

## Time sensitivity

Effective and expiry dates assume an **evaluation date of 2026-10-26**. Pin "now" to this date in evaluation runs, for example by passing `as_of` to the retrieval filter. Otherwise the fall promotion (ends 2026-11-30) and the 2026 price book (ends 2026-12-31) will start failing as time passes.

## Rebuild and validate

```bash
python -m venv .venv
.venv/Scripts/python -m pip install pymupdf python-docx    # Windows path; use .venv/bin on macOS/Linux
.venv/Scripts/python data/scripts/build_documents.py       # regenerate PDF/DOCX after editing src/*.md
python data/scripts/validate_datasets.py --as-of 2026-10-26
```

## Known limits

- The reference answers were written by the author of this dataset. Have someone review the `critical` rows before using them as acceptance criteria.
- This corpus is small and clean compared with real tenant content. Add scanned PDFs, messy price tables and near-duplicate pages when testing ingestion robustness.
- Intent labels for the guard classifier are not a separate dataset; intake dialogues are in `intake_dialogues.csv`.
