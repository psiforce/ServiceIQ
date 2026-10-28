"""Deterministic eligibility check: is the ZIP served, and is the equipment something the company works on?

Reads the tenant's current approved documents (the registry when the database has one, else the manifest):
the service-area CSV (zip_code, coverage, trip_fee_usd, notes) and, when present, the brands-serviced CSV
(brand, equipment_types, repair_and_maintenance, installation, notes). No language model is involved.
"""
import csv
from dataclasses import dataclass
from datetime import date

from sqlalchemy.orm import Session

from serviceiq.ingestion.manifest import ManifestEntry, read_manifest
from serviceiq.storage import registry

# How the brands file names each equipment type; most specific first ("geothermal heat pump" is not a heat pump)
EQUIPMENT_WORDS = [("geothermal", "geothermal"), ("oil_furnace", "oil furnace"), ("boiler", "boiler"),
                   ("window_ac", "window"), ("water_heater", "water heater"), ("ductless", "ductless"),
                   ("heat_pump", "heat pump"), ("furnace", "furnace"), ("central_ac", "central ac")]


def equipment_type_of(label: str) -> str | None:
    label = label.lower()
    return next((kind for kind, words in EQUIPMENT_WORDS if words in label), None)


@dataclass
class Eligibility:
    ok: bool
    reason: str | None = None       # why not eligible (shown to the customer)
    note: str | None = None         # eligible, but worth telling the customer (trip fee, brand to confirm)
    coverage: str | None = None     # Core | Extended
    trip_fee_usd: int | None = None


def _documents(session: Session | None, data_dir, tenant_id: str, as_of: date) -> list[ManifestEntry]:
    if session is not None and registry.has_registry(session, tenant_id):
        return registry.current_entries(session, tenant_id)
    return [e for e in read_manifest(data_dir, tenant_id) if e.approval_status == "approved"
            and e.effective_date <= as_of.isoformat() and (not e.expiry_date or as_of.isoformat() <= e.expiry_date)]


def _rows(entry: ManifestEntry) -> list[dict]:
    with open(entry.path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def check(session: Session | None, data_dir, tenant_id: str, company: str, zip_code: str | None,
          equipment_type: str | None, brand: str | None, as_of: date) -> Eligibility:
    docs = _documents(session, data_dir, tenant_id, as_of)
    result = Eligibility(ok=True)

    area = next((e for e in docs if e.document_type == "service_area" and e.path.suffix == ".csv"), None)
    if zip_code and area is not None:
        row = next((r for r in _rows(area) if r.get("zip_code", "").strip() == zip_code), None)
        if row is None:
            return Eligibility(ok=False, reason=f"{company} doesn't serve ZIP {zip_code}, so I can't set up a visit "
                                                "there. The office may be able to suggest someone nearby.")
        result.coverage = row.get("coverage")
        result.trip_fee_usd = int(float(row.get("trip_fee_usd") or 0))
        if result.trip_fee_usd:
            extra = f" {row['notes'].strip()}." if (row.get("notes") or "").strip() else ""
            result.note = (f"{zip_code} is in our {result.coverage.lower()} service area, so a "
                           f"${result.trip_fee_usd} trip fee applies.{extra}")

    brands = next((e for e in docs if e.path.suffix == ".csv" and e.document_type == "service_catalog"
                   and "brand" in e.document_id), None)
    if equipment_type and brands is not None:
        rows = _rows(brands)
        excluded = next((r for r in rows if r["brand"].lower() == "any brand"
                         and equipment_type_of(r["equipment_types"]) == equipment_type
                         and r["repair_and_maintenance"].lower() == "no"), None)
        if excluded is not None:
            what = excluded["equipment_types"].split("/")[0].strip().lower()
            notes = excluded["notes"].strip().removeprefix("Not serviced.").strip()
            return Eligibility(ok=False, reason=f"{company} doesn't service {what} systems." + (f" {notes}" if notes
                                                                                                else ""))
        if brand:
            known = next((r for r in rows if r["brand"].lower() == brand.strip().lower()), None)
            if known is None:
                note = f"The office will confirm they can work on {brand} equipment."
                result.note = f"{result.note} {note}" if result.note else note
            elif known["repair_and_maintenance"].lower() == "no":
                return Eligibility(ok=False, reason=f"{company} doesn't service {brand} equipment.")
    return result
