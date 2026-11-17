"""Pings -> CargoTel "Location/ETA" for the loads on Kesh: Tracking Update Screen.

The rule (the user's, 2026-11-12): a load with any ping on its "GPS City/St Link" (the
CargoTel driver app, last 2 days) gets that last ping written into Location/ETA and as a
dispatch comment, and MacroPoint is not looked at; a load with no ping there at all gets
MacroPoint's latest location written into Location/ETA and as a comment labelled
"(MacroPoint)". The CargoTel side is planned in ``tracking_update_alerts.run_once``; this
module holds the MacroPoint side and the writer both share.

The MacroPoint receiver (``macropoint_webhook/``) keeps the newest MacroPoint ping of each
CargoTel load in ``s3://.../macropoint/latest/<load>.json``; the hourly run copies those
files to a folder and this module decides, per load on the screen, whether to write the
ping's place into CargoTel, and writes it.

How CargoTel takes it (verified live on 354350, 2026-11-12): the load page's Location/ETA
function is a small form submitted as a GET to ``/cargotel/db.mcgi`` with ``mode=location``,
``db__location`` and the five ETA selects (they may stay empty). The browser first runs the
typed place through ``/cargotel/zipService.mcgi?action=getZip&query=...`` and puts the
answer in the field (``31029 (Forsyth, GA)``; an answer starting with ``*`` means "not a
valid City, State or Zip"); this module does the same. CargoTel answers "Location Updated"
and records a "Location Update" comment on the load (``Location Update: FORSYTH GA 31029``).

A load with no CargoTel ping is written from MacroPoint when MacroPoint's newest ping:

* is at most ``max_age_hours`` old (default 48, the same two days the GPS link covers);
* is newer than the ping last written on that load, from either source (``location_state.json``);
* names a different place than the last write, or comes ``same_place_hours`` (48) after
  it: a parked truck's place is written once, not with every ping (since 2026-11-17);
* tracks the load's driver: when MacroPoint's locator and CargoTel's driver cell are both
  phone numbers they must match (the account also tracks TransportPro loads, whose order
  ids could collide with a CargoTel load number); a truck or ELD locator is accepted.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

#: MacroPoint pings up to two days old count, like the GPS link's own two-day window.
DEFAULT_MAX_AGE_HOURS = 48.0
#: A ping at the place last written is held back for this long (48 h, the whole window a
#: MacroPoint ping is used in, so in practice until the truck moves); 0 = write every new ping.
#: The user's request, 2026-11-17, after a parked truck got the same Location Update hourly.
DEFAULT_SAME_PLACE_HOURS = 48.0
ZIP_SERVICE_PATH = "/cargotel/zipService.mcgi"
LOCATION_FORM_PATH = "/cargotel/db.mcgi"


@dataclass
class MpPing:
    load_id: str
    when: datetime | None  # aware UTC
    city: str
    state: str
    postal: str
    lat: float | None
    lng: float | None
    locator: str
    source: str

    @property
    def place(self) -> str:
        if self.city and self.state:
            return f"{self.city.strip().title() if self.city.isupper() else self.city.strip()}, {self.state.strip().upper()}"
        return self.postal.strip()

    @property
    def stamp(self) -> str:
        return self.when.isoformat() if self.when else ""


def _when(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _ping(raw: Any) -> MpPing | None:
    if not isinstance(raw, dict) or not raw.get("load_id"):
        return None
    return MpPing(load_id=str(raw["load_id"]), when=_when(raw.get("when_utc")), city=raw.get("city") or "",
                  state=raw.get("state") or "", postal=raw.get("postal") or "", lat=raw.get("lat"), lng=raw.get("lng"),
                  locator=raw.get("locator") or "", source=raw.get("data_source") or "")


def ping_from_record(record: dict[str, Any]) -> MpPing | None:
    return _ping(record.get("latest") if isinstance(record, dict) else None)


def history_from_record(record: dict[str, Any]) -> list[MpPing]:
    """Every ping in a receiver record (``latest`` and ``history``), oldest first."""

    if not isinstance(record, dict):
        return []
    raw = [record.get("latest")] + list(record.get("history") or [])
    pings = [p for p in (_ping(r) for r in raw) if p is not None and p.when is not None]
    return sorted(pings, key=lambda p: p.when)


def load_pings(folder: str | None) -> dict[str, MpPing]:
    """``{load_id: newest ping}`` from ``<folder>/<load>.json`` files written by the receiver."""

    pings: dict[str, MpPing] = {}
    if not folder or not os.path.isdir(folder):
        return pings
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                ping = ping_from_record(json.load(handle))
        except (OSError, ValueError):
            continue
        if ping:
            pings[ping.load_id] = ping
    return pings


def load_histories(folder: str | None) -> dict[str, list[MpPing]]:
    """``{load_id: every stored ping, oldest first}`` from the receiver's ``<load>.json`` files
    (the newest ping and up to ``MP_HISTORY`` before it): the full report's steady test."""

    out: dict[str, list[MpPing]] = {}
    if not folder or not os.path.isdir(folder):
        return out
    for name in os.listdir(folder):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as handle:
                pings = history_from_record(json.load(handle))
        except (OSError, ValueError):
            continue
        if pings:
            out[pings[-1].load_id] = pings
    return out


