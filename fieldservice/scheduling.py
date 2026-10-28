"""Availability and conflict checks (new in this port: OpenFieldService has no availability or double-booking check).

Working hours are kept in each org's local time ("mon": ["08:00", "17:00"]); jobs are stored in UTC. A slot is
offered when a technician with the right skill, serving that ZIP, is working for the whole visit and has no
overlapping job. Daylight-saving changes are handled by building local times with the org's zoneinfo.
"""
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from fieldservice.models import Job, Technician

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
ACTIVE_STATUSES = {"scheduled", "confirmed", "in_progress"}  # cancelled and completed jobs free the time
SLOT_STEP_MINUTES = 30


@dataclass
class Slot:
    start: datetime          # UTC
    technician_id: str
    technician_name: str


def utc(dt: datetime) -> datetime:
    """SQLite returns naive datetimes; everything stored is UTC."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def working_window(tech: Technician, day: date, tz: ZoneInfo) -> tuple[datetime, datetime] | None:
    hours = (tech.working_hours or {}).get(DAYS[day.weekday()])
    if not hours:
        return None
    start = datetime.combine(day, time.fromisoformat(hours[0]), tz)
    end = datetime.combine(day, time.fromisoformat(hours[1]), tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def can_do(tech: Technician, skill: str, zip_code: str | None) -> bool:
    if not tech.active:
        return False
    if skill and skill not in (tech.skills or []):
        return False
    return not (zip_code and tech.service_zips and zip_code not in tech.service_zips)


def overlaps(start: datetime, minutes: int, jobs: list[Job], ignore_job_id: str | None = None) -> bool:
    end = start + timedelta(minutes=minutes)
    for job in jobs:
        if job.id == ignore_job_id or job.status not in ACTIVE_STATUSES:
            continue
        other = utc(job.scheduled_start)
        if start < other + timedelta(minutes=job.duration) and other < end:
            return True
    return False


def within_hours(tech: Technician, start: datetime, minutes: int, tz: ZoneInfo) -> bool:
    window = working_window(tech, start.astimezone(tz).date(), tz)
    return window is not None and window[0] <= start and start + timedelta(minutes=minutes) <= window[1]


def free_slots(techs: list[Technician], jobs_by_tech: dict[str, list[Job]], day_from: date, day_to: date,
               minutes: int, tz: ZoneInfo, not_before: datetime, limit: int) -> list[Slot]:
    """Earliest-first free slots; one technician per start time (the first free one in name order)."""
    slots: list[Slot] = []
    day = day_from
    while day <= day_to and len(slots) < limit:
        starts: dict[datetime, Slot] = {}
        for tech in sorted(techs, key=lambda t: t.name):
            window = working_window(tech, day, tz)
            if window is None:
                continue
            start = window[0]
            while start + timedelta(minutes=minutes) <= window[1]:
                if start >= not_before and start not in starts and not overlaps(start, minutes,
                                                                                jobs_by_tech.get(tech.id, [])):
                    starts[start] = Slot(start, tech.id, tech.name)
                start += timedelta(minutes=SLOT_STEP_MINUTES)
        slots += [starts[s] for s in sorted(starts)]
        day += timedelta(days=1)
    return slots[:limit]
