"""Operations (Phase 3): turn a service request into a booked job in the field-service platform.

Everything here is deterministic: which service type a request needs, which free slots match the customer's
preferred times, the published price from the current price book, and the booking itself (behind the policy
layer's hard limits). Used by the agent graph and by the admin console when an approver decides.
"""
import csv
import re
from datetime import date, datetime, timedelta
from email.message import EmailMessage

from sqlalchemy.orm import Session

from serviceiq.agent import policy
from serviceiq.agent.eligibility import _documents
from serviceiq.agent.fieldservice_client import FieldService, SlotTaken
from serviceiq.config import Settings
from serviceiq.escalation import send
from serviceiq.tenants import TenantProfile

EQUIPMENT_SKILLS = {"central_ac": "ac", "window_ac": "ac", "furnace": "furnace", "heat_pump": "heat_pump",
                    "ductless": "ductless", "boiler": "boiler", "water_heater": "water_heater"}
INSTALL_WORDS = ("install", "replace", "replacement", "new system", "new unit", "estimate")
MAINTENANCE_WORDS = ("tune", "maintenance", "check-up", "checkup", "inspection", "annual", "service my", "flush",
                     "cleaning")
DAY_NAMES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def choose_service(types: list[dict], draft: dict) -> dict | None:
    """Estimate for installs, a tune-up for maintenance of matching equipment, else a standard diagnostic."""
    text = f"{draft.get('symptoms') or ''} {draft.get('notes') or ''}".lower()
    skill = EQUIPMENT_SKILLS.get(draft.get("equipment_type"), "")

    def first(pred):
        return next((t for t in types if pred(t)), None)
    if any(w in text for w in INSTALL_WORDS):
        found = first(lambda t: "EST" in t["code"])
        if found:
            return found
    if any(w in text for w in MAINTENANCE_WORDS) and skill:
        found = first(lambda t: t.get("skill") == skill and any(k in t["code"] for k in ("TUNE", "BOILER")))
        if found:
            return found
    return (first(lambda t: "DIAG" in t["code"] and not any(k in t["code"] for k in ("AH", "COM", "-02")))
            or (types[0] if types else None))


def matches_windows(local: datetime, windows: list[str]) -> bool:
    """Does a local start time fit any of the customer's phrases ("weekday mornings", "Tuesday afternoon")?"""
    if not windows:
        return True
    for phrase in windows:
        p = phrase.lower()
        if any(w in p for w in ("any", "flexible", "whenever", "asap", "soon as")):
            return True
        days = [i for i, name in enumerate(DAY_NAMES) if name in p or re.search(rf"\b{name[:3]}\b", p)]
        if "weekday" in p:
            days += [0, 1, 2, 3, 4]
        if "weekend" in p:
            days += [5, 6]
        day_ok = not days or local.weekday() in days
        hour = local.hour
        if "morning" in p:
            time_ok = hour < 12
        elif "afternoon" in p:
            time_ok = 12 <= hour < 17
        elif "evening" in p or "night" in p:
            time_ok = hour >= 16
        else:
            time_ok = True
        if day_ok and time_ok:
            return True
    return False


def label(local_iso: str) -> str:
    dt = datetime.fromisoformat(local_iso)
    hour = dt.hour % 12 or 12
    return f"{dt:%a %b} {dt.day} at {hour}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"


def propose(fsp: FieldService, settings: Settings, tenant_id: str, draft: dict, today: date,
            count: int = 3) -> tuple[dict | None, list[dict]]:
    """(service type, up to `count` slots): urgent requests get the earliest times; others prefer their stated
    windows, one slot per day so the customer gets a real choice."""
    service = choose_service(fsp.service_types(tenant_id), draft)
    if service is None:
        return None, []
    slots = fsp.availability(tenant_id, date_from=today.isoformat(),
                             date_to=(today + timedelta(days=settings.booking_days_ahead)).isoformat(),
                             service_type_id=service["id"], zip=draft.get("zip"),
                             skill=EQUIPMENT_SKILLS.get(draft.get("equipment_type")) if "DIAG" in service["code"]
                             else None, limit=100)
    wanted = [s for s in slots if matches_windows(datetime.fromisoformat(s["local_start"]),
                                                  draft.get("preferred_windows") or [])] or slots
    if draft.get("urgency") == "urgent":  # earliest times, but at least two hours apart so they're a real choice
        picked = []
        for s in wanted:
            start = datetime.fromisoformat(s["start"])
            if all(abs(start - datetime.fromisoformat(p["start"])) >= timedelta(hours=2) for p in picked):
                picked.append(s)
            if len(picked) == count:
                break
    else:
        picked, days = [], set()
        for s in wanted:
            day = s["local_start"][:10]
            if day not in days:
                picked.append(s)
                days.add(day)
            if len(picked) == count:
                break
    return service, [s | {"label": label(s["local_start"])} for s in picked]