def digits(text: str | None) -> str:
    return re.sub(r"\D", "", text or "")


def _phone(text: str | None) -> str:
    """The last ten digits when ``text`` is a US phone number (10 digits, or 11 with a 1)."""

    d = digits(text)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    return d if len(d) == 10 else ""


def macropoint_covers(load: Any, ping: MpPing | None, now: datetime, max_age_hours: float = 48.0, match_phone: bool = True) -> bool:
    """Whether ``ping`` (MacroPoint's newest for the load) shows the load is being tracked: at
    most ``max_age_hours`` old, not in the future, and, when MacroPoint's locator and the CargoTel
    driver cell are both phone numbers, the same phone."""

    if ping is None or ping.when is None:
        return False
    age_h = (now - ping.when).total_seconds() / 3600
    if age_h > max_age_hours or age_h < -0.25:
        return False
    mp_phone, ct_phone = _phone(ping.locator), _phone(getattr(load, "driver_phone", ""))
    return not (match_phone and mp_phone and ct_phone and mp_phone != ct_phone)


class LocationState:
    """``location_state.json``: ``{"written": {load: {"ping_utc", "place", "value", "at"}}}``."""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self.written: dict[str, dict[str, str]] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.written = data.get("written", {}) if isinstance(data, dict) else {}

    def get(self, load_id: str) -> dict[str, str] | None:
        return self.written.get(load_id)

    def mark(self, load_id: str, ping: MpPing, value: str, now: datetime, source: str = "macropoint") -> None:
        self.written[load_id] = {"ping_utc": ping.stamp, "place": ping.place, "value": value, "source": source,
                                 "at": now.isoformat(timespec="seconds")}

    def prune(self, now: datetime, keep_days: int = 30) -> None:
        cutoff = now - timedelta(days=keep_days)
        for load_id in list(self.written):
            at = _when(self.written[load_id].get("at"))
            if at is None or at < cutoff:
                del self.written[load_id]

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"updated": datetime.now(timezone.utc).isoformat(timespec="seconds"), "written": self.written}, handle, indent=2)
        os.replace(tmp, self.path)


@dataclass
class LocationPlan:
    load_id: str
    ping: MpPing
    status: str
    reason: str  # "first" | "moved" | "refresh" | "new ping"
    source: str = "macropoint"  # or "cargotel"


def plan_locations(
    loads: Iterable[Any],
    pings: dict[str, MpPing],
    state: LocationState,
    now: datetime,
    *,
    max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
    same_place_hours: float = DEFAULT_SAME_PLACE_HOURS,
    match_phone: bool = True,
) -> tuple[list[LocationPlan], dict[str, str]]:
    """``(to_write, skipped)`` for the screen's ``loads`` (objects with ``load_id``,
    ``status`` and ``driver_phone``); ``skipped`` maps a load id to why it was not written.
    Loads without a MacroPoint ping are in neither."""

    to_write: list[LocationPlan] = []
    skipped: dict[str, str] = {}
    for load in loads:
        ping = pings.get(load.load_id)
        if ping is None:
            continue
        if ping.when is None:
            skipped[load.load_id] = "ping has no time"
            continue
        age_h = (now - ping.when).total_seconds() / 3600
        if age_h > max_age_hours:
            skipped[load.load_id] = f"newest ping is {age_h:.1f} h old"
            continue
        if age_h < -0.25:
            skipped[load.load_id] = "ping time is in the future"
            continue
        if not ping.place:
            skipped[load.load_id] = "ping has no city, state or zip"
            continue
        mp_phone, ct_phone = _phone(ping.locator), _phone(getattr(load, "driver_phone", ""))
        if match_phone and mp_phone and ct_phone and mp_phone != ct_phone:
            skipped[load.load_id] = f"MacroPoint tracks {mp_phone}, the CargoTel driver cell is {ct_phone}"
            continue
        prev = state.get(load.load_id)
        reason = "first"
        if prev:
            prev_when = _when(prev.get("ping_utc"))
            if prev_when and prev_when >= ping.when:
                skipped[load.load_id] = "this ping is already written"
                continue
            prev_at = _when(prev.get("at"))
            if prev.get("place") == ping.place:
                if same_place_hours > 0 and prev_at and (now - prev_at).total_seconds() / 3600 < same_place_hours:
                    skipped[load.load_id] = f"still at {ping.place}, written {(now - prev_at).total_seconds() / 3600:.1f} h ago"
                    continue
                reason = "refresh"
            else:
                reason = "moved"
        to_write.append(LocationPlan(load_id=load.load_id, ping=ping, status=getattr(load, "status", ""), reason=reason))
    return to_write, skipped


