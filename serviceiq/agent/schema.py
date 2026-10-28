"""Service request schema v1.0 (Phase 2): what intake collects, and how it is summarized.

The request payload never contains contact details: those go to the field-service platform, and the request
keeps only the customer reference. Contact details are collected through a form, not free text, so they never
pass through a language model.
"""
from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

EQUIPMENT_TYPES = ["central_ac", "furnace", "heat_pump", "ductless", "boiler", "geothermal", "oil_furnace",
                   "window_ac", "water_heater", "thermostat", "air_quality", "other"]
EQUIPMENT_LABELS = {"central_ac": "Central AC", "furnace": "Furnace", "heat_pump": "Heat pump",
                    "ductless": "Ductless mini-split", "boiler": "Boiler", "geothermal": "Geothermal heat pump",
                    "oil_furnace": "Oil furnace", "window_ac": "Window or portable AC", "water_heater": "Water heater",
                    "thermostat": "Thermostat", "air_quality": "Indoor air quality", "other": "Other equipment"}
URGENCY = ["urgent", "soon", "routine"]
URGENCY_LABELS = {"urgent": "Urgent (system down now)", "soon": "Within a few days", "routine": "Routine / flexible"}

# Asked in this order; a request is complete when all are present (plus the contact form)
REQUIRED = ["zip", "equipment_type", "symptoms", "urgency", "preferred_windows"]
CONTACT_REQUIRED = ["name", "phone", "street"]


class Contact(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    phone: str = Field(min_length=7, max_length=50)
    email: str = Field(default="", max_length=200)
    street: str = Field(min_length=1, max_length=300)
    city: str = Field(default="", max_length=100)
    zip: str = Field(default="", pattern=r"^(\d{5})?$")


class ServiceRequestV1(BaseModel):
    schema_version: str = SCHEMA_VERSION
    zip: str
    equipment_type: str
    brand: str | None = None
    model: str | None = None
    approx_age_years: int | None = None
    symptoms: str
    urgency: str
    preferred_windows: list[str]
    notes: str | None = None
    service_area: str | None = None  # Core | Extended (from the eligibility check)
    trip_fee_usd: int | None = None


def missing_fields(draft: dict) -> list[str]:
    return [f for f in REQUIRED if not draft.get(f)]


def missing_contact(contact: dict) -> list[str]:
    return [f for f in CONTACT_REQUIRED if not contact.get(f)]


def summary(draft: dict, contact: dict | None = None) -> str:
    """Plain-text summary for the customer to confirm. Pass `contact` only for display, never for storage."""
    equipment = EQUIPMENT_LABELS.get(draft.get("equipment_type"), draft.get("equipment_type") or "-")
    detail = ", ".join(x for x in (draft.get("brand"), draft.get("model"),
                                   f"about {draft['approx_age_years']} years old" if draft.get("approx_age_years")
                                   else None) if x)
    lines = [f"- Equipment: {equipment}" + (f" ({detail})" if detail else ""),
             f"- Problem: {draft.get('symptoms') or '-'}",
             f"- Urgency: {URGENCY_LABELS.get(draft.get('urgency'), draft.get('urgency') or '-')}",
             f"- Preferred times: {', '.join(draft.get('preferred_windows') or []) or '-'}",
             f"- Service ZIP: {draft.get('zip') or '-'}" + (f" ({draft['service_area']} area, ${draft['trip_fee_usd']} "
                                                             "trip fee)" if draft.get("trip_fee_usd") else "")]
    if draft.get("notes"):
        lines.append(f"- Notes: {draft['notes']}")
    if contact:
        address = ", ".join(x for x in (contact.get("street"), contact.get("city"), contact.get("zip")) if x)
        lines += [f"- Name: {contact.get('name')}", f"- Phone: {contact.get('phone')}",
                  f"- Address: {address}"] + ([f"- Email: {contact['email']}"] if contact.get("email") else [])
    return "\n".join(lines)
