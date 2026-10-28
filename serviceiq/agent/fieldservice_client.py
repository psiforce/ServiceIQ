"""Adapter to the field-service platform (the `fieldservice` package): customers and equipment.

ServiceIQ keeps only the customer's ID in that platform; names, phones and addresses live there. Every write
carries an Idempotency-Key, so the single retry on a network error or 5xx can never create a duplicate.
"""
import json
import time
import uuid
from typing import Protocol

import httpx

from serviceiq.config import PROJECT_ROOT, Settings

EQUIPMENT_NAMES = {"central_ac": "Central AC", "furnace": "Furnace", "heat_pump": "Heat pump",
                   "ductless": "Ductless mini-split", "boiler": "Boiler", "water_heater": "Water heater"}


class FieldServiceError(RuntimeError):
    pass


class FieldService(Protocol):
    def find_or_create_customer(self, tenant_id: str, contact: dict, idempotency_key: str) -> str: ...
    def add_equipment(self, tenant_id: str, customer_ref: str, draft: dict, idempotency_key: str) -> str | None: ...
    def get_customer(self, tenant_id: str, customer_ref: str) -> dict | None: ...


class HttpFieldService:
    def __init__(self, base_url: str, keys: dict[str, str], http=None, id_token: bool = False):
        if http is None:
            from serviceiq.api.client import _IdTokenAuth
            http = httpx.Client(base_url=base_url.rstrip("/"), timeout=15,
                                auth=_IdTokenAuth(base_url.rstrip("/")) if id_token else None)
        self.http, self.keys = http, keys

    def _request(self, tenant_id: str, method: str, path: str, idempotency_key: str | None = None, **kwargs):
        if tenant_id not in self.keys:
            raise FieldServiceError(f"no field-service API key for tenant {tenant_id}")
        headers = {"X-FSP-Key": self.keys[tenant_id]} | ({"Idempotency-Key": idempotency_key} if idempotency_key else {})
        for attempt in (1, 2):  # one retry on transport errors and server errors
            try:
                response = self.http.request(method, path, headers=headers, **kwargs)
            except httpx.TransportError as exc:
                if attempt == 2:
                    raise FieldServiceError(f"field-service platform unreachable: {type(exc).__name__}") from exc
                time.sleep(0.5)
                continue
            if response.status_code >= 500 and attempt == 1:
                time.sleep(0.5)
                continue
            return response
        raise FieldServiceError(f"field-service platform error {response.status_code}")

    def find_or_create_customer(self, tenant_id: str, contact: dict, idempotency_key: str) -> str:
        found = self._request(tenant_id, "GET", "/api/customers", params={"phone": contact["phone"]})
        if found.status_code != 200:
            raise FieldServiceError(f"customer lookup failed ({found.status_code})")
        body = {"name": contact["name"], "phone": contact["phone"], "email": contact.get("email") or "",
                "address": contact["street"], "city": contact.get("city") or "", "zip": contact.get("zip") or ""}
        if found.json()["customers"]:
            customer = found.json()["customers"][0]
            changes = {k: v for k, v in body.items() if v and customer.get(k) != v}
            if changes:
                self._request(tenant_id, "PUT", f"/api/customers/{customer['id']}", json=changes)
            return customer["id"]
        created = self._request(tenant_id, "POST", "/api/customers", idempotency_key, json=body)
        if created.status_code != 201:
            raise FieldServiceError(f"customer create failed ({created.status_code})")
        return created.json()["id"]

    def add_equipment(self, tenant_id: str, customer_ref: str, draft: dict, idempotency_key: str) -> str | None:
        name = EQUIPMENT_NAMES.get(draft.get("equipment_type"), "HVAC equipment")
        year = (time.gmtime().tm_year - draft["approx_age_years"]) if draft.get("approx_age_years") else None
        created = self._request(tenant_id, "POST", f"/api/customers/{customer_ref}/assets", idempotency_key,
                                json={"name": name, "manufacturer": draft.get("brand") or "",
                                      "model": draft.get("model") or "", "installation_year": year,
                                      "notes": "Added by ServiceIQ intake"})
        return created.json()["id"] if created.status_code == 201 else None

    def get_customer(self, tenant_id: str, customer_ref: str) -> dict | None:
        response = self._request(tenant_id, "GET", f"/api/customers/{customer_ref}")
        return response.json() if response.status_code == 200 else None

    def call(self, tenant_id: str, method: str, path: str, idempotency_key: str | None = None,
             **kwargs) -> tuple[int, dict]:
        """Any platform endpoint (scheduling, technicians): (status code, JSON body)."""
        response = self._request(tenant_id, method, path, idempotency_key, **kwargs)
        try:
            body = response.json()
        except ValueError:
            body = {"detail": response.text[:200]}
        return response.status_code, body


class MemoryFieldService:
    """In-memory stand-in for tests: same behaviour, no HTTP."""

    def __init__(self, fail: bool = False):
        self.customers: dict[str, dict] = {}
        self.assets: list[dict] = []
        self.fail = fail

    def find_or_create_customer(self, tenant_id: str, contact: dict, idempotency_key: str) -> str:
        if self.fail:
            raise FieldServiceError("field-service platform unreachable")
        digits = "".join(c for c in contact["phone"] if c.isdigit())[-10:]
        for ref, c in self.customers.items():
            if c["tenant_id"] == tenant_id and c["digits"] == digits:
                return ref
        ref = str(uuid.uuid4())
        self.customers[ref] = {"tenant_id": tenant_id, "digits": digits, **contact}
        return ref

    def add_equipment(self, tenant_id: str, customer_ref: str, draft: dict, idempotency_key: str) -> str | None:
        self.assets.append({"tenant_id": tenant_id, "customer": customer_ref, **draft})
        return str(uuid.uuid4())

    def get_customer(self, tenant_id: str, customer_ref: str) -> dict | None:
        c = self.customers.get(customer_ref)
        if c is None or c["tenant_id"] != tenant_id:
            return None
        return {"customer": {"id": customer_ref, "name": c["name"], "phone": c["phone"], "email": c.get("email", ""),
                             "address": c["street"], "city": c.get("city", ""), "zip": c.get("zip", "")},
                "assets": [a for a in self.assets if a["customer"] == customer_ref]}


def load_keys(settings: Settings) -> dict[str, str]:
    if settings.fsp_api_keys:
        return json.loads(settings.fsp_api_keys)
    path = PROJECT_ROOT / "var" / "dev_fsp_keys.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def make_field_service(settings: Settings) -> FieldService:
    """The platform over HTTP when FSP_URL is set; otherwise the same API run in-process (local dev)."""
    if settings.fsp_url:
        return HttpFieldService(settings.fsp_url, load_keys(settings), id_token=settings.fsp_id_token)
    from fastapi.testclient import TestClient

    from fieldservice.app import create_app
    from fieldservice.config import FSPSettings
    return HttpFieldService("", load_keys(settings),
                            http=TestClient(create_app(FSPSettings(fsp_database_url=settings.fsp_database_url))))
