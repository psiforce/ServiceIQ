"""Tenant profiles: company details used in safety, fallback and escalation messages."""
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class TenantProfile:
    tenant_id: str
    company_name: str
    office_phone: str
    emergency_phone: str | None
    business_hours: str
    escalation_email: str
    # Phase 4: payment methods offered on the Stripe page, Zelle details for invoices, the public review page
    payment_methods: tuple[str, ...] = ("card", "us_bank_account", "cashapp")
    zelle_handle: str | None = None
    review_url: str | None = None


@lru_cache
def load_profile(data_dir: Path, tenant_id: str) -> TenantProfile:
    path = data_dir / "tenants" / tenant_id / "profile.json"
    if not path.exists():
        raise ValueError(f"unknown tenant: {tenant_id}")
    return TenantProfile(**json.loads(path.read_text(encoding="utf-8")))