def published_price(session: Session | None, settings: Settings, tenant_id: str, code: str) -> dict | None:
    """The current price-book row for a service code: {low, high, unit}, or None."""
    for entry in _documents(session, settings.data_dir, tenant_id, settings.serviceiq_as_of_date):
        if entry.document_type != "price_book" or entry.path.suffix != ".csv":
            continue
        with open(entry.path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                if row.get("item_code") == code and row.get("price_low_usd"):
                    return {"low": float(row["price_low_usd"]), "high": float(row["price_high_usd"] or
                                                                              row["price_low_usd"]),
                            "unit": row.get("unit") or "", "name": row.get("service") or code}
    return None


def price_text(price: dict | None, trip_fee: int | None) -> str:
    if price is None:
        return "The office will confirm the price."
    amount = f"${price['low']:g}" if price["low"] == price["high"] else f"${price['low']:g}-${price['high']:g}"
    text = f"{price['name']}: {amount} {price['unit']} (published price; any repair is quoted before work starts)"
    return text + (f", plus the ${trip_fee} trip fee." if trip_fee else ".")


def book(session: Session, fsp: FieldService, settings: Settings, tenant_id: str, request, slot: dict,
         service: dict, approver: str | None = None, price: float | None = None,
         price_range: tuple[float, float] | None = None, outside_hours: bool = False) -> dict:
    """Create the job behind the policy layer's hard limits. Raises PolicyViolation or SlotTaken."""
    flags = request.payload.get("flags") or []
    policy.check_booking(session, settings, tenant_id, flags, approver, price, price_range, outside_hours)
    body = {"customer_id": request.customer_ref, "service_type_id": service["id"],
            "technician_id": slot.get("technician_id"), "scheduled_start": slot["start"],
            "asset_id": request.payload.get("asset_ref"),
            "priority": "high" if request.payload.get("urgency") == "urgent" else "normal",
            "notes": f"Booked by ServiceIQ for request {request.request_id[:8].upper()}"
                     + (f" (approved by {approver})" if approver else ""),
            "external_ref": request.request_id, "allow_outside_hours": outside_hours}
    if price is not None:
        body["price"] = price
    code, job = fsp.create_job(tenant_id, body, f"job-{request.request_id}-{slot['start']}")
    if code == 409:
        raise SlotTaken(job.get("detail", "slot taken"))
    if code != 201:
        raise RuntimeError(f"booking failed ({code}: {job.get('detail')})")
    request.job_ref, request.status = job["id"], "scheduled"
    return {"job_id": job["id"], "identifier": job.get("identifier"), "start": slot["start"],
            "local_start": slot["local_start"], "technician_name": job.get("technician_name")
            or slot.get("technician_name")}


def confirmation_email(settings: Settings, profile: TenantProfile, to: str, booked: dict, service: dict,
                       price_line: str) -> str:
    msg = EmailMessage()
    msg["Subject"] = f"Your {profile.company_name} visit: {label(booked['local_start'])}"
    msg["From"] = settings.escalation_from_email or "serviceiq@localhost"
    msg["To"] = to
    msg.set_content("\n".join([
        f"Your visit is booked for {label(booked['local_start'])}.", "",
        f"Service: {service['name']}", f"Technician: {(booked.get('technician_name') or 'our technician').split()[0]}",
        f"Reference: {booked['identifier']}", price_line, "",
        f"To change or cancel, call {profile.company_name} at {profile.office_phone} ({profile.business_hours}).",
    ]))
    return send(settings, msg, f"CONFIRM-{booked['job_id']}")