class LocationWriter:
    """Writes a place into a load's Location/ETA the way the load page does. ``session``
    needs ``get(url, params) -> (status, html)`` (``CargoTelSession``)."""

    def __init__(self, session: Any, base_url: str, log: Callable[[str], None] = lambda m: None) -> None:
        self.session = session
        self.base_url = base_url.rstrip("/")
        self.log = log
        self._zip: dict[str, str | None] = {}

    def normalize(self, query: str) -> str | None:
        """CargoTel's own City/State/Zip lookup: ``31029 (Forsyth, GA)``, or None when it
        does not know the place."""

        query = query.strip()
        if not query:
            return None
        if query not in self._zip:
            status, body = self.session.get(self.base_url + ZIP_SERVICE_PATH, {"action": "getZip", "query": query})
            answer = (body or "").strip()
            self._zip[query] = answer if status == 200 and answer and not answer.startswith("*") and len(answer) < 120 else None
        return self._zip[query]

    def value_for(self, ping: MpPing) -> str | None:
        value = self.normalize(ping.place)
        if value is None and ping.postal and ping.postal != ping.place:
            value = self.normalize(ping.postal)
        return value

    def write(self, load_id: str, value: str) -> tuple[bool, str]:
        params = {"db__location": value, "db__month_eta": "", "db__day_eta": "", "db__year_eta": "", "db__hour_eta": "",
                  "db__minute_eta": "", "load_id": load_id, "mode": "location", "closex": "", "reloadx": "1"}
        status, body = self.session.get(self.base_url + LOCATION_FORM_PATH, params)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"<(script|style)\b.*?</\1>", " ", body or "", flags=re.S | re.I)))
        if status == 200 and "Location Updated" in text:
            return True, value
        error = re.search(r"An Error was encountered.{0,200}", text)
        return False, (error.group(0) if error else f"HTTP {status}, no 'Location Updated' in the answer")[:200]

    def apply(self, plans: list[LocationPlan]) -> dict[str, tuple[bool, str]]:
        """``{load_id: (written, value or error)}``."""

        out: dict[str, tuple[bool, str]] = {}
        for plan in plans:
            value = self.value_for(plan.ping)
            if value is None:
                out[plan.load_id] = (False, f"CargoTel does not know the place {plan.ping.place!r}")
                continue
            try:
                out[plan.load_id] = self.write(plan.load_id, value)
            except Exception as exc:  # noqa: BLE001 - one load must not stop the others
                out[plan.load_id] = (False, str(exc)[:200])
        return out


def mp_comment_text(ping: MpPing, tz: Any = None) -> str:
    """``"2026-11-12 10:09:00-05 Covington, GA (MacroPoint)"``: the ping in the stamp format of
    the driver-app comments (local time with offset), the place, and where it came from."""

    when = ping.when.astimezone(tz) if (ping.when and tz) else ping.when
    if when is None:
        return f"{ping.place} (MacroPoint)"
    offset = when.strftime("%z")
    offset = (offset[:3] if offset.endswith("00") else f"{offset[:3]}:{offset[3:]}") if offset else ""
    return f"{when:%Y-%m-%d %H:%M:%S}{offset} {ping.place} (MacroPoint)"[:255]


def write_location_dump(outbox: str, plans: list[LocationPlan], now: datetime) -> str:
    os.makedirs(outbox, exist_ok=True)
    path = os.path.join(outbox, f"{datetime.now():%Y%m%d-%H%M%S}_locations.txt")
    with open(path, "w", encoding="utf-8") as handle:
        for plan in plans:
            age = (now - plan.ping.when).total_seconds() / 60 if plan.ping.when else 0
            handle.write(f"{plan.load_id} | {plan.status} | {plan.source} | {plan.ping.place} | ping {plan.ping.stamp} ({age:.0f} min ago) | "
                         f"{plan.ping.source} {plan.ping.locator} | {plan.reason}\n")
    return path
