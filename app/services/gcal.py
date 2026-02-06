from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, date
from typing import Dict, List, Tuple, Optional

import json

from ..config import GCAL_ICS_URLS_JSON

import pytz
import requests
from icalendar import Calendar
import recurring_ical_events


@dataclass
class CalEvent:
    event_id: str
    title: str
    start_utc: datetime
    end_utc: datetime
    source: str


def _load_ics_urls() -> Dict[str, str]:
    raw = GCAL_ICS_URLS_JSON
    if not raw:
        raise RuntimeError("GCAL_ICS_URLS_JSON is not set")
    try:
        d = json.loads(raw)
    except json.JSONDecodeError as e:
        raise RuntimeError("GCAL_ICS_URLS_JSON is not valid JSON") from e
    if not isinstance(d, dict) or not d:
        raise RuntimeError("GCAL_ICS_URLS_JSON must be a non-empty JSON object")
    return {str(k): str(v) for k, v in d.items()}


def _day_window_local(tz_name: str, day: date) -> Tuple[datetime, datetime]:
    tz = pytz.timezone(tz_name)
    start = tz.localize(datetime(day.year, day.month, day.day, 0, 0, 0))
    end = start + timedelta(days=1)
    return start, end


def _download_ics(url: str) -> bytes:
    cache_buster = f"t={int(datetime.utcnow().timestamp())}"
    full = url + ("&" if "?" in url else "?") + cache_buster
    resp = requests.get(full, timeout=25)
    resp.raise_for_status()
    return resp.content


def _pick_tz(tzid: Optional[str], fallback_tz_name: str) -> pytz.BaseTzInfo:
    """
    Prefer explicit TZID from ICS property params; else user timezone.
    """
    if tzid:
        try:
            return pytz.timezone(tzid)
        except Exception:
            pass
    return pytz.timezone(fallback_tz_name)


def _as_aware_dt(dt, tzid: Optional[str], fallback_tz_name: str) -> Optional[datetime]:
    """
    Convert icalendar dt to tz-aware datetime.
    - If dt is date-only => all-day => return None (skip)
    - If dt is naive datetime => treat as "floating" local time in TZID or fallback tz
    - If dt already has tzinfo => keep it
    """
    if isinstance(dt, date) and not isinstance(dt, datetime):
        return None

    if dt.tzinfo is None:
        tz = _pick_tz(tzid, fallback_tz_name)
        return tz.localize(dt)

    return dt


def _get_dtend_or_duration(component, fallback_tz_name: str) -> Optional[datetime]:
    """
    Return tz-aware DTEND if present; otherwise DTSTART + DURATION if present.
    """
    dtstart_prop = component.get("DTSTART")
    if not dtstart_prop:
        return None

    tzid_start = dtstart_prop.params.get("TZID") if hasattr(dtstart_prop, "params") else None
    dtstart = _as_aware_dt(dtstart_prop.dt, tzid_start, fallback_tz_name)
    if dtstart is None:
        return None

    dtend_prop = component.get("DTEND")
    if dtend_prop:
        tzid_end = dtend_prop.params.get("TZID") if hasattr(dtend_prop, "params") else tzid_start
        return _as_aware_dt(dtend_prop.dt, tzid_end, fallback_tz_name)

    dur_prop = component.get("DURATION")
    if dur_prop:
        try:
            return dtstart + dur_prop.dt
        except Exception:
            return None

    return None


def _extract_occurrences_for_day(cal: Calendar, tz_name: str, day: date) -> List:
    day_start_local, day_end_local = _day_window_local(tz_name, day)
    return list(recurring_ical_events.of(cal).between(day_start_local, day_end_local))


def _events_from_one_ics(
    ics_url: str,
    source: str,
    tz_name: str,
    day: date,
    include_all_day: bool = False,
) -> List[CalEvent]:
    ics_bytes = _download_ics(ics_url)
    cal = Calendar.from_ical(ics_bytes)

    tz = pytz.timezone(tz_name)
    day_start_local, day_end_local = _day_window_local(tz_name, day)

    components = _extract_occurrences_for_day(cal, tz_name, day)

    out: List[CalEvent] = []
    for comp in components:
        if comp.name != "VEVENT":
            continue

        dtstart_prop = comp.get("DTSTART")
        if not dtstart_prop:
            continue

        tzid_start = dtstart_prop.params.get("TZID") if hasattr(dtstart_prop, "params") else None
        start = _as_aware_dt(dtstart_prop.dt, tzid_start, tz_name)

        if start is None:
            if not include_all_day:
                continue
            # represent all-day as local day window
            start_utc = day_start_local.astimezone(pytz.utc)
            end_utc = day_end_local.astimezone(pytz.utc)
        else:
            end = _get_dtend_or_duration(comp, tz_name)
            if end is None:
                end = start + timedelta(minutes=60)

            # overlap safety check in local tz
            start_local = start.astimezone(tz)
            end_local = end.astimezone(tz)
            if end_local <= day_start_local or start_local >= day_end_local:
                continue

            start_utc = start.astimezone(pytz.utc)
            end_utc = end.astimezone(pytz.utc)

        title = str(comp.get("SUMMARY", "Untitled")).strip()
        uid = str(comp.get("UID", "")).strip() or f"no-uid-{hash(title)}"

        # unique per occurrence
        event_id = f"{source}:{uid}:{start_utc.isoformat()}"

        out.append(CalEvent(
            event_id=event_id,
            title=title,
            start_utc=start_utc,
            end_utc=end_utc,
            source=source,
        ))

    out.sort(key=lambda e: e.start_utc)
    return out


def fetch_events_for_day_multi_ics(tz_name: str, day: date, include_all_day: bool = False) -> List[CalEvent]:
    urls = _load_ics_urls()

    all_events: List[CalEvent] = []
    for source, url in urls.items():
        all_events.extend(_events_from_one_ics(url, source, tz_name, day, include_all_day=include_all_day))

    # Dedupe across calendars (safe)
    seen = set()
    unique: List[CalEvent] = []
    for e in all_events:
        key = (e.start_utc, e.end_utc, e.title)
        if key in seen:
            continue
        seen.add(key)
        unique.append(e)

    unique.sort(key=lambda e: e.start_utc)
    return unique
