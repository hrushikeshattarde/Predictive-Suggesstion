"""Tracking-update alerts: check the GPS pings of every load on Operations Reports > Kesh:
Tracking Update Screen, write the last ping into the dispatch comments of the loads that
are pinging, and email the dispatch rep, the sales rep and the sales rep's manager about
the loads that are not.

What it does, once per run:

1. Pull the saved search (id 2077): the Dispatched loads whose carrier was added in the
   last 90 days, with the sales rep and customer columns added. The screen's own
   "Pickup ETA before <date>" filter cannot be moved from a URL (CargoTel crashes on
   the mnemonic), but sending only the date parts of that filter drops it, so the job
   pulls the unfiltered set and applies the rule locally: pickup ETA before
   ``today + --days-ahead`` (2) at midnight, which is the date the screen is meant to
   carry each day (Nov 8 on Nov 6, Nov 9 on Nov 7, ...). Verified 2026-11-06: the local
   rule reproduces the saved screen's 93 rows exactly.
2. Open every load's "GPS City/St Link" (``gpshistory.mcgi``) for the last
   ``--gps-days`` (2: the same 48-hour window the page shows when a rep clicks the link,
   so the job never sees a ping the rep would not) and read the ping table (user,
   timestamp with UTC offset, city/state).
3. Sort the loads:

   * **active**: the last ping is within ``--stale-hours`` (4). The last ping's time and
     place go into the load's "Dispatch Comment (Add)" box, in the format the team
     already uses by hand (``2026-11-05 10:19:29-05 Burnsville, MN``), unless the load's
     last dispatch comment already carries that ping;
   * **stale**: pings exist but the last one is older than ``--stale-hours``. The last
     ping is written as a comment the same way (once: it does not change while the
     driver is quiet) AND the load is emailed;
   * **never**: no ping at all in the window: emailed, nothing to comment;
   * **unknown**: the GPS page could not be read (logged, never emailed).

   Loads of an excluded customer (``--exclude-customer``; Merchants Automotive Group
   Holdings LLC by default) are left out of the run altogether: no comment, no email.
4. Email the stale and never loads, grouped: for Stellantis loads (customer id 62943) one
   email per dispatch rep, To the dispatch rep and ``stellantisob@circledelivers.com``;
   for every other customer one email per (dispatch rep, sales rep), To both, Cc the
   sales rep's manager from ``reps.csv`` (its ``manager`` column) and the secondary
   sales rep. Groups with the same recipients are merged. When nobody can be addressed
   the escalation list (``--escalate-to``) gets the email with the reps named per load.
5. Remember what was done: a load is re-alerted after ``--repeat-hours`` (4) while it
   stays stale, or at once when its state or owners change; a ping is written as a
   comment once. Dry runs (no ``--send`` / ``--post-comments``) write the emails and the
   comments to ``outbox/`` and leave the state alone.
6. Four times a day (07:00, 11:00, 15:00, 18:00 ET; ``--report-hours``) send the operations
   manager a report of the whole screen, the Dispatched loads above plus the In-Route
   ones (``--report-status``), which are GPS-checked for the report only (no comments,
   no alerts): how many loads were scanned with and without issues per status, per
   customer and per dispatch rep, then every load with no GPS pings and every driver
   whose last ping is more than ``--report-quiet-hours`` (11) old, listed by customer
   and dispatch rep. ``--report`` sends it from any run (with ``--redirect-to`` for a
   look first).

Within a run the emails and the report go out first and the alert state is saved (and,
on AWS, checkpointed to S3); the comment form is posted last, so a timeout there costs
the comments only. CargoTel re-saves every row of the form it is sent (about two seconds
a row: the whole 320-row screen took over ten minutes and timed the Lambda out on
2026-11-10), so the comments are posted from the screen re-read with its "CargoTel IDs"
filter set to just the loads being commented: still the page's whole ``Menu_alt`` form,
only a few dozen rows long.

``--no-alerts`` (``TRACK_ALERTS=0``) switches the hourly emails to the reps off while the
comments and the report carry on (the user's choice from 2026-11-09 14:35 ET).

Locations (2026-11-12, the user's rule): with ``--macropoint-dir`` (the newest MacroPoint
ping per load, written by ``macropoint_webhook/`` and copied from S3 by the Lambda handler)
the In-Route loads are GPS-checked every run too, and every load on the screen (Dispatched
and In-Route, pickup ETA before the screen's date) gets a location: a load with any ping on
its GPS City/St link in the last 4 hours (``--gps-fallback-hours``) gets its last ping
written into Location/ETA (the way the load page's Location/ETA function does it) and as a
dispatch comment, MacroPoint not looked at; a load with no ping there at all, or only older
ones, gets MacroPoint's latest location written into Location/ETA and as a comment labelled
"(MacroPoint)" (``macropoint_locations.py``). ``--post-locations`` writes
for real, otherwise the planned writes go to ``outbox/``. This runs after the emails and
before the comment form.

Usage::

    python tracking_update_alerts.py --list                    # the loads and their GPS state, nothing done
    python tracking_update_alerts.py                           # dry run: emails + comments to outbox/
    python tracking_update_alerts.py --post-comments           # comments into CargoTel, emails still to outbox/
    python tracking_update_alerts.py --send --post-comments    # live
    python tracking_update_alerts.py --from-html pages/grid.html --gps-dir pages/gps   # offline

The CargoTel modules (``cargotel_search``, ``suggest_reloads``, ``send_drafts``) live one
folder up and the email plumbing is shared with ``due_date_alerts``; both are on
``sys.path`` so this runs from here and from the flat Lambda zip alike.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
for _path in (PARENT, os.path.join(PARENT, "due_date_alerts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from cargotel_search import (  # noqa: E402
    BASE_URL,
    SEARCH_URL,
    CargoTelSession,
    Grid,
    GridRow,
    SearchParams,
    _BROWSER_HEADERS,
    is_signin_page,
    parse_grid,
)
from due_date_alerts import (  # noqa: E402
    DEFAULT_FROM,
    Email,
    FileTransport,
    SesTransport,
    _dedupe,
    _first_name,
    _fmt_when,
    append_log,
    redirect,
)
from send_drafts import _name_key, load_rep_details  # noqa: E402
from suggest_reloads import nice_name  # noqa: E402

from macropoint_locations import (  # noqa: E402
    DEFAULT_MAX_AGE_HOURS as MP_MAX_AGE_HOURS,
    DEFAULT_SAME_PLACE_HOURS as MP_SAME_PLACE_HOURS,
    LocationPlan,
    LocationState,
    LocationWriter,
    MpPing,
    load_pings,
    mp_comment_text,
    plan_locations,
    write_location_dump,
)

__all__ = [
    "ACTIVE", "NEVER", "STALE", "UNKNOWN",
    "AlertState", "CommentState", "GpsLoad", "MailGroup", "OutboxTransport", "Ping",
    "build_comment_post", "check_gps", "classify", "comment_boxes", "comment_text", "compose", "compose_report", "customer_excluded", "eta_cutoff",
    "form_fields", "gps_params", "grid_params", "load_from_row", "mail_groups", "main", "needs_comment", "nice_place",
    "parse_ping_stamp", "parse_pings", "parse_report_hours", "parse_stamp", "plan_comments", "report_breakdowns", "report_bucket", "report_due",
    "report_loads", "run_once", "select_loads", "verify_comments",
]

SEARCH_ID = "2077"
SEARCH_NAME = "Kesh: Tracking Update Screen"
SEARCH_PATH = "Operations Reports > Kesh: Tracking Update Screen"
GPS_URL = f"{BASE_URL}/backoffice/gpshistory.mcgi"
LOAD_URL = f"{BASE_URL}/backoffice/loadmaint.mcgi?load_id="
#: The screen's date: pickup ETA before today + this many days (at midnight).
DEFAULT_DAYS_AHEAD = 2
#: A driver whose last ping is older than this is "not pinging".
DEFAULT_STALE_HOURS = 4.0
#: How far back the GPS history is asked for: 2 days, the page's own default window, so a
#: load reads "no GPS pings" exactly when the link shows an empty table. (A wider window
#: turned up pings from before the pickup on loads whose link showed nothing, 2026-11-06.)
DEFAULT_GPS_DAYS = 2
#: A GPS-link ping older than this counts like no ping for the locations: MacroPoint's latest
#: location is used instead (the user's choice, 2026-11-12: 4 hours, the same line as "stale").
DEFAULT_GPS_FALLBACK_HOURS = 4.0
#: A load that stays stale is alerted again after this long.
DEFAULT_REPEAT_HOURS = 4.0
DEFAULT_STATUSES = ("Dispatched",)
DEFAULT_MAX_GPS = 400
#: Stellantis loads go to the dispatch rep and this mailbox, nobody else.
DEFAULT_STELLANTIS_TO = ("stellantisob@circledelivers.com",)
STELLANTIS_CUSTOMER_IDS = ("62943",)
_STELLANTIS_RE = re.compile(r"stellantis|\bfca\b", re.I)
#: Where an alert goes when neither rep can be addressed (same list as the pickup-ETA job).
DEFAULT_ESCALATE = ("justice.cromwell@circledelivers.com", "smitchell@circledelivers.com")
#: Customers left out of the whole run (no comments, no emails): a customer id, or a
#: case-insensitive part of the customer name. Merchants Automotive Group Holdings LLC
#: (id 81464) at the user's request, 2026-11-06.
DEFAULT_EXCLUDE_CUSTOMERS = ("81464", "Merchants Automotive")
#: The daily report for the operations manager (user's request, 2026-11-06): every load
#: with no GPS pings and every driver quiet for longer than DEFAULT_REPORT_QUIET_HOURS,
#: by customer and dispatch rep. Sent by the run(s) whose Eastern hour is listed in
#: DEFAULT_REPORT_HOURS ("all" = every run), once per hour slot. 07:00, 11:00, 15:00 and 18:00 ET: the
#: user's choice (2026-11-09; 10:00 and 16:00 earlier that day, 08:00 before).
#: Justice Cromwell, the Stellantis mailbox, Brett Booth, Podge Ormsby, Mitchell Meyers and
#: Stuart Mitchell (the user's list: Justice alone until 2026-11-09 13:35 ET, Stuart added
#: 2026-11-11).
DEFAULT_REPORT_TO = (
    "justice.cromwell@circledelivers.com",
    "stellantisob@circledelivers.com",
    "brett.booth@circledelivers.com",
    "pormsby@circledelivers.com",
    "mitchell.meyers@circledelivers.com",
    "smitchell@circledelivers.com",
)
DEFAULT_REPORT_HOURS: tuple[int, ...] | str = (7, 11, 15, 18)
DEFAULT_REPORT_QUIET_HOURS = 11.0
#: A load quiet for more than DEFAULT_REPORT_QUIET_HOURS is still not an issue when its driver
#: pinged steadily in the past 16 hours: at least 5 pings, none more than 3 hours apart (a
#: driver who drove all evening and parked for the night). The operations team's request,
#: 2026-11-12, after 356838 was listed while its driver was at home.
DEFAULT_REPORT_STEADY_HOURS = 16.0
DEFAULT_REPORT_STEADY_PINGS = 5
DEFAULT_REPORT_STEADY_GAP_HOURS = 3.0
#: Statuses scanned for the report only (no comments, no alerts): the In-Route loads on
#: the same screen, asked for on 2026-11-09.
DEFAULT_REPORT_STATUSES = ("In-Route",)
#: Columns added to the saved view on the GET (extras only, see cargotel_search notes):
#: the sales reps, and the customer. ``consignor`` reads "N/A" on EDI loads (every
#: Stellantis load); ``original_consignor`` and ``cons_id`` carry the real customer.
EXTRA_VIEW_FIELDS = ("sales_rep1", "sales_rep2", "original_consignor", "cons_id", "consignor")
REQUIRED_COLUMNS = ("pickiup_eta_datetime", "GPSLink", "DispatchCmt_IDLastCmt", "dispatch_rep", "status")
#: The screen's "CargoTel IDs" filter (a textarea, one load id per line). Sent on the GET it
#: narrows the saved search to those loads, and the page's Menu_alt form shrinks with it
#: (3 loads: 177 fields instead of 4,271 for the 318-row screen, 2026-11-10); the form
#: echoes it back, so the comment POST re-saves and re-renders those rows only.
COMMENT_FILTER = "Menu__Load_Id"
UNASSIGNED = ""

ACTIVE = "active"
STALE = "stale"
NEVER = "never"
UNKNOWN = "unknown"

_STAMP_RE = re.compile(r"^\s*(\d{1,2}/\d{1,2}/\d{4})(?:\s+(\d{1,2}:\d{2}))?")
_PING_STAMP_RE = re.compile(r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\s*([+-])(\d{2}):?(\d{2})?)?")
_HREF_RE = re.compile(r'href="([^"]+)"', re.I)
_TABLE_RE = re.compile(r"<table\b.*?</table>", re.S | re.I)
_TR_RE = re.compile(r"<tr\b.*?</tr>", re.S | re.I)
_TD_RE = re.compile(r"<td\b[^>]*>(.*?)</td>", re.S | re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def parse_stamp(text: str | None) -> datetime | None:
    """``"11/06/2026 20:00"`` -> naive datetime (CargoTel's clock, Eastern); else ``None``."""

    if not text:
        return None
    match = _STAMP_RE.match(text)
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)} {match.group(2) or '00:00'}", "%m/%d/%Y %H:%M")
    except ValueError:
        return None


def parse_ping_stamp(text: str | None, default_tz: Any = None) -> datetime | None:
    """``"2026-11-06 06:12:25-05"`` -> aware datetime (UTC-5). A stamp with no offset gets
    ``default_tz`` (the run's local zone)."""

    if not text:
        return None
    match = _PING_STAMP_RE.search(text)
    if not match:
        return None
    try:
        naive = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if match.group(3):
        offset = timedelta(hours=int(match.group(4)), minutes=int(match.group(5) or 0))
        return naive.replace(tzinfo=timezone(-offset if match.group(3) == "-" else offset))
    if default_tz is not None:
        return naive.replace(tzinfo=default_tz)
    return naive.astimezone()


def nice_place(text: str | None) -> str:
    """``"Oak Lawn,IL "`` -> ``"Oak Lawn, IL"``."""

    text = " ".join((text or "").split())
    return re.sub(r"\s*,\s*", ", ", text).strip(", ")


def ago_text(hours: float) -> str:
    """``0.3`` -> ``"just now"``; ``5.6`` -> ``"6 h ago"``; ``50`` -> ``"2 d 2 h ago"``."""

    whole = int(hours + 0.5)
    if whole <= 0:
        return "just now"
    days, rem = divmod(whole, 24)
    if not days:
        return f"{rem} h ago"
    if not rem:
        return f"{days} d ago"
    return f"{days} d {rem} h ago"


def _plain(text: str | None) -> str:
    return " ".join((text or "").split())


def eta_cutoff(now: datetime, days_ahead: int = DEFAULT_DAYS_AHEAD) -> datetime:
    """The screen's date as a naive midnight: loads with a pickup ETA before it are in."""

    day = now.date() + timedelta(days=days_ahead)
    return datetime(day.year, day.month, day.day)


# --------------------------------------------------------------------------- #
# GPS pings                                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class Ping:
    when: datetime  # aware
    where: str  # "Oak Lawn, IL"
    who: str
    stamp: str  # as CargoTel prints it: "2026-11-06 06:12:25-05"

    def to_record(self) -> dict[str, str]:
        return {"when": self.when.isoformat(), "where": self.where, "who": self.who, "stamp": self.stamp}


def parse_pings(html: str, default_tz: Any = None) -> list[Ping]:
    """The ping table of a ``gpshistory.mcgi`` page (the one whose header says
    Timestamp), oldest first. Rows that do not parse are skipped."""

    table = next((t for t in _TABLE_RE.findall(html) if "Timestamp" in t), None)
    if table is None:
        return []
    pings: list[Ping] = []
    for row in _TR_RE.findall(table)[1:]:
        cells = [_plain(html_lib.unescape(_TAG_RE.sub(" ", c))) for c in _TD_RE.findall(row)]
        if len(cells) < 3:
            continue
        when = parse_ping_stamp(cells[1], default_tz)
        if when is None:
            continue
        pings.append(Ping(when=when, where=nice_place(cells[2]), who=cells[0], stamp=cells[1]))
    pings.sort(key=lambda p: p.when)
    return pings


def gps_params(load_id: str, start: datetime, end: datetime) -> dict[str, str]:
    """The ``gpshistory.mcgi`` query for one load and an explicit window (the form's
    select names; CargoTel honours them on a GET, verified 2026-11-06)."""

    out = {"load_id": load_id}
    for tag, when in (("start", start), ("end", end)):
        out.update({
            f"gpshistory_form__month_{tag}_date": str(when.month),
            f"gpshistory_form__day_{tag}_date": str(when.day),
            f"gpshistory_form__year_{tag}_date": str(when.year),
            f"gpshistory_form__hour_{tag}_date": str(when.hour),
            f"gpshistory_form__minute_{tag}_date": str(when.minute),
        })
    return out


# --------------------------------------------------------------------------- #
# The loads                                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class GpsLoad:
    load_id: str
    kind: str  # "load" (bundle parent) or "order"
    customer: str
    customer_id: str
    dispatch_rep: str | None
    status: str
    carrier: str
    origin: str
    dest: str
    dest_name: str
    pickup_eta: datetime | None
    pickup_eta_text: str
    driver: str
    driver_phone: str
    gps_url: str
    current_location: str
    last_comment_at: str
    last_comment_by: str
    last_comment: str
    sales_rep: str | None = None
    sales_rep2: str | None = None
    pings: list[Ping] = field(default_factory=list)
    gps_error: str = ""
    gps_checked: bool = False
    tracking: str = ""  # active | stale | never | unknown

    @property
    def url(self) -> str:
        return LOAD_URL + self.load_id

    @property
    def lane(self) -> str:
        return f"{self.origin or '?'} -> {self.dest or '?'}"

    @property
    def is_stellantis(self) -> bool:
        return self.customer_id in STELLANTIS_CUSTOMER_IDS or bool(_STELLANTIS_RE.search(self.customer or ""))

    @property
    def dispatch_key(self) -> str:
        return _name_key(self.dispatch_rep) if self.dispatch_rep else UNASSIGNED

    @property
    def sales_key(self) -> str:
        return _name_key(self.sales_rep) if self.sales_rep else UNASSIGNED

    @property
    def group_key(self) -> tuple[str, str]:
        return (self.dispatch_key, "stellantis") if self.is_stellantis else (self.dispatch_key, self.sales_key)

    @property
    def last_ping(self) -> Ping | None:
        return self.pings[-1] if self.pings else None

    def hours_since_ping(self, now: datetime) -> float | None:
        ping = self.last_ping
        if ping is None:
            return None
        return (now - ping.when).total_seconds() / 3600

    def to_record(self, now: datetime) -> dict[str, Any]:
        record = asdict(self)
        record["pickup_eta"] = self.pickup_eta.isoformat(timespec="minutes") if self.pickup_eta else None
        record["pings"] = len(self.pings)
        record["last_ping"] = self.last_ping.to_record() if self.last_ping else None
        since = self.hours_since_ping(now)
        record["hours_since_ping"] = round(since, 1) if since is not None else None
        record["is_stellantis"] = self.is_stellantis
        record["url"] = self.url
        return record


def _customer_of(row: GridRow) -> tuple[str, str]:
    """``(name, id)`` from the added customer columns: ``original_consignor`` (the real
    customer, also on EDI loads where ``consignor`` reads N/A) with ``consignor`` as the
    fallback."""

    name = _plain(row.text("original_consignor"))
    if not name or name.upper() == "N/A":
        name = _plain(row.text("consignor"))
    if name.upper() == "N/A":
        name = ""
    return name, _plain(row.text("cons_id"))


def load_from_row(row: GridRow) -> GpsLoad:
    """One grid row -> a :class:`GpsLoad` (no GPS check yet)."""

    dest_lines = [l for l in row.lines("destination") if l.strip()]
    if not dest_lines:
        dest_name, dest = "", ""
    elif len(dest_lines) > 1:
        dest_name, dest = dest_lines[0], dest_lines[-1]
    else:
        dest_name, dest = "", dest_lines[0]
    gps_cell = row.cells.get("GPSLink")
    href = _HREF_RE.search(gps_cell.html) if gps_cell else None
    gps_url = html_lib.unescape(href.group(1)) if href else ""
    if gps_url.startswith("/"):
        gps_url = BASE_URL + gps_url
    comment_lines = [l for l in row.lines("DispatchCmt_IDLastCmt") if l.strip()]
    customer, customer_id = _customer_of(row)
    eta_text = _plain(row.text("pickiup_eta_datetime"))
    reps: dict[str, Any] = {}
    if "sales_rep1" in row.cells:
        reps = {"sales_rep": _plain(row.text("sales_rep1")) or None, "sales_rep2": _plain(row.text("sales_rep2")) or None}
    return GpsLoad(
        load_id=row.load_id or "",
        kind="load" if row.link_class == "LinkBundleParent" else "order",
        customer=customer,
        customer_id=customer_id,
        dispatch_rep=_plain(row.text("dispatch_rep")) or None,
        status=_plain(row.text("status")),
        carrier=_plain(row.text("carriername")) or re.sub(r"^Carrier:\s*", "", _plain(row.section), flags=re.I),
        origin=_plain(row.text("originCityState")),
        dest=_plain(dest),
        dest_name=_plain(dest_name),
        pickup_eta=parse_stamp(eta_text),
        pickup_eta_text=eta_text,
        driver=_plain(row.text("driver_name")),
        driver_phone=_plain(row.text("driver_cell_phone")),
        gps_url=gps_url,
        current_location=_plain(row.text("currentlocation")),
        last_comment_at=_plain(comment_lines[0]) if comment_lines else "",
        last_comment_by=_plain(comment_lines[1]) if len(comment_lines) > 1 else "",
        last_comment=_plain(" ".join(comment_lines[2:])) if len(comment_lines) > 2 else "",
        **reps,
    )


def customer_excluded(load: GpsLoad, patterns: tuple[str, ...] | list[str]) -> bool:
    """True when the load's customer matches one of ``patterns``: an all-digit pattern is
    a customer id, anything else a case-insensitive part of the customer name."""

    name = (load.customer or "").lower()
    for pattern in patterns:
        pattern = pattern.strip()
        if not pattern:
            continue
        if pattern.isdigit():
            if load.customer_id == pattern:
                return True
        elif pattern.lower() in name:
            return True
    return False


def screen_loads(grid: Grid, *, statuses: tuple[str, ...] | list[str],
                 exclude_customers: tuple[str, ...] | list[str] = DEFAULT_EXCLUDE_CUSTOMERS) -> list[GpsLoad]:
    """Every load row on the screen in ``statuses`` (no pickup-ETA rule), minus the excluded
    customers and the bundle members: the loads a MacroPoint location may be written on."""

    wanted = {s.strip().lower() for s in statuses if s.strip()}
    out: list[GpsLoad] = []
    for row in grid.rows:
        if not row.load_id or row.link_class == "LinkBundleChild":
            continue
        load = load_from_row(row)
        if wanted and load.status.lower() not in wanted:
            continue
        if customer_excluded(load, exclude_customers):
            continue
        out.append(load)
    return out


def split_by_gps_age(loads: list[GpsLoad], now: datetime, fallback_hours: float = DEFAULT_GPS_FALLBACK_HOURS
                     ) -> tuple[list[GpsLoad], list[GpsLoad]]:
    """``(gps_link, macropoint)``: the loads whose GPS-link ping is at most ``fallback_hours``
    old, and the loads with no ping at all or only older ones (those use MacroPoint). A load
    whose GPS page could not be read is in neither."""

    gps_link: list[GpsLoad] = []
    macropoint: list[GpsLoad] = []
    for load in loads:
        if load.tracking == UNKNOWN:
            continue
        since = load.hours_since_ping(now)
        (gps_link if since is not None and since <= fallback_hours else macropoint).append(load)
    return gps_link, macropoint


def plan_cargotel_locations(loads: list[GpsLoad], state: LocationState) -> list[LocationPlan]:
    """The loads with any ping on their GPS City/St link (active or stale) whose last ping is
    not yet in Location/ETA: newer than the ping last written there, from either source."""

    out: list[LocationPlan] = []
    for load in loads:
        ping = load.last_ping
        if load.tracking not in (ACTIVE, STALE) or ping is None or not ping.where:
            continue
        city, _sep, st = ping.where.rpartition(",")
        mp = MpPing(load_id=load.load_id, when=ping.when.astimezone(timezone.utc), city=city.strip() if _sep else ping.where,
                    state=st.strip() if _sep else "", postal="", lat=None, lng=None, locator="", source="CargoTel")
        prev = state.get(load.load_id)
        if prev:
            prev_when = _parse_iso(prev.get("ping_utc"))
            if prev_when and prev_when >= mp.when:
                continue
        out.append(LocationPlan(load_id=load.load_id, ping=mp, status=load.status, reason="new ping" if prev else "first",
                                source="cargotel"))
    return out


def _parse_iso(text: str | None) -> datetime | None:
    try:
        when = datetime.fromisoformat(text) if text else None
    except ValueError:
        return None
    return when.replace(tzinfo=timezone.utc) if when and when.tzinfo is None else when


def select_loads(
    grid: Grid,
    now: datetime,
    *,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    statuses: tuple[str, ...] | list[str] = DEFAULT_STATUSES,
    exclude_customers: tuple[str, ...] | list[str] = DEFAULT_EXCLUDE_CUSTOMERS,
) -> tuple[list[GpsLoad], list[str], dict[str, int]]:
    """The loads the screen is meant to show: in ``statuses`` with a pickup ETA before
    :func:`eta_cutoff`, minus the customers in ``exclude_customers``. Returns ``(loads,
    warnings, counts)`` with counts ``rows``, ``in_status``, ``excluded``, ``no_eta``,
    ``eta_later``, ``selected``. Refuses to run when the grid lacks a column the job
    reads."""

    missing = [key for key in REQUIRED_COLUMNS if key not in grid.column_keys]
    if missing:
        raise RuntimeError(f"the {SEARCH_NAME} grid lacks column(s) {', '.join(missing)}; check the saved view in CargoTel")
    cutoff = eta_cutoff(now, days_ahead)
    wanted = {s.strip().lower() for s in statuses if s.strip()}
    counts = {"rows": 0, "in_status": 0, "excluded": 0, "no_eta": 0, "eta_later": 0, "selected": 0}
    warnings: list[str] = []
    picked: list[GpsLoad] = []
    for row in grid.rows:
        if not row.load_id or row.link_class == "LinkBundleChild":
            continue
        counts["rows"] += 1
        load = load_from_row(row)
        if wanted and load.status.lower() not in wanted:
            continue
        counts["in_status"] += 1
        if customer_excluded(load, exclude_customers):
            counts["excluded"] += 1
            continue
        if load.pickup_eta is None:
            counts["no_eta"] += 1
            if load.pickup_eta_text:
                warnings.append(f"load {load.load_id}: unreadable pickup ETA {load.pickup_eta_text!r}; skipped")
            continue
        if load.pickup_eta >= cutoff:
            counts["eta_later"] += 1
            continue
        if not load.gps_url:
            warnings.append(f"load {load.load_id}: no GPS link in its row")
        picked.append(load)
    counts["selected"] = len(picked)
    picked.sort(key=lambda l: (l.dispatch_key or "~", l.pickup_eta or datetime.max, l.load_id))
    return picked, warnings, counts


# --------------------------------------------------------------------------- #
# GPS check                                                                    #
# --------------------------------------------------------------------------- #


def classify(load: GpsLoad, now: datetime, stale_hours: float = DEFAULT_STALE_HOURS) -> str:
    if load.gps_error:
        return UNKNOWN
    since = load.hours_since_ping(now)
    if since is None:
        return NEVER
    return STALE if since > stale_hours else ACTIVE


def gps_fetcher(session: CargoTelSession, now: datetime, gps_days: int = DEFAULT_GPS_DAYS) -> Callable[[GpsLoad], str]:
    """Live GPS pages: the load's link with the window set to the last ``gps_days`` (the
    page's own default is 48 hours from the moment it is opened)."""

    def fetch(load: GpsLoad) -> str:
        if not load.gps_url:
            raise RuntimeError("no GPS link")
        url = load.gps_url.split("?", 1)[0]
        params = gps_params(load.load_id, now - timedelta(days=gps_days), now + timedelta(days=1))
        status, html = session.get(url, params)
        if is_signin_page(html, status):
            session.cookie(refresh=True)
            status, html = session.get(url, params)
        if status != 200:
            raise RuntimeError(f"gpshistory.mcgi returned HTTP {status}")
        if is_signin_page(html, status):
            raise RuntimeError("CargoTel answered with the sign-in page; the session cookie is stale")
        return html

    return fetch


def dir_fetcher(folder: str) -> Callable[[GpsLoad], str]:
    """Offline GPS pages: ``<folder>/gps_<load>.html``."""

    def fetch(load: GpsLoad) -> str:
        with open(os.path.join(folder, f"gps_{load.load_id}.html"), encoding="utf-8") as handle:
            return handle.read()

    return fetch


def check_gps(
    loads: list[GpsLoad],
    fetch: Callable[[GpsLoad], str] | None,
    now: datetime,
    *,
    stale_hours: float = DEFAULT_STALE_HOURS,
    max_checks: int = DEFAULT_MAX_GPS,
    save_html: str | None = None,
    log: Callable[[str], None] = lambda m: None,
) -> list[str]:
    """Read every load's GPS history and set ``pings`` and ``tracking``. Returns the
    errors; a load whose page failed is ``unknown`` and never emailed or commented."""

    errors: list[str] = []
    for n, load in enumerate(loads):
        if fetch is None:
            load.gps_error = "no GPS lookup available"
        elif n >= max_checks:
            load.gps_error = f"GPS cap of {max_checks} loads reached"
        else:
            try:
                html = fetch(load)
                if save_html:
                    os.makedirs(save_html, exist_ok=True)
                    with open(os.path.join(save_html, f"gps_{load.load_id}.html"), "w", encoding="utf-8") as handle:
                        handle.write(html)
                load.pings = parse_pings(html, now.tzinfo)
                load.gps_checked = True
            except Exception as exc:  # noqa: BLE001 - one bad page must not sink the run
                load.gps_error = str(exc)
        if load.gps_error:
            errors.append(f"load {load.load_id}: {load.gps_error}")
            log(f"GPS check failed for {load.load_id}: {load.gps_error}")
        load.tracking = classify(load, now, stale_hours)
    return errors


# --------------------------------------------------------------------------- #
# Dispatch comments                                                            #
# --------------------------------------------------------------------------- #


def comment_text(ping: Ping) -> str:
    """``"2026-11-06 06:12:25-05 Oak Lawn, IL"``: the ping as CargoTel prints it, the
    format the team already types by hand (255 characters max in the box)."""

    return f"{ping.stamp} {ping.where}"[:255]


def needs_comment(load: GpsLoad, state: CommentState | None = None) -> bool:
    """A load with a ping, whether the driver is still pinging (active) or went quiet
    (stale), gets its last ping as a comment unless the load's last dispatch comment
    already carries that ping (by anyone) or the job wrote it earlier. A stale load is
    emailed as well; since its last ping does not change, the comment is written once."""

    if load.tracking not in (ACTIVE, STALE) or load.last_ping is None:
        return False
    stamp = load.last_ping.stamp
    if stamp and stamp in load.last_comment:
        return False
    if state is not None and state.written(load.load_id) == stamp:
        return False
    return True


def plan_comments(loads: list[GpsLoad], state: CommentState | None = None) -> dict[str, str]:
    """``{load_id: comment}`` for the active and stale loads whose last ping is not on the
    load yet."""

    return {l.load_id: comment_text(l.last_ping) for l in loads if needs_comment(l, state) and l.last_ping}


_FIELD_RE = re.compile(r"<(input|select|textarea)\b([^>]*)>(?:(.*?)</\1\s*>)?", re.I | re.S)
_OPTION_RE = re.compile(r"<option\b([^>]*)>", re.I)
_ATTR = {name: re.compile(rf'\b{name}="([^"]*)"', re.I) for name in ("name", "value", "type")}


def _attr(attrs: str, name: str) -> str | None:
    match = _ATTR[name].search(attrs)
    return html_lib.unescape(match.group(1)) if match else None


def form_fields(form_html: str) -> list[tuple[str, str]]:
    """Every successful control of a form as ``(name, value)`` pairs in document order,
    the way a browser would post it: hidden and text inputs, checked boxes and radios,
    the selected option(s) of each select (the first option when none is marked, nothing
    for an empty multi-select), every textarea. Buttons are left out."""

    out: list[tuple[str, str]] = []
    for match in _FIELD_RE.finditer(form_html):
        tag, attrs, body = match.group(1).lower(), match.group(2), match.group(3) or ""
        name = _attr(attrs, "name")
        if not name:
            continue
        if tag == "input":
            kind = (_attr(attrs, "type") or "text").lower()
            if kind in ("submit", "button", "image", "reset", "file"):
                continue
            if kind in ("checkbox", "radio") and not re.search(r"\bchecked\b", attrs, re.I):
                continue
            out.append((name, _attr(attrs, "value") or ""))
        elif tag == "select":
            options = _OPTION_RE.findall(body)
            chosen = [o for o in options if re.search(r"\bselected\b", o, re.I)]
            if chosen:
                out.extend((name, _attr(o, "value") or "") for o in chosen)
            elif options and not re.search(r"\bmultiple\b", attrs, re.I):
                out.append((name, _attr(options[0], "value") or ""))
        else:  # textarea
            out.append((name, html_lib.unescape(_TAG_RE.sub("", body))))
    return out


_BOX_RE = re.compile(r'name="Control__DispatchCmt_ID__(\d+)__comment"', re.I)


def _menu_alt_form(page_html: str) -> str | None:
    match = re.search(r'<form\s+name="Menu_alt"', page_html, re.I)
    if not match:
        return None
    end = re.compile(r"</form\s*>", re.I).search(page_html, match.start())
    return page_html[match.start():end.start() if end else len(page_html)]


def comment_boxes(page_html: str) -> list[str]:
    """The load ids with a "Dispatch Comment (Add)" box in the page's Menu_alt form."""

    form = _menu_alt_form(page_html)
    return _BOX_RE.findall(form) if form else []


def build_comment_post(page_html: str, comments: dict[str, str]) -> list[tuple[str, str]]:
    """The POST body that adds ``comments`` (``{load_id: text}``) through the grid's own
    form: the ``Menu_alt`` form of the fetched page, every field as the browser would
    send it, with the "Dispatch Comment (Add)" box of each load filled in and the Submit
    button pressed. Raises when the page has no such form or a load has no box."""

    form = _menu_alt_form(page_html)
    if form is None:
        raise RuntimeError("no Menu_alt form on the page; cannot add dispatch comments")
    boxes = {f"Control__DispatchCmt_ID__{load_id}__comment": text for load_id, text in comments.items()}
    data: list[tuple[str, str]] = []
    seen: set[str] = set()
    for name, value in form_fields(form):
        if name == "Menu__is_submitted":
            continue
        if name in boxes:
            value = boxes[name]
            seen.add(name)
        data.append((name, value))
    missing = sorted(set(boxes) - seen)
    if missing:
        raise RuntimeError("no Dispatch Comment (Add) box on the page for load(s) "
                           + ", ".join(m.split("__")[2] for m in missing))
    data.append(("Menu__is_submitted", "Submit"))
    return data


def session_post(session: CargoTelSession, url: str, data: list[tuple[str, str]], timeout: float = 300.0) -> tuple[int, str]:
    """POST a form with the session cookie (``CargoTelSession`` only knows GET)."""

    import requests

    headers = {**_BROWSER_HEADERS, "cookie": f"cgt-browser-session={session.cookie()}", "origin": BASE_URL, "referer": url}
    response = requests.post(url, headers=headers, data=data, timeout=timeout)
    return response.status_code, response.text


def verify_comments(grid: Grid, comments: dict[str, str]) -> dict[str, bool]:
    """Which comments show as the load's last dispatch comment on a re-fetched grid."""

    rows = {row.load_id: row for row in grid.rows if row.load_id}
    out: dict[str, bool] = {}
    for load_id, text in comments.items():
        row = rows.get(load_id)
        last = _plain(" ".join(row.lines("DispatchCmt_IDLastCmt"))) if row else ""
        out[load_id] = bool(row) and _plain(text) in last
    return out


class CommentState:
    """``comment_state.json``: ``{"written": {load_id: {"ping", "text", "at"}}}``, the last
    ping the job wrote on each load, so a run never writes the same ping twice."""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self.entries: dict[str, dict[str, str]] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.entries = data.get("written", {}) if isinstance(data, dict) else {}

    def written(self, load_id: str) -> str | None:
        return (self.entries.get(load_id) or {}).get("ping")

    def mark(self, load_id: str, ping: str, text: str, now: datetime) -> None:
        self.entries[load_id] = {"ping": ping, "text": text, "at": now.isoformat(timespec="seconds")}

    def prune(self, now: datetime, keep_days: int = 60) -> None:
        cutoff = now - timedelta(days=keep_days)
        for load_id in list(self.entries):
            try:
                at = datetime.fromisoformat(self.entries[load_id]["at"])
                if at.tzinfo is None and now.tzinfo is not None:
                    at = at.replace(tzinfo=now.tzinfo)
                if at < cutoff:
                    del self.entries[load_id]
            except (KeyError, ValueError, TypeError):
                del self.entries[load_id]

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"updated": datetime.now().isoformat(timespec="seconds"), "written": self.entries}, handle, indent=2)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# Who gets the email                                                           #
# --------------------------------------------------------------------------- #


@dataclass
class MailGroup:
    """One email. Groups that resolve to the same To and Cc are merged, so a rep gets
    one email per run."""

    to: list[str]
    cc: list[str]
    routed: bool
    stellantis: bool
    notes: list[str] = field(default_factory=list)
    dispatch_reps: list[str] = field(default_factory=list)  # nice names, first seen first
    sales_reps: list[str] = field(default_factory=list)
    greet_names: list[str] = field(default_factory=list)
    loads: list[GpsLoad] = field(default_factory=list)
    keys: set[tuple[str, str]] = field(default_factory=set)

    @property
    def unassigned(self) -> bool:
        return not self.dispatch_reps and not self.sales_reps

    @property
    def key(self) -> str:
        head = "stellantis|" if self.stellantis else ""
        return head + f"{'+'.join(self.dispatch_reps) or 'nobody'}|{'+'.join(self.sales_reps) or 'nobody'}"

    @property
    def label(self) -> str:
        who = f"{', '.join(self.dispatch_reps) or 'no dispatch rep'} / {', '.join(self.sales_reps) or 'no sales rep'}"
        return ("Stellantis: " if self.stellantis else "") + who

    @property
    def who(self) -> str:
        return ", ".join(self.dispatch_reps) or ", ".join(self.sales_reps)

    def matches(self, wanted: str) -> bool:
        if wanted == "unassigned":
            return self.unassigned
        if wanted == "stellantis":
            return self.stellantis
        return any(wanted in key for key in self.keys)


def _rep_email(name: str | None, reps: dict[str, dict[str, str]]) -> str:
    return reps.get(_name_key(name), {}).get("email", "") if name else ""


def _which(loads: list[GpsLoad]) -> str:
    ids = [l.load_id for l in loads]
    return "the load" if not ids else (f"load {ids[0]}" if len(ids) == 1 else f"loads {', '.join(ids)}")


def address_regular(
    dispatch_rep: str | None, sales_rep: str | None, loads: list[GpsLoad], reps: dict[str, dict[str, str]],
    *, escalate: list[str], default_manager: list[str],
) -> tuple[list[str], list[str], bool, list[str]]:
    """``(to, cc, routed, notes)``: the dispatch rep and the sales rep get To, the sales
    rep's manager (``reps.csv`` ``manager`` column, else ``default_manager``) and the
    secondary sales rep Cc. Nobody addressable -> ``escalate`` gets To (``routed``)."""

    to: list[str] = []
    cc: list[str] = []
    notes: list[str] = []
    which = _which(loads)
    for role, name in (("dispatch rep", dispatch_rep), ("sales rep", sales_rep)):
        if not name:
            notes.append(f"no {role} on {which}")
            continue
        email = _rep_email(name, reps)
        if email:
            to.append(email)
        else:
            notes.append(f"{role} {nice_name(name)} has no email in reps.csv")
    if sales_rep:
        manager = reps.get(_name_key(sales_rep), {}).get("manager", "")
        if manager:
            cc.extend(a.strip() for a in manager.split(";") if a.strip())
        elif _rep_email(sales_rep, reps):
            if default_manager:
                cc.extend(default_manager)
            else:
                notes.append(f"no manager set for {nice_name(sales_rep)} in reps.csv")
    cc.extend(_rep_email(l.sales_rep2, reps) for l in loads if l.sales_rep2)
    to = _dedupe(to)
    if not to:
        return _dedupe(list(escalate)), [], True, notes
    return to, _dedupe(cc, exclude=to), False, notes


def address_stellantis(
    dispatch_rep: str | None, loads: list[GpsLoad], reps: dict[str, dict[str, str]], *, stellantis_to: list[str],
) -> tuple[list[str], list[str], bool, list[str]]:
    """Stellantis loads: To the dispatch rep and the Stellantis mailbox, nobody in Cc.
    With no addressable dispatch rep the mailbox still gets it, with a note."""

    notes: list[str] = []
    to: list[str] = []
    if not dispatch_rep:
        notes.append(f"no dispatch rep on {_which(loads)}")
    else:
        email = _rep_email(dispatch_rep, reps)
        if email:
            to.append(email)
        else:
            notes.append(f"dispatch rep {nice_name(dispatch_rep)} has no email in reps.csv")
    routed = not to
    return _dedupe(to + list(stellantis_to)), [], routed, notes


def mail_groups(
    loads: list[GpsLoad],
    reps: dict[str, dict[str, str]],
    *,
    stellantis_to: list[str] | tuple[str, ...] = DEFAULT_STELLANTIS_TO,
    escalate: list[str] | tuple[str, ...] = DEFAULT_ESCALATE,
    default_manager: list[str] | tuple[str, ...] = (),
) -> list[MailGroup]:
    """Group the loads (Stellantis per dispatch rep; the rest per dispatch rep and sales
    rep), address each group and merge the ones with the same recipients, in first-seen
    order."""

    by_key: dict[tuple[str, str], list[GpsLoad]] = {}
    for load in loads:
        by_key.setdefault(load.group_key, []).append(load)
    out: list[MailGroup] = []
    by_recipients: dict[tuple[bool, bool, frozenset[str], frozenset[str]], MailGroup] = {}
    for key, members in by_key.items():
        first = members[0]
        if first.is_stellantis:
            to, cc, routed, notes = address_stellantis(first.dispatch_rep, members, reps, stellantis_to=list(stellantis_to))
        else:
            to, cc, routed, notes = address_regular(first.dispatch_rep, first.sales_rep, members, reps,
                                                    escalate=list(escalate), default_manager=list(default_manager))
        rkey = (first.is_stellantis, routed, frozenset(a.lower() for a in to), frozenset(a.lower() for a in cc))
        group = by_recipients.get(rkey)
        if group is None:
            group = by_recipients[rkey] = MailGroup(to=list(to), cc=list(cc), routed=routed, stellantis=first.is_stellantis)
            out.append(group)
        names = [first.dispatch_rep] + ([] if first.is_stellantis else [first.sales_rep])
        for lst, name in zip((group.dispatch_reps, group.sales_reps), names):
            nice = nice_name(name) if name else ""
            if nice and nice not in lst:
                lst.append(nice)
        for name in names:
            short = _first_name(nice_name(name)) if name else ""
            if name and _rep_email(name, reps) and short and short not in group.greet_names:
                group.greet_names.append(short)
        for note in notes:
            if note not in group.notes:
                group.notes.append(note)
        group.loads.extend(members)
        group.keys.add(key)
    return out


# --------------------------------------------------------------------------- #
# The email                                                                    #
# --------------------------------------------------------------------------- #


def _sections(stale_hours: float, gps_days: int) -> tuple[tuple[str, str, str, str], ...]:
    hours = f"{stale_hours:g}"
    return (
        (NEVER, "No GPS pings", "with no GPS pings",
         f"The tracking app has not reported a position for this driver in the last {gps_days} days. Call the driver "
         "(or the carrier) to get the app running and get a location, then add it as a dispatch comment on the load."),
        (STALE, f"GPS stopped more than {hours} h ago", f"not pinging for more than {hours} h",
         f"The driver was pinging but the last position is older than {hours} hours. Call the driver for a location "
         "update, get the app back on, and add the location as a dispatch comment on the load."),
    )


def _subject_counts(loads: list[GpsLoad], sections: tuple[tuple[str, str, str, str], ...]) -> str:
    n = len(loads)
    head = f"{n} load{'s' if n != 1 else ''}"
    parts = [(sum(1 for l in loads if l.tracking == kind), phrase) for kind, _title, phrase, _hint in sections]
    parts = [(count, phrase) for count, phrase in parts if count]
    if len(parts) == 1 and parts[0][0] == n:
        return f"{head} {parts[0][1]}"
    return f"{head} ({', '.join(f'{count} {phrase}' for count, phrase in parts)})"


def _fmt_eta(when: datetime | None, fallback: str = "none") -> str:
    if when is None:
        return fallback
    return when.strftime("%a %m/%d %H:%M") if (when.hour or when.minute) else when.strftime("%a %m/%d")


def _local(when: datetime, now: datetime) -> datetime:
    return when.astimezone(now.tzinfo) if now.tzinfo else when


def _fmt_ping(ping: Ping, now: datetime) -> str:
    return f"{_local(ping.when, now):%a %m/%d %H:%M} {ping.where}".strip() + f" ({ago_text((now - ping.when).total_seconds() / 3600)})"


def _last_ping_text(load: GpsLoad, now: datetime, gps_days: int) -> str:
    if load.last_ping is None:
        return f"none in the last {gps_days} days"
    return _fmt_ping(load.last_ping, now)


def _text_block(load: GpsLoad, now: datetime, *, gps_days: int, show_reps: bool) -> str:
    head = (f"{'Load' if load.kind == 'load' else 'Order'} {load.load_id}  |  {load.customer or 'customer ?'}  |  "
            f"last GPS ping: {_last_ping_text(load, now, gps_days)}")
    lines = [head, f"  {load.origin or '?'} -> {load.dest_name + ', ' if load.dest_name else ''}{load.dest or '?'}"]
    carrier = load.carrier or "carrier ?"
    if load.driver:
        carrier += f"  |  driver {nice_name(load.driver)}" + (f" {load.driver_phone}" if load.driver_phone else "")
    lines.append("  " + carrier)
    lines.append(f"  pickup ETA {_fmt_eta(load.pickup_eta)}"
                 + (f"  |  current location (CargoTel): {load.current_location}" if load.current_location else ""))
    if load.last_comment:
        by = nice_name(load.last_comment_by) if load.last_comment_by else "?"
        lines.append(f"  last dispatch comment ({load.last_comment_at}, {by}): {load.last_comment}")
    if show_reps:
        lines.append(f"  dispatch rep: {nice_name(load.dispatch_rep) if load.dispatch_rep else 'none'}; "
                     f"sales rep: {nice_name(load.sales_rep) if load.sales_rep else 'none'}")
    lines.append("  " + load.url + "  |  GPS: " + (load.gps_url or "n/a"))
    return "\n".join(lines)


def _html_rows(loads: list[GpsLoad], now: datetime, *, gps_days: int, show_reps: bool) -> str:
    esc = html_lib.escape
    out = []
    for load in loads:
        carrier = esc(load.carrier or "?")
        if load.driver:
            carrier += (f"<br><small>driver {esc(nice_name(load.driver))}"
                        + (f" {esc(load.driver_phone)}" if load.driver_phone else "") + "</small>")
        if load.last_ping is None:
            ping_cell = f'<b style="color:#b00020">none in the last {gps_days} days</b>'
        else:
            local = _local(load.last_ping.when, now)
            ping_cell = (f'<b style="color:#b00020">{esc(local.strftime("%a %m/%d %H:%M"))}</b> {esc(load.last_ping.where)}'
                         f'<br><small>{esc(ago_text((now - load.last_ping.when).total_seconds() / 3600))}</small>')
        comment = ""
        if load.last_comment:
            comment = (f"{esc(load.last_comment)}<br><small>{esc(load.last_comment_at)}"
                       + (f", {esc(nice_name(load.last_comment_by))}" if load.last_comment_by else "") + "</small>")
        cells = [
            f'<a href="{esc(load.url)}">{esc(load.load_id)}</a>' + ("" if load.kind == "load" else "<br><small>order</small>"),
            esc(load.customer or ""),
            f"{esc(load.origin)}<br>&rarr; {esc(load.dest)}" + (f"<br><small>{esc(load.dest_name)}</small>" if load.dest_name else ""),
            carrier,
            esc(_fmt_eta(load.pickup_eta)),
            ping_cell + (f'<br><a href="{esc(load.gps_url)}">GPS history</a>' if load.gps_url else ""),
            esc(load.current_location),
            comment,
        ]
        if show_reps:
            cells.append(esc(nice_name(load.dispatch_rep) if load.dispatch_rep else "none") + "<br><small>sales: "
                         + esc(nice_name(load.sales_rep) if load.sales_rep else "none") + "</small>")
        out.append("<tr>" + "".join(
            f'<td style="padding:4px 8px;border-bottom:1px solid #ddd;vertical-align:top">{c}</td>' for c in cells) + "</tr>")
    return "\n".join(out)


FOOTNOTE = ("GPS pings come from each load's \"GPS City/St Link\" on the tracking screen (the driver app's position history). "
            "Every load with a ping gets its last position written into the dispatch comments automatically, whether the driver "
            "is still pinging or has gone quiet; a load with no ping at all gets no comment.")


def _trailer(repeat_hours: float) -> str:
    return (f"This alert is generated from {SEARCH_NAME} ({SEARCH_PATH}) and repeats every {repeat_hours:g} hours while a load's "
            "driver is still not pinging. It stops as soon as pings resume or the load leaves the screen. Reply to this email if a "
            "load is mis-assigned.")


def compose(group: MailGroup, *, now: datetime, stale_hours: float = DEFAULT_STALE_HOURS, gps_days: int = DEFAULT_GPS_DAYS,
            repeat_hours: float = DEFAULT_REPEAT_HOURS) -> Email:
    """The alert for one addressed group, plain text and HTML: one section per state."""

    sections = _sections(stale_hours, gps_days)
    loads = sorted(group.loads, key=lambda l: (l.pickup_eta or datetime.max, l.load_id))
    if group.unassigned:
        who_text = "no rep assigned"
    elif group.routed:
        who_text = f"{group.who} (no email on file)"
    else:
        who_text = group.who
    prefix = "Stellantis GPS tracking alert" if group.stellantis else "GPS tracking alert"
    subject = f"{prefix}: {_subject_counts(loads, sections)} - {who_text}"

    names = group.greet_names
    greeting = (f"Hi {' and '.join(names) if len(names) <= 2 else ', '.join(names[:-1]) + ' and ' + names[-1]},"
                if (names and not group.routed) else "Hi all,")
    intro = (f"As of {_fmt_when(now)}, the drivers on these {'Stellantis ' if group.stellantis else ''}loads on {SEARCH_NAME} "
             f"({SEARCH_PATH}) are not pinging. Please get a location update from each driver.")
    if group.routed:
        intro += (" Nobody could be emailed directly (" + "; ".join(group.notes) + "), so this alert comes to you instead; "
                  "the reps are named per load below.")
    elif group.notes:
        intro += " Note: " + "; ".join(group.notes) + "."

    show_reps = group.routed or len(group.dispatch_reps) + len(group.sales_reps) > 2
    placed: list[tuple[str, str, list[GpsLoad]]] = []
    for kind, title, _phrase, hint in sections:
        members = [l for l in loads if l.tracking == kind]
        if members:
            placed.append((title, hint, members))
    text_parts = [greeting, "", intro, ""]
    for title, hint, members in placed:
        text_parts += [f"{title.upper()} ({len(members)})", hint, ""]
        text_parts += [_text_block(l, now, gps_days=gps_days, show_reps=show_reps) + "\n" for l in members]
    text_parts += [FOOTNOTE, _trailer(repeat_hours)]
    text = "\n".join(text_parts)

    esc = html_lib.escape
    headers = ["Load", "Customer", "Lane", "Carrier / driver", "Pickup ETA", "Last GPS ping", "Current location",
               "Last dispatch comment"] + (["Dispatch / sales rep"] if show_reps else [])
    th = "".join(f'<th style="text-align:left;padding:4px 8px;border-bottom:2px solid #999">{h}</th>' for h in headers)
    blocks = []
    for title, hint, members in placed:
        blocks.append(f'<h3 style="margin:16px 0 2px">{esc(title)} ({len(members)})</h3>'
                      f'<p style="margin:0 0 6px;color:#444">{esc(hint)}</p>'
                      f'<table style="border-collapse:collapse;font-size:13px"><tr>{th}</tr>'
                      f"{_html_rows(members, now, gps_days=gps_days, show_reps=show_reps)}</table>")
    html = ('<html><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">'
            f"<p>{esc(greeting)}</p><p>{esc(intro)}</p>" + "".join(blocks)
            + f'<p style="font-size:12px;color:#666;margin-top:16px">{esc(FOOTNOTE)}<br>{esc(_trailer(repeat_hours))}</p>'
            "</body></html>")
    return Email(key=group.key, rep=group.who or None, to=list(group.to), cc=list(group.cc), subject=subject, text=text, html=html,
                 load_ids=[l.load_id for l in loads], routed=group.routed)


# --------------------------------------------------------------------------- #
# The daily report                                                             #
# --------------------------------------------------------------------------- #


def parse_report_hours(text: str | None) -> tuple[int, ...] | str:
    """``"8"`` or ``"8,14"``: the hours of day whose run sends the report; ``"all"``: every
    run; ``""``, ``"never"`` or ``"-"``: no report."""

    value = (text or "").strip().lower()
    if value in ("", "never", "none", "off", "-"):
        return ()
    if value in ("all", "every", "*"):
        return "all"
    hours: set[int] = set()
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not 0 <= int(part) <= 23:
            raise ValueError(f"report hour must be 0-23, 'all' or 'never', not {part!r}")
        hours.add(int(part))
    return tuple(sorted(hours))


def report_due(now: datetime, report_hours: tuple[int, ...] | str) -> bool:
    return report_hours == "all" or now.hour in report_hours


def _customer_key(load: GpsLoad) -> str:
    # By name, not id: CargoTel has customer records that share a name (two "Circle Logistics"
    # on 2026-11-09), and the reader thinks in names.
    return (load.customer or "").strip().lower() or (f"id {load.customer_id}" if load.customer_id else "?")


def _customer_label(load: GpsLoad) -> str:
    return load.customer or (f"customer {load.customer_id}" if load.customer_id else "customer ?")


def _dispatch_label(load: GpsLoad) -> str:
    return nice_name(load.dispatch_rep) if load.dispatch_rep else "no dispatch rep"


def _report_key(load: GpsLoad) -> tuple[Any, ...]:
    return ((load.customer or "~").lower(), load.customer_id, load.dispatch_key or "~", load.pickup_eta or datetime.max, load.load_id)


@dataclass(frozen=True)
class SteadyRule:
    """A driver "pinged steadily" in the last ``hours`` when the GPS link has at least
    ``pings`` pings in them, none more than ``gap_hours`` apart. ``hours=0`` switches it off."""

    hours: float = DEFAULT_REPORT_STEADY_HOURS
    pings: int = DEFAULT_REPORT_STEADY_PINGS
    gap_hours: float = DEFAULT_REPORT_STEADY_GAP_HOURS

    def applies(self, load: GpsLoad, now: datetime) -> bool:
        if self.hours <= 0:
            return False
        start = now - timedelta(hours=self.hours)
        recent = sorted(p.when for p in load.pings if start <= p.when <= now)
        if len(recent) < max(self.pings, 1):
            return False
        return all((b - a).total_seconds() <= self.gap_hours * 3600 for a, b in zip(recent, recent[1:]))

    def describe(self) -> str:
        return f"at least {self.pings} pings in the past {self.hours:g} hours, none more than {self.gap_hours:g} hours apart"


DEFAULT_STEADY = SteadyRule()


def is_quiet(load: GpsLoad, now: datetime, quiet_hours: float) -> bool:
    return load.tracking in (ACTIVE, STALE) and (load.hours_since_ping(now) or 0.0) > quiet_hours


def report_loads(loads: list[GpsLoad], now: datetime, quiet_hours: float = DEFAULT_REPORT_QUIET_HOURS,
                 steady: SteadyRule | None = DEFAULT_STEADY) -> tuple[list[GpsLoad], list[GpsLoad]]:
    """``(never, quiet)`` for the report: the loads with no ping in the GPS window, and the
    loads whose driver pinged but not within ``quiet_hours`` (its own threshold, apart from
    the 4 h one the alerts use), minus those who pinged steadily in the past 16 hours
    (``steady``). Both sorted by customer, dispatch rep, pickup ETA."""

    never = [l for l in loads if l.tracking == NEVER]
    quiet = [l for l in loads if report_bucket(l, now, quiet_hours, steady) == QUIET]
    return sorted(never, key=_report_key), sorted(quiet, key=_report_key)


def report_bucket(load: GpsLoad, now: datetime, quiet_hours: float = DEFAULT_REPORT_QUIET_HOURS,
                  steady: SteadyRule | None = DEFAULT_STEADY) -> str:
    """How the report counts a load: ``never`` (no ping in the window), ``quiet`` (last
    ping older than ``quiet_hours`` and no steady pinging in the past 16 hours), ``ok``
    (pinged within them, or quiet after steady pinging) or ``unknown`` (GPS page
    unreadable). ``never`` and ``quiet`` are the "issues"."""

    if load.tracking == UNKNOWN:
        return UNKNOWN
    if load.tracking == NEVER:
        return NEVER
    if is_quiet(load, now, quiet_hours) and not (steady and steady.applies(load, now)):
        return QUIET
    return OK


def steady_quiet_loads(loads: list[GpsLoad], now: datetime, quiet_hours: float = DEFAULT_REPORT_QUIET_HOURS,
                       steady: SteadyRule | None = DEFAULT_STEADY) -> list[GpsLoad]:
    """The loads quiet for more than ``quiet_hours`` that the steady rule takes off the list."""

    if not steady:
        return []
    return sorted((l for l in loads if is_quiet(l, now, quiet_hours) and steady.applies(l, now)), key=_report_key)


QUIET = "quiet"
OK = "ok"
TALLY_KEYS = ("scanned", NEVER, QUIET, OK, UNKNOWN)


def _tally() -> dict[str, int]:
    return {k: 0 for k in TALLY_KEYS}


def _tally_add(tally: dict[str, int], bucket: str) -> None:
    tally["scanned"] += 1
    tally[bucket] += 1


def _issues(tally: dict[str, int]) -> int:
    return tally[NEVER] + tally[QUIET]


def _status_groups(loads: list[GpsLoad], status_order: tuple[str, ...] | list[str]) -> list[tuple[str, list[GpsLoad]]]:
    """``[(status label, loads)]`` in the configured order (every configured status, even
    with no loads), then any other status seen."""

    groups: list[tuple[str, list[GpsLoad]]] = []
    seen: set[str] = set()
    for label in list(status_order) + [l.status for l in loads]:
        key = label.strip().lower()
        if key and key not in seen:
            seen.add(key)
            groups.append((label.strip(), [l for l in loads if l.status.strip().lower() == key]))
    return groups


def report_breakdowns(loads: list[GpsLoad], now: datetime, quiet_hours: float = DEFAULT_REPORT_QUIET_HOURS,
                      status_order: tuple[str, ...] | list[str] = DEFAULT_STATUSES + DEFAULT_REPORT_STATUSES,
                      steady: SteadyRule | None = DEFAULT_STEADY) -> dict[str, Any]:
    """The counts behind the report. ``"statuses"``: the status labels in order;
    ``"status"``: ``[(status, tally)]`` plus ``("All", tally)``; ``"customer"`` and
    ``"dispatch_rep"``: ``[(label, {status: tally}, all_tally)]``, the most issues first.
    A tally is ``{scanned, never, quiet, ok, unknown}``."""

    groups = _status_groups(loads, status_order)
    statuses = [label for label, _members in groups]
    by_status: list[tuple[str, dict[str, int]]] = []
    total = _tally()
    for label, members in groups:
        tally = _tally()
        for load in members:
            bucket = report_bucket(load, now, quiet_hours, steady)
            _tally_add(tally, bucket)
            _tally_add(total, bucket)
        by_status.append((label, tally))
    out: dict[str, Any] = {"statuses": statuses, "status": by_status + [("All", total)]}
    for name, key, label_of in (("customer", _customer_key, _customer_label),
                                ("dispatch_rep", lambda l: l.dispatch_key or "~", _dispatch_label)):
        rows: dict[str, tuple[str, dict[str, dict[str, int]], dict[str, int]]] = {}
        for label, members in groups:
            for load in members:
                k = key(load)
                if k not in rows:
                    rows[k] = (label_of(load), {s: _tally() for s in statuses}, _tally())
                bucket = report_bucket(load, now, quiet_hours, steady)
                _tally_add(rows[k][1][label], bucket)
                _tally_add(rows[k][2], bucket)
        out[name] = sorted(rows.values(), key=lambda r: (-_issues(r[2]), -r[2]["scanned"], r[0].lower()))
    return out


def _report_greeting(to: list[str], reps: dict[str, dict[str, str]] | None) -> str:
    by_email = {(v.get("email") or "").strip().lower(): k for k, v in (reps or {}).items() if v.get("email")}
    names = []
    for address in to:
        key = by_email.get(address.strip().lower())
        if key:
            names.append(_first_name(nice_name(key)))
        else:
            local = address.split("@")[0]
            names.append(local.split(".")[0].capitalize() if "." in local else "")
    if not names or not all(names):
        return "Hi all,"
    if len(names) == 1:
        return f"Hi {names[0]},"
    return f"Hi {', '.join(names[:-1])} and {names[-1]},"


def _text_table(headers: list[str], rows: list[tuple[Any, ...] | list[Any]],
                groups: list[tuple[str, int, int]] | None = None) -> list[str]:
    """A fixed-width table; ``groups`` = ``[(title, first column, span)]`` adds a row of
    column-group titles above the headers."""

    widths = [max([len(str(h))] + [len(str(r[i])) for r in rows]) for i, h in enumerate(headers)]

    def fmt(cells: tuple[Any, ...] | list[Any]) -> str:
        return "  ".join(str(c).ljust(w) if i == 0 else str(c).rjust(w) for i, (c, w) in enumerate(zip(cells, widths)))

    lines = []
    if groups:
        parts = [" " * widths[0]]
        for title, first, span in groups:
            width = sum(widths[first:first + span]) + 2 * (span - 1)
            parts.append(title.ljust(width)[:width])
        lines.append(("  " + "  ".join(parts)).rstrip())
    lines += ["  " + fmt(headers), "  " + fmt(["-" * w for w in widths])] + ["  " + fmt(r) for r in rows]
    return lines


def _report_line(load: GpsLoad, now: datetime) -> str:
    bits = [f"{'Load' if load.kind == 'load' else 'Order'} {load.load_id}", load.status or "status ?",
            f"{load.origin or '?'} -> {load.dest or '?'}", f"pickup ETA {_fmt_eta(load.pickup_eta)}"]
    carrier = load.carrier or "carrier ?"
    if load.driver:
        carrier += f", driver {nice_name(load.driver)}" + (f" {load.driver_phone}" if load.driver_phone else "")
    bits.append(carrier)
    if load.last_ping is not None:
        bits.append(f"last ping {_fmt_ping(load.last_ping, now)}")
    bits.append(f"sales rep {nice_name(load.sales_rep) if load.sales_rep else 'none'}")
    if load.last_comment:
        bits.append(f"last dispatch comment ({load.last_comment_at}): {load.last_comment}")
    return "  |  ".join(bits)


def _report_text_section(title: str, members: list[GpsLoad], now: datetime, hint: str) -> list[str]:
    lines = [f"{title.upper()} ({len(members)})", hint, ""]
    if not members:
        return lines + ["  none", ""]
    i = 0
    while i < len(members):
        customer = [l for l in members if _customer_key(l) == _customer_key(members[i])]
        lines.append(f"{_customer_label(members[i])} ({len(customer)})")
        j = 0
        while j < len(customer):
            rep = [l for l in customer if l.dispatch_key == customer[j].dispatch_key]
            lines.append(f"  {_dispatch_label(customer[j])} ({len(rep)})")
            lines += ["    " + _report_line(l, now) for l in rep]
            j += len(rep)
        lines.append("")
        i += len(customer)
    return lines


_TD = '<td style="padding:4px 8px;border-bottom:1px solid #ddd;vertical-align:top">'
_TH = '<th style="text-align:left;padding:4px 8px;border-bottom:2px solid #999">'
_TD_NUM = '<td style="padding:4px 8px;border-bottom:1px solid #ddd;text-align:right">'


def _report_html_table(members: list[GpsLoad], now: datetime, *, with_ping: bool) -> str:
    esc = html_lib.escape
    headers = ["Customer", "Dispatch rep", "Load", "Status", "Lane", "Carrier / driver", "Pickup ETA"] \
        + (["Last GPS ping"] if with_ping else []) + ["Sales rep", "Last dispatch comment"]
    rows = []
    for load in members:
        carrier = esc(load.carrier or "?")
        if load.driver:
            carrier += (f"<br><small>driver {esc(nice_name(load.driver))}" + (f" {esc(load.driver_phone)}" if load.driver_phone else "") + "</small>")
        comment = ""
        if load.last_comment:
            comment = (f"{esc(load.last_comment)}<br><small>{esc(load.last_comment_at)}"
                       + (f", {esc(nice_name(load.last_comment_by))}" if load.last_comment_by else "") + "</small>")
        cells = [esc(_customer_label(load)), esc(_dispatch_label(load)),
                 f'<a href="{esc(load.url)}">{esc(load.load_id)}</a>' + ("" if load.kind == "load" else "<br><small>order</small>")
                 + (f'<br><small><a href="{esc(load.gps_url)}">GPS history</a></small>' if load.gps_url else ""),
                 esc(load.status or "?"),
                 f"{esc(load.origin)}<br>&rarr; {esc(load.dest)}" + (f"<br><small>{esc(load.dest_name)}</small>" if load.dest_name else ""),
                 carrier, esc(_fmt_eta(load.pickup_eta))]
        if with_ping:
            if load.last_ping is None:
                cells.append("none")
            else:
                local = _local(load.last_ping.when, now)
                cells.append(f'<b style="color:#b00020">{esc(local.strftime("%a %m/%d %H:%M"))}</b> {esc(load.last_ping.where)}'
                             f"<br><small>{esc(ago_text((now - load.last_ping.when).total_seconds() / 3600))}</small>")
        cells += [esc(nice_name(load.sales_rep) if load.sales_rep else "none"), comment]
        rows.append("<tr>" + "".join(_TD + c + "</td>" for c in cells) + "</tr>")
    th = "".join(_TH + esc(h) + "</th>" for h in headers)
    return f'<table style="border-collapse:collapse;font-size:13px"><tr>{th}</tr>{"".join(rows)}</table>'


def _num(n: int, strong: bool = False) -> str:
    return _TD_NUM + (f"<b>{n}</b>" if strong else str(n)) + "</td>"


def _issue_num(n: int) -> str:
    return _TD_NUM + (f'<b style="color:#b00020">{n}</b>' if n else "0") + "</td>"


def _rate(tally: dict[str, int]) -> str:
    """The tracking rate: the share of the scanned loads that pinged within the quiet window."""

    return f"{100.0 * tally[OK] / tally['scanned']:.2f}%" if tally["scanned"] else "n/a"


def _sum_tallies(tallies: list[dict[str, int]]) -> dict[str, int]:
    total = _tally()
    for t in tallies:
        for k in TALLY_KEYS:
            total[k] += t[k]
    return total


def _tally_cells(t: dict[str, int], show_unknown: bool) -> list[Any]:
    return [t[NEVER], t[QUIET], t[OK], _issues(t), t["scanned"]] + ([t[UNKNOWN]] if show_unknown else []) + [_rate(t)]


def _tally_columns(hours: str, show_unknown: bool) -> list[str]:
    return ["No Pings", f"Quiet > {hours} h", "OK", "Total Issues", "Total Scanned"] + (["Unreadable"] if show_unknown else []) + ["Tracking Rate"]


def _tally_row_html(label: str, t: dict[str, int], show_unknown: bool, strong: bool = False) -> str:
    esc = html_lib.escape
    cells = [_TD + (f"<b>{esc(label)}</b>" if strong else esc(label)) + "</td>", _issue_num(t[NEVER]), _issue_num(t[QUIET]),
             _num(t[OK], strong), _issue_num(_issues(t)), _num(t["scanned"], strong)]
    if show_unknown:
        cells.append(_num(t[UNKNOWN], strong))
    cells.append(_TD_NUM + f'<b style="color:#1a7f37">{_rate(t)}</b></td>')
    return "<tr>" + "".join(cells) + "</tr>"


def _tally_table_html(first: str, rows: list[tuple[str, dict[str, int]]], hours: str, show_unknown: bool, *, total: bool) -> str:
    """One row per label, the issue counts in red, the tracking rate in green, a Total row."""

    esc = html_lib.escape
    th = "".join(_TH + esc(h) + "</th>" for h in [first] + _tally_columns(hours, show_unknown))
    body = "".join(_tally_row_html(label, t, show_unknown, strong=(label == "All")) for label, t in rows)
    if total:
        body += _tally_row_html("Total", _sum_tallies([t for _l, t in rows]), show_unknown, True)
    return f'<table style="border-collapse:collapse;font-size:13px"><tr>{th}</tr>{body}</table>'


def _tally_table_text(first: str, rows: list[tuple[str, dict[str, int]]], hours: str, show_unknown: bool, *, total: bool) -> list[str]:
    headers = [first] + [c.lower() if c != "OK" else c for c in _tally_columns(hours, show_unknown)]
    table: list[list[Any]] = [[label] + _tally_cells(t, show_unknown) for label, t in rows]
    if total:
        table.append(["Total"] + _tally_cells(_sum_tallies([t for _l, t in rows]), show_unknown))
    return _text_table(headers, table)


def compose_report(
    loads: list[GpsLoad],
    *,
    now: datetime,
    to: list[str],
    reps: dict[str, dict[str, str]] | None = None,
    quiet_hours: float = DEFAULT_REPORT_QUIET_HOURS,
    gps_days: int = DEFAULT_GPS_DAYS,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    statuses: tuple[str, ...] | list[str] = DEFAULT_STATUSES,
    report_statuses: tuple[str, ...] | list[str] = DEFAULT_REPORT_STATUSES,
    exclude_customers: tuple[str, ...] | list[str] = DEFAULT_EXCLUDE_CUSTOMERS,
    counts: dict[str, int] | None = None,
    alerts_on: bool = True,
    steady: SteadyRule | None = DEFAULT_STEADY,
) -> Email:
    """The report for the operations manager: the whole screen at a glance. ``loads`` are
    the alert loads (``statuses``) plus the report-only ones (``report_statuses``, In-Route),
    all GPS-checked. Counts per status, then per customer and per dispatch rep (scanned,
    with issues, without issues, per status and overall), then every load with no GPS
    pings and every load whose driver has been quiet for more than ``quiet_hours``,
    listed by customer and dispatch rep. Plain text and HTML."""

    never, quiet = report_loads(loads, now, quiet_hours, steady)
    rested = steady_quiet_loads(loads, now, quiet_hours, steady)
    unknown = [l for l in loads if l.tracking == UNKNOWN]
    hours = f"{quiet_hours:g}"
    breakdown = report_breakdowns(loads, now, quiet_hours, tuple(statuses) + tuple(report_statuses), steady)
    total = breakdown["status"][-1][1]
    show_unknown = total[UNKNOWN] > 0
    cutoff = eta_cutoff(now, days_ahead)
    stamp = f"{now:%a %m/%d %H:%M}"

    issues = _issues(total)
    parts = [(c, phrase) for c, phrase in ((total[NEVER], "with no GPS pings"), (total[QUIET], f"quiet for more than {hours} h")) if c]
    if not issues:
        subject = f"GPS tracking report {stamp}: all {total['scanned']} loads scanned are pinging"
    else:
        subject = (f"GPS tracking report {stamp}: {issues} of {total['scanned']} loads with issues ("
                   + ", ".join(f"{c} {p}" for c, p in parts) + ")")

    greeting = _report_greeting(to, reps)
    left_out = [p for p in exclude_customers if not p.strip().isdigit()]
    excluded = ""
    if counts and counts.get("excluded") and left_out:
        excluded = f"; {counts['excluded']} {', '.join(left_out)} load{'s' if counts['excluded'] != 1 else ''} left out"
    scanned_desc = " and ".join(f"{t['scanned']} {label}" for label, t in breakdown["status"][:-1]) + " loads"
    intro = (f"GPS tracking report as of {_fmt_when(now)}: {SEARCH_NAME} ({SEARCH_PATH}) shows {scanned_desc} with a pickup ETA "
             f"before {cutoff:%m/%d/%Y}{excluded}. Each load's GPS history (its \"GPS City/St Link\") was checked over the last "
             f"{gps_days} days; a load has an issue when it has no ping at all or its last ping is older than {hours} hours"
             + (f" (unless the driver pinged steadily before that: {steady.describe()})" if steady and steady.hours > 0 else "")
             + ". The "
             f"tracking rate is the share of the scanned loads without an issue.")
    sections = [
        ("No GPS pings", never, f"No position from the tracking app in the last {gps_days} days.", False),
        (f"Quiet for more than {hours} h", quiet, f"The driver was pinging, but the last position is older than {hours} hours.", True),
    ]
    if alerts_on:
        trailer = (f"The reps get the hourly GPS alerts for the {'/'.join(statuses)} loads separately (a driver quiet for more than 4 h, "
                   f"or never pinging, repeated every 4 h); the {'/'.join(report_statuses) or 'other'} loads are scanned for this report only "
                   f"and get no comments or alerts. Generated from {SEARCH_NAME}; reply to this email if a load is mis-assigned.")
    else:
        trailer = (f"The hourly GPS alerts to the reps are switched off, so this report is the only email about these loads. The "
                   f"{'/'.join(statuses)} loads still get their last ping written as a dispatch comment every hour; the "
                   f"{'/'.join(report_statuses) or 'other'} loads are scanned for this report only. Generated from {SEARCH_NAME}; reply to "
                   "this email if a load is mis-assigned.")

    by_customer = [(label, total) for label, _per, total in breakdown["customer"]]
    by_rep = [(label, total) for label, _per, total in breakdown["dispatch_rep"]]
    text_parts = [greeting, "", intro, ""]
    text_parts += ["SCANNED"] + _tally_table_text("status", breakdown["status"], hours, show_unknown, total=False) + [""]
    text_parts += ["BY CUSTOMER"] + _tally_table_text("customer", by_customer, hours, show_unknown, total=True) + [""]
    text_parts += ["BY DISPATCH REP"] + _tally_table_text("dispatch rep", by_rep, hours, show_unknown, total=True) + [""]
    for title, members, hint, _with_ping in sections:
        text_parts += _report_text_section(title, members, now, hint)
    if rested and steady:
        text_parts += [f"NOT COUNTED AS ISSUES ({len(rested)}): quiet for more than {hours} h, but the driver pinged steadily before "
                       f"that ({steady.describe()}), e.g. parked for the night: " + ", ".join(l.load_id for l in rested), ""]
    if unknown:
        text_parts += [f"GPS PAGE UNREADABLE ({len(unknown)}): " + ", ".join(l.load_id for l in unknown), ""]
    text_parts.append(trailer)
    text = "\n".join(text_parts)

    esc = html_lib.escape
    blocks = [_tally_table_html("Status", breakdown["status"], hours, show_unknown, total=False),
              f'<h3 style="margin:16px 0 4px">By customer</h3>' + _tally_table_html("Customer", by_customer, hours, show_unknown, total=True),
              f'<h3 style="margin:16px 0 4px">By dispatch rep</h3>' + _tally_table_html("Dispatch rep", by_rep, hours, show_unknown, total=True)]
    for title, members, hint, with_ping in sections:
        blocks.append(f'<h3 style="margin:20px 0 2px">{esc(title)} ({len(members)})</h3><p style="margin:0 0 6px;color:#444">{esc(hint)}</p>'
                      + (_report_html_table(members, now, with_ping=with_ping) if members else "<p>none</p>"))
    if rested and steady:
        blocks.append(f'<p style="color:#444">Not counted as issues ({len(rested)}): quiet for more than {esc(hours)} h, but the driver '
                      f"pinged steadily before that ({esc(steady.describe())}), e.g. parked for the night: "
                      + ", ".join(f'<a href="{esc(l.url)}">{esc(l.load_id)}</a>' for l in rested) + "</p>")
    if unknown:
        blocks.append(f'<p style="color:#444">GPS page unreadable ({len(unknown)}): '
                      + ", ".join(f'<a href="{esc(l.url)}">{esc(l.load_id)}</a>' for l in unknown) + "</p>")
    html = ('<html><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">'
            f"<p>{esc(greeting)}</p><p>{esc(intro)}</p>" + "".join(blocks)
            + f'<p style="font-size:12px;color:#666;margin-top:16px">{esc(trailer)}</p></body></html>')
    return Email(key="report", rep=None, to=list(to), cc=[], subject=subject, text=text, html=html,
                 load_ids=[l.load_id for l in never + quiet])


# --------------------------------------------------------------------------- #
# State and transports                                                         #
# --------------------------------------------------------------------------- #


class OutboxTransport(FileTransport):
    """Dry run: ``outbox/<stamp>_<group>.txt``, one file per email, never overwriting."""

    def send(self, email: Email, from_addr: str) -> str:
        os.makedirs(self.outbox, exist_ok=True)
        who = "_".join(re.sub(r"[^A-Za-z0-9]+", "-", part or "nobody").strip("-").lower() for part in email.key.split("|"))
        base = os.path.join(self.outbox, f"{datetime.now():%Y%m%d-%H%M%S}_{who}")
        path, n = base + ".txt", 1
        while os.path.exists(path):
            n += 1
            path = f"{base}_{n}.txt"
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(email.dump(from_addr))
        return f"file:{path}"


class AlertState:
    """``alert_state.json``: ``{"sent": {load_id: {"tracking", "group", "at"}}, "report": {"slot", "at"}}``.
    A load is alerted again after ``repeat_hours``, or at once when its state or owners
    changed; the report goes once per hour slot (a second run in the same hour, say a
    manual one, does not send it again)."""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self.sent: dict[str, dict[str, str]] = {}
        self.report: dict[str, str] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                self.sent = data.get("sent", {})
                self.report = data.get("report", {}) or {}

    @staticmethod
    def _slot(now: datetime) -> str:
        return now.strftime("%Y-%m-%d %H")

    def report_sent(self, now: datetime) -> bool:
        return self.report.get("slot") == self._slot(now)

    def mark_report(self, now: datetime) -> None:
        self.report = {"slot": self._slot(now), "at": now.isoformat(timespec="seconds")}

    @staticmethod
    def _signature(load: GpsLoad) -> tuple[str, str]:
        return load.tracking, "|".join(load.group_key)

    def should_alert(self, load: GpsLoad, now: datetime, repeat_hours: float) -> bool:
        rec = self.sent.get(load.load_id)
        if not rec:
            return True
        tracking, group = self._signature(load)
        if rec.get("tracking") != tracking or rec.get("group") != group:
            return True
        try:
            at = datetime.fromisoformat(rec["at"])
        except (KeyError, ValueError):
            return True
        if at.tzinfo is None and now.tzinfo is not None:
            at = at.replace(tzinfo=now.tzinfo)
        return now - at >= timedelta(hours=repeat_hours)

    def mark(self, load: GpsLoad, now: datetime) -> None:
        tracking, group = self._signature(load)
        self.sent[load.load_id] = {"tracking": tracking, "group": group, "at": now.isoformat(timespec="seconds")}

    def prune(self, now: datetime, keep_days: int = 60) -> None:
        cutoff = now - timedelta(days=keep_days)
        for load_id in list(self.sent):
            try:
                at = datetime.fromisoformat(self.sent[load_id]["at"])
                if at.tzinfo is None and now.tzinfo is not None:
                    at = at.replace(tzinfo=now.tzinfo)
                if at < cutoff:
                    del self.sent[load_id]
            except (KeyError, ValueError, TypeError):
                del self.sent[load_id]

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"updated": datetime.now().isoformat(timespec="seconds"), "sent": self.sent, "report": self.report}, handle, indent=2)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# One run                                                                      #
# --------------------------------------------------------------------------- #


def grid_params(now: datetime, days_ahead: int = DEFAULT_DAYS_AHEAD) -> dict[str, Any]:
    """The extra GET parameters: the rep and customer columns, and the date parts of the
    Pickup ETA filter (the mnemonic must NOT be sent: CargoTel crashes on it; the date
    parts alone make it drop the filter, which the job then applies locally)."""

    cutoff = eta_cutoff(now, days_ahead)
    return {
        "Menu__ViewFields": list(EXTRA_VIEW_FIELDS),
        "Menu__month_DateStart_PickupETA": str(cutoff.month),
        "Menu__day_DateStart_PickupETA": str(cutoff.day),
        "Menu__year_DateStart_PickupETA": str(cutoff.year),
    }


def fetch_grid_html(session: CargoTelSession, now: datetime, *, saved_id: str = SEARCH_ID, days_ahead: int = DEFAULT_DAYS_AHEAD,
                    save_html: str | None = None, load_ids: list[str] | None = None) -> tuple[str, Grid]:
    """The screen with every row, the extra columns and the Pickup ETA filter dropped;
    with ``load_ids``, only those loads (the screen's "CargoTel IDs" filter, for the
    comment form). Returns the page and the parsed grid."""

    extra = grid_params(now, days_ahead)
    if load_ids:
        extra[COMMENT_FILTER] = "\n".join(load_ids)
    spec = SearchParams(saved_id, per_page="All", extra=extra)  # type: ignore[arg-type]
    status, html, _params = session.fetch_search_html(spec)
    if save_html:
        os.makedirs(save_html, exist_ok=True)
        with open(os.path.join(save_html, f"grid_{datetime.now():%Y%m%d-%H%M}.html"), "w", encoding="utf-8") as handle:
            handle.write(html)
    if status != 200:
        raise RuntimeError(f"CargoTel Search.mcgi returned HTTP {status} for saved search {saved_id}")
    grid = parse_grid(html, status)
    if grid.signin_page:
        raise RuntimeError("CargoTel answered with the sign-in page twice; the S3 session cookie is stale")
    return html, grid


class CommentPoster:
    """Adds dispatch comments through the grid form and checks them on a re-fetched
    grid. ``post(page_html, comments)`` -> ``{load_id: landed}``.

    ``page_html`` (the whole screen the run read) is NOT what gets posted: CargoTel
    re-saves every row of the form it receives, about two seconds a row, and the 320-row
    form ran past the Lambda's 900 s on 2026-11-10. The screen is read again with the
    "CargoTel IDs" filter (:data:`COMMENT_FILTER`) set to the loads being commented, and
    that page's whole Menu_alt form is posted (a trimmed form saves nothing; a narrower
    screen is still a whole form). The check re-reads the same narrow screen. Loads that
    left the screen since the run read it are logged and left for the next run."""

    def __init__(self, session: CargoTelSession, now: datetime, *, saved_id: str = SEARCH_ID,
                 days_ahead: int = DEFAULT_DAYS_AHEAD, log: Callable[[str], None] = lambda m: None) -> None:
        self.session = session
        self.now = now
        self.saved_id = saved_id
        self.days_ahead = days_ahead
        self.log = log

    def _fetch(self, load_ids: list[str]) -> tuple[str, Grid]:
        return fetch_grid_html(self.session, self.now, saved_id=self.saved_id, days_ahead=self.days_ahead, load_ids=load_ids)

    def post(self, page_html: str, comments: dict[str, str]) -> dict[str, bool]:
        started = time.monotonic()
        ids = sorted(comments)
        small_html, small = self._fetch(ids)
        on_screen = set(comment_boxes(small_html))
        others = len({r.load_id for r in small.rows if r.load_id} - set(ids))
        if others:
            self.log(f"comment form: the {COMMENT_FILTER} filter brought {others} other load(s) along ({len(small.rows)} rows); posting them unchanged")
        gone = [load_id for load_id in ids if load_id not in on_screen]
        if gone:
            self.log(f"comment form: {len(gone)} load(s) no longer on the screen, left for the next run: {', '.join(gone)}")
        wanted = {load_id: comments[load_id] for load_id in ids if load_id in on_screen}
        if not wanted:
            return {}
        data = build_comment_post(small_html, wanted)
        fetched = time.monotonic()
        url = f"{SEARCH_URL}?expand_saved={self.saved_id}&expand_saved_type=priv&Menu__perPage=All"
        status, html = session_post(self.session, url, data)
        posted = time.monotonic()
        if status != 200:
            raise RuntimeError(f"CargoTel answered HTTP {status} to the comment form")
        text = _TAG_RE.sub(" ", html)
        error = re.search(r"An Error was encountered.{0,300}", text, re.S)
        if error:
            raise RuntimeError("CargoTel rejected the comment form: " + " ".join(error.group(0).split())[:300])
        _html, grid = self._fetch(sorted(wanted))
        self.log(f"comment form: {len(wanted)} box(es) on a {len(small.rows)}-row screen, {len(data)} fields; read {fetched - started:.0f} s, "
                 f"POST {posted - fetched:.0f} s ({len(comment_boxes(html))} rows back), check {time.monotonic() - posted:.0f} s")
        return verify_comments(grid, wanted)


def write_comment_dump(outbox: str, comments: dict[str, str], loads: list[GpsLoad]) -> str:
    """Dry run: the comments that would be added, one line per load, in ``outbox/``."""

    os.makedirs(outbox, exist_ok=True)
    path = os.path.join(outbox, f"{datetime.now():%Y%m%d-%H%M%S}_comments.txt")
    by_id = {l.load_id: l for l in loads}
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("Dispatch comments that would be added (load | comment | driver | carrier):\n")
        for load_id, text in comments.items():
            load = by_id.get(load_id)
            driver = nice_name(load.driver) if load and load.driver else "?"
            handle.write(f"{load_id} | {text} | {driver} | {load.carrier if load else '?'}\n")
    return path


def run_once(
    page_html: str,
    grid: Grid,
    *,
    now: datetime,
    reps: dict[str, dict[str, str]],
    gps_fetch: Callable[[GpsLoad], str] | None,
    state: AlertState,
    comment_state: CommentState,
    transport: Any,
    poster: Any,
    from_addr: str,
    record: bool,
    post_comments: bool,
    sent_log: str | None,
    outbox: str,
    days_ahead: int = DEFAULT_DAYS_AHEAD,
    statuses: tuple[str, ...] | list[str] = DEFAULT_STATUSES,
    exclude_customers: tuple[str, ...] | list[str] = DEFAULT_EXCLUDE_CUSTOMERS,
    stale_hours: float = DEFAULT_STALE_HOURS,
    gps_days: int = DEFAULT_GPS_DAYS,
    repeat_hours: float = DEFAULT_REPEAT_HOURS,
    stellantis_to: list[str] | tuple[str, ...] = DEFAULT_STELLANTIS_TO,
    escalate: list[str] | tuple[str, ...] = DEFAULT_ESCALATE,
    default_manager: list[str] | tuple[str, ...] = (),
    report_to: list[str] | tuple[str, ...] = DEFAULT_REPORT_TO,
    report_hours: tuple[int, ...] | str = DEFAULT_REPORT_HOURS,
    report_quiet_hours: float = DEFAULT_REPORT_QUIET_HOURS,
    report_steady: SteadyRule | None = DEFAULT_STEADY,
    report_statuses: tuple[str, ...] | list[str] = DEFAULT_REPORT_STATUSES,
    force_report: bool = False,
    send_alerts: bool = True,
    mp_pings: dict[str, MpPing] | None = None,
    location_state: LocationState | None = None,
    location_writer: Any = None,
    post_locations: bool = False,
    mp_max_age_hours: float = MP_MAX_AGE_HOURS,
    mp_same_place_hours: float = MP_SAME_PLACE_HOURS,
    mp_match_phone: bool = True,
    mp_comments: bool = True,
    gps_fallback_hours: float = DEFAULT_GPS_FALLBACK_HOURS,
    max_gps: int = DEFAULT_MAX_GPS,
    save_gps: str | None = None,
    redirect_to: list[str] | None = None,
    only_rep: str | None = None,
    only_loads: set[str] | None = None,
    checkpoint: Callable[[], None] | None = None,
    log: Callable[[str], None] = lambda m: None,
) -> dict[str, Any]:
    """Select, check GPS, comment, group, compose, send. ``record=False`` (dry run)
    leaves the alert state untouched and writes the emails to ``transport`` (the
    outbox); ``post_comments=False`` writes the planned comments to ``outbox/`` instead
    of CargoTel. ``redirect_to`` sends every email to those addresses as a marked test
    copy that is never recorded; ``only_rep`` limits the emails to the groups with that
    rep ("unassigned" and "stellantis" work too); ``only_loads`` limits the whole run
    (GPS checks, comments and emails) to those load ids, for tests. The report goes to
    ``report_to`` when the run's hour is in ``report_hours`` (or ``force_report``), once
    per hour slot; ``only_rep`` runs skip it unless forced. When it is due, the loads in
    ``report_statuses`` (In-Route) are selected and GPS-checked too, for the report alone.
    ``checkpoint`` is called once the emails are out and the alert state is saved, before
    the comment phase. ``send_alerts=False`` skips the emails to the reps altogether (comments
    and report unaffected)."""

    loads, warnings, counts = select_loads(grid, now, days_ahead=days_ahead, statuses=statuses, exclude_customers=exclude_customers)
    for warning in warnings:
        log(warning)
    cutoff = eta_cutoff(now, days_ahead)
    log(f"{SEARCH_NAME}: {counts['rows']} loads, {counts['in_status']} in status {'/'.join(statuses)}, {counts['excluded']} of an excluded "
        f"customer, {len(loads)} with a pickup ETA before {cutoff:%m/%d/%Y} ({counts['eta_later']} later, {counts['no_eta']} without)")
    if only_loads is not None:
        loads = [l for l in loads if l.load_id in only_loads]
        missing = sorted(only_loads - {l.load_id for l in loads})
        log(f"limited to {len(loads)} load(s): {', '.join(l.load_id for l in loads) or 'none'}"
            + (f"; not on the screen: {', '.join(missing)}" if missing else ""))
    gps_errors = check_gps(loads, gps_fetch, now, stale_hours=stale_hours, max_checks=max_gps, save_html=save_gps, log=log)
    by_state = {kind: sum(1 for l in loads if l.tracking == kind) for kind in (ACTIVE, STALE, NEVER, UNKNOWN)}
    log(f"GPS: {by_state[ACTIVE]} active (pinged within {stale_hours:g} h), {by_state[STALE]} stale, {by_state[NEVER]} never pinged "
        f"in {gps_days} days, {by_state[UNKNOWN]} unreadable")

    report_due_now = force_report or (report_due(now, report_hours) and not only_rep)
    extra: list[GpsLoad] = []
    extra_counts: dict[str, int] = {"rows": counts["rows"], "in_status": 0, "excluded": 0, "no_eta": 0, "eta_later": 0, "selected": 0}
    # the In-Route loads: read for the report at its hours, and every run when locations are written (the rule needs
    # to know whether each load has a GPS-link ping)
    if report_statuses and ((report_due_now and report_to) or mp_pings is not None):
        extra, extra_warnings, extra_counts = select_loads(grid, now, days_ahead=days_ahead, statuses=report_statuses,
                                                           exclude_customers=exclude_customers)
        for warning in extra_warnings:
            log(warning)
        if only_loads is not None:
            extra = [l for l in extra if l.load_id in only_loads]
        gps_errors += check_gps(extra, gps_fetch, now, stale_hours=stale_hours, max_checks=max(0, max_gps - len(loads)),
                                save_html=save_gps, log=log)
        extra_state = {kind: sum(1 for l in extra if l.tracking == kind) for kind in (ACTIVE, STALE, NEVER, UNKNOWN)}
        log(f"{'/'.join(report_statuses)} (no alerts): {extra_counts['in_status']} loads, {extra_counts['excluded']} of an excluded customer, "
            f"{len(extra)} with a pickup ETA before {cutoff:%m/%d/%Y}; GPS: {extra_state[ACTIVE]} active, {extra_state[STALE]} stale, "
            f"{extra_state[NEVER]} never pinged, {extra_state[UNKNOWN]} unreadable")

    summary: dict[str, Any] = {"alerts_on": send_alerts, "rows": counts["rows"], "selected": len(loads), "counts": counts, "by_state": by_state,
                               "warnings": warnings, "gps_errors": gps_errors, "comments": {}, "comments_planned": 0,
                               "comments_written": 0, "comments_failed": 0, "emails": [], "skipped_repeat": 0}

    # 1. emails for the loads that are not pinging
    alerts = [l for l in loads if l.tracking in (STALE, NEVER)]
    if redirect_to:
        record = False
    wanted = None
    if only_rep:
        wanted = only_rep.strip().lower() if only_rep.strip().lower() in ("unassigned", "stellantis") else _name_key(only_rep)
    if not send_alerts:
        log(f"alerts off (--no-alerts): {len(alerts)} load(s) not pinging, no emails to the reps")
    groups = mail_groups(alerts, reps, stellantis_to=stellantis_to, escalate=escalate, default_manager=default_manager) if send_alerts else []
    for group in groups:
        if wanted is not None and not group.matches(wanted):
            continue
        fresh = [l for l in group.loads if state.should_alert(l, now, repeat_hours)]
        summary["skipped_repeat"] += len(group.loads) - len(fresh)
        label = group.label
        if not fresh:
            log(f"{label}: {len(group.loads)} load(s), all alerted within the last {repeat_hours:g} h; nothing sent")
            continue
        email = compose(replace(group, loads=fresh), now=now, stale_hours=stale_hours, gps_days=gps_days, repeat_hours=repeat_hours)
        if redirect_to:
            email = redirect(email, redirect_to)
        if not email.to:
            log(f"{label}: nobody to send to; held")
            continue
        try:
            message_id = transport.send(email, from_addr)
        except Exception as exc:  # noqa: BLE001 - keep going with the other groups
            log(f"send failed for {label}: {exc}")
            summary.setdefault("send_errors", []).append(f"{label}: {exc}")
            continue
        verb = "sent" if record else ("test copy sent" if redirect_to else "dry run")
        log(f"{verb}: {email.subject} -> to {', '.join(email.to)}" + (f" cc {', '.join(email.cc)}" if email.cc else "")
            + f" [{message_id}]")
        entry = {"at": now.isoformat(timespec="seconds"), "stellantis": group.stellantis, "dispatch_reps": group.dispatch_reps,
                 "sales_reps": group.sales_reps, "group": group.key, "to": email.to, "cc": email.cc, "subject": email.subject,
                 "loads": email.load_ids, "message_id": message_id, "sent": record,
                 "redirected_to": list(redirect_to) if redirect_to else None}
        summary["emails"].append(entry)
        if record:
            for load in fresh:
                state.mark(load, now)
            append_log(sent_log, entry)

    # 2. the report for the operations manager: the whole screen, by customer and dispatch rep
    summary["report"] = None
    due = report_due_now
    if due and not report_to:
        log("report: due, but nobody to send it to (--report-to is empty); skipped")
    elif due and record and not force_report and state.report_sent(now):
        log(f"report: already sent at {state.report.get('at')}; skipped")
    elif due:
        scanned = loads + extra
        never, quiet = report_loads(scanned, now, report_quiet_hours, report_steady)
        rested = steady_quiet_loads(scanned, now, report_quiet_hours, report_steady)
        tallies = report_breakdowns(scanned, now, report_quiet_hours, tuple(statuses) + tuple(report_statuses), report_steady)["status"]
        email = compose_report(scanned, now=now, to=list(report_to), reps=reps, quiet_hours=report_quiet_hours, gps_days=gps_days,
                               days_ahead=days_ahead, statuses=statuses, report_statuses=report_statuses, exclude_customers=exclude_customers,
                               counts={"excluded": counts["excluded"] + extra_counts["excluded"]}, alerts_on=send_alerts,
                               steady=report_steady)
        if redirect_to:
            email = redirect(email, redirect_to)
        try:
            message_id = transport.send(email, from_addr)
        except Exception as exc:  # noqa: BLE001 - the state still gets saved
            log(f"report: send failed: {exc}")
            summary.setdefault("send_errors", []).append(f"report: {exc}")
        else:
            verb = "report sent" if record else ("report test copy sent" if redirect_to else "report dry run")
            log(f"{verb}: {email.subject} -> to {', '.join(email.to)} [{message_id}]")
            log("report: " + "; ".join(f"{label} {t['scanned']} scanned, {t[NEVER] + t[QUIET]} with issues, {t[OK]} without"
                                       + (f", {t[UNKNOWN]} unreadable" if t[UNKNOWN] else "") for label, t in tallies))
            entry = {"at": now.isoformat(timespec="seconds"), "report": True, "to": email.to, "cc": email.cc, "subject": email.subject,
                     "never": [l.load_id for l in never], "quiet": [l.load_id for l in quiet],
                     "steady_not_counted": [l.load_id for l in rested], "scanned": {label: t for label, t in tallies},
                     "message_id": message_id, "sent": record, "redirected_to": list(redirect_to) if redirect_to else None}
            summary["report"] = entry
            if record:
                state.mark_report(now)
                append_log(sent_log, entry)
    if record:
        state.prune(now)
        state.save()
    if checkpoint is not None:
        # the emails are out: get the alert state off this box before the slow comment phase
        try:
            checkpoint()
        except Exception as exc:  # noqa: BLE001
            log(f"checkpoint failed: {exc}")

    # 3. Locations into Location/ETA for every load on the screen (Dispatched and In-Route): a load with any ping on its
    # GPS City/St link gets that last ping (its comment comes from step 4, as before); a load with no ping there at all
    # gets MacroPoint's latest location, and a comment labelled (MacroPoint).
    summary["locations"] = {"on_macropoint": 0, "planned": 0, "written": 0, "failed": 0, "skipped": {}, "writes": {},
                            "from_cargotel": 0, "from_macropoint": 0}
    # The screen shows neither Location/ETA nor its "Location Update" history, only the Last Dispatch Comment column,
    # hence the (MacroPoint) comment; a CargoTel ping already gets its comment.
    mp_comment_texts: dict[str, str] = {}
    if mp_pings is not None:
        state_l = location_state if location_state is not None else LocationState(None)
        located = loads + extra
        # a GPS-link ping within gps_fallback_hours (4) wins; older than that, or none, falls back to MacroPoint
        recent, no_ping = split_by_gps_age(located, now, gps_fallback_hours)
        ct_plans = plan_cargotel_locations(recent, state_l)
        mp_plans, skipped = plan_locations(no_ping, mp_pings, state_l, now, max_age_hours=mp_max_age_hours,
                                           same_place_hours=mp_same_place_hours, match_phone=mp_match_phone)
        plans = ct_plans + mp_plans
        loc = summary["locations"]
        loc["on_macropoint"] = sum(1 for l in no_ping if l.load_id in mp_pings)
        loc["planned"], loc["skipped"] = len(plans), skipped
        loc["from_cargotel"], loc["from_macropoint"] = len(ct_plans), len(mp_plans)
        for load_id, why in skipped.items():
            if "tracks" in why or "future" in why or "no time" in why:
                log(f"MacroPoint location for {load_id} not written: {why}")
        if plans and post_locations and location_writer is not None:
            results = location_writer.apply(plans)
            for plan in plans:
                ok, detail = results.get(plan.load_id, (False, "not attempted"))
                loc["writes"][plan.load_id] = {"place": plan.ping.place, "ping_utc": plan.ping.stamp, "source": plan.source,
                                               "written": ok, "detail": detail}
                if ok:
                    loc["written"] += 1
                    state_l.mark(plan.load_id, plan.ping, detail, now, source=plan.source)
                    if mp_comments and plan.source == "macropoint":
                        mp_comment_texts[plan.load_id] = mp_comment_text(plan.ping, now.tzinfo)
                    origin = "GPS link" if plan.source == "cargotel" else f"MacroPoint {plan.ping.source}"
                    log(f"location written on {plan.load_id} ({plan.status}): {detail} ({origin} ping {plan.ping.stamp}, {plan.reason})")
                else:
                    loc["failed"] += 1
                    if detail.startswith("CargoTel does not know the place"):
                        state_l.mark(plan.load_id, plan.ping, "", now, source=plan.source)  # not retried for this ping
                    log(f"location NOT written on {plan.load_id}: {detail}")
            state_l.prune(now)
            state_l.save()
            if checkpoint is not None:
                try:
                    checkpoint()
                except Exception as exc:  # noqa: BLE001
                    log(f"checkpoint failed: {exc}")
        elif plans:
            path = write_location_dump(outbox, plans, now)
            loc["writes"] = {p.load_id: {"place": p.ping.place, "ping_utc": p.ping.stamp, "source": p.source, "written": False,
                                         "detail": "dry run"} for p in plans}
            if mp_comments and not post_comments:  # a full dry run lists them; never post a comment for an unwritten location
                mp_comment_texts.update({p.load_id: mp_comment_text(p.ping, now.tzinfo) for p in mp_plans})
            log(f"dry run: {len(plans)} location(s) would be written (listed in {path})")
        reasons: dict[str, int] = {}
        for why in skipped.values():
            head = (why.split(",")[0] if why.startswith("still at") else "MacroPoint phone differs from the driver cell" if " tracks " in why
                    else re.sub(r"[\d.]+ h old", "N h old", why))
            reasons[head] = reasons.get(head, 0) + 1
        log(f"locations: {len(located)} loads, {len(ct_plans)} from a GPS-link ping, {len(no_ping)} with no GPS-link ping "
            f"within {gps_fallback_hours:g} h "
            f"({loc['on_macropoint']} of them on MacroPoint, {len(mp_plans)} to write from it); {loc['written']} written, "
            f"{loc['failed']} failed; MacroPoint skipped: "
            + (", ".join(f"{n} {why}" for why, n in sorted(reasons.items(), key=lambda kv: -kv[1])) or "none"))

    # 4. dispatch comments for every load with a ping (still pinging or gone quiet). Last, because it is the
    # riskiest part (a POST to CargoTel; the poster sends only the commented loads' rows since 2026-11-10, a
    # whole-screen form took over ten minutes): a Lambda timeout here costs the comments only, and the next
    # run retries them.
    comment_loads = loads + extra if mp_pings is not None else loads
    gps_comments = plan_comments(comment_loads, comment_state)
    comments = dict(gps_comments)
    for load_id, text in mp_comment_texts.items():
        # one box per load per POST: a load with a new driver-app ping and a MacroPoint ping gets both in one comment
        comments[load_id] = f"{comments[load_id]} | {text}"[:255] if load_id in comments else text
    summary["comments_planned"] = len(comments)
    summary["locations"]["comments"] = len(mp_comment_texts)
    if comments and post_comments and poster is not None:
        try:
            landed = poster.post(page_html, comments)
        except Exception as exc:  # noqa: BLE001 - the run must still finish
            log(f"comment form failed: {exc}")
            summary["comment_error"] = str(exc)
            landed = {}
        for load_id, text in comments.items():
            ok = landed.get(load_id, False)
            summary["comments"][load_id] = {"text": text, "written": ok}
            if ok:
                summary["comments_written"] += 1
                if load_id in gps_comments:
                    ping = next((l.last_ping for l in comment_loads if l.load_id == load_id), None)
                    comment_state.mark(load_id, ping.stamp if ping else "", text, now)
                log(f"comment added on {load_id}: {text}")
            else:
                summary["comments_failed"] += 1
                log(f"comment NOT found on {load_id} after posting: {text}")
        comment_state.prune(now)
        comment_state.save()
    elif comments:
        path = write_comment_dump(outbox, comments, comment_loads)
        summary["comments"] = {load_id: {"text": text, "written": False} for load_id, text in comments.items()}
        log(f"dry run: {len(comments)} comment(s) would be added (written to {path})")
    else:
        log("no comments to add")

    summary["loads"] = [l.to_record(now) for l in loads]
    summary["report_loads"] = [l.to_record(now) for l in extra]
    return summary


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def print_table(loads: list[GpsLoad], now: datetime) -> None:
    print(f"{'load':<8} {'state':<8} {'customer':<22} {'dispatch rep':<18} {'sales rep':<18} {'pickup ETA':<13} {'last ping':<42} driver")
    for load in loads:
        ping = load.last_ping
        last = _fmt_ping(ping, now) if ping else ("GPS unreadable" if load.gps_error else "-")
        print(f"{load.load_id:<8} {load.tracking:<8} {(load.customer or '-')[:22]:<22} "
              f"{(nice_name(load.dispatch_rep) if load.dispatch_rep else '-')[:18]:<18} "
              f"{(nice_name(load.sales_rep) if load.sales_rep else '-')[:18]:<18} "
              f"{_fmt_eta(load.pickup_eta, '-'):<13} {last[:42]:<42} {nice_name(load.driver) if load.driver else '-'}")


def _addresses(text: str | None) -> list[str]:
    return [a.strip() for a in (text or "").split(",") if a.strip()]


def main(argv: list[str] | None = None, checkpoint: Callable[[], None] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--saved-id", default=SEARCH_ID, help=f"CargoTel saved search id (default {SEARCH_ID}, {SEARCH_NAME})")
    parser.add_argument("--days-ahead", type=int, default=DEFAULT_DAYS_AHEAD,
                        help="pickup ETA before today + this many days, the screen's own date (default 2)")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES), help="comma-separated statuses to check (default Dispatched)")
    parser.add_argument("--exclude-customer", default=",".join(DEFAULT_EXCLUDE_CUSTOMERS), metavar="ID|NAME[,...]",
                        help="customers left out of the whole run, by customer id or part of the name (default Merchants Automotive, 81464; "
                             "\"\" for none)")
    parser.add_argument("--stale-hours", type=float, default=DEFAULT_STALE_HOURS,
                        help="a last ping older than this is 'not pinging' (default 4)")
    parser.add_argument("--gps-days", type=int, default=DEFAULT_GPS_DAYS,
                        help="GPS history window asked for, in days (default 2, the window the GPS link itself shows)")
    parser.add_argument("--max-gps", type=int, default=DEFAULT_MAX_GPS, help="GPS pages read per run at most")
    parser.add_argument("--reps", default=os.path.join(PARENT, "reps.csv"),
                        help="reps.csv: name,email[,title][,phone][,manager email] (default ../reps.csv)")
    parser.add_argument("--stellantis-to", default=",".join(DEFAULT_STELLANTIS_TO),
                        help="comma-separated addresses that get every Stellantis alert with the dispatch rep")
    parser.add_argument("--escalate-to", default=",".join(DEFAULT_ESCALATE),
                        help="comma-separated addresses that get an alert when neither rep can be emailed")
    parser.add_argument("--default-manager", default="", help="comma-separated Cc for sales reps with no manager in reps.csv")
    parser.add_argument("--report-to", default=",".join(DEFAULT_REPORT_TO), metavar="ADDR[,ADDR]",
                        help="who gets the GPS report (default Justice Cromwell, the Stellantis mailbox, Brett Booth, Podge Ormsby, Mitchell Meyers "
                             "and Stuart Mitchell; "
                             "\"\" for nobody)")
    parser.add_argument("--report-hours", default="all" if DEFAULT_REPORT_HOURS == "all" else ",".join(str(h) for h in DEFAULT_REPORT_HOURS),
                        metavar="H[,H]|all|never", help="the run(s) that send the report, by hour of day (default 7,11,15,18: the 07:00, 11:00, 15:00 and 18:00 ET runs)")
    parser.add_argument("--report-quiet-hours", type=float, default=DEFAULT_REPORT_QUIET_HOURS,
                        help="the report lists the drivers quiet for longer than this (default 11)")
    parser.add_argument("--report-steady-hours", type=float, default=DEFAULT_REPORT_STEADY_HOURS,
                        help="a quiet load is not an issue when its driver pinged steadily in this many past hours (default 16; 0 = off)")
    parser.add_argument("--report-steady-pings", type=int, default=DEFAULT_REPORT_STEADY_PINGS,
                        help="pings needed in those hours to count as steady (default 5)")
    parser.add_argument("--report-steady-gap-hours", type=float, default=DEFAULT_REPORT_STEADY_GAP_HOURS,
                        help="largest gap between those pings that still counts as steady (default 3)")
    parser.add_argument("--report-status", default=",".join(DEFAULT_REPORT_STATUSES), metavar="STATUS[,...]",
                        help="statuses scanned for the report only, no comments or alerts (default In-Route; \"\" for none)")
    parser.add_argument("--report", action="store_true", help="send the report this run whatever the hour (with --redirect-to for a look first)")
    parser.add_argument("--macropoint-dir", metavar="DIR",
                        help="folder of MacroPoint pings (<load>.json from macropoint_webhook); enables the Location/ETA writes")
    parser.add_argument("--post-locations", action="store_true",
                        help="really write the MacroPoint places into CargoTel's Location/ETA; otherwise they go to --outbox")
    parser.add_argument("--location-state", default=os.path.join(HERE, "location_state.json"),
                        help="which MacroPoint ping was written on each load")
    parser.add_argument("--mp-max-age-hours", type=float, default=MP_MAX_AGE_HOURS,
                        help="write a MacroPoint ping only while it is at most this old (default 48, the GPS link's window)")
    parser.add_argument("--mp-same-place-hours", type=float, default=MP_SAME_PLACE_HOURS,
                        help="re-write an unchanged MacroPoint place only after this long (default 0: every new ping)")
    parser.add_argument("--gps-fallback-hours", type=float, default=DEFAULT_GPS_FALLBACK_HOURS,
                        help="a GPS-link ping older than this counts like no ping: MacroPoint's latest location is used (default 4)")
    parser.add_argument("--no-mp-comments", action="store_true",
                        help="do not also post the MacroPoint place as a dispatch comment labelled (MacroPoint)")
    parser.add_argument("--mp-no-phone-match", action="store_true",
                        help="write even when MacroPoint's tracked phone differs from the CargoTel driver cell")
    parser.add_argument("--no-alerts", action="store_true",
                        help="no emails to the reps about loads not pinging; the dispatch comments and the report carry on")
    parser.add_argument("--from", dest="from_addr", default=os.environ.get("MAIL_FROM", DEFAULT_FROM), help="verified SES sender")
    parser.add_argument("--send", action="store_true",
                        help="really send through SES; otherwise emails go to --outbox and the alert state is untouched")
    parser.add_argument("--post-comments", action="store_true",
                        help="really add the dispatch comments in CargoTel; otherwise they are written to --outbox")
    parser.add_argument("--redirect-to", metavar="ADDR[,ADDR]",
                        help="send every email to these addresses instead, as a marked test copy; nothing is recorded")
    parser.add_argument("--only-rep", metavar="NAME", help='only the email groups with this rep ("Kaleb Hamman"; "stellantis"; "unassigned")')
    parser.add_argument("--only-load", metavar="ID[,ID]", help="limit the whole run (GPS checks, comments, emails) to these load ids, for tests")
    parser.add_argument("--region", help="AWS region for SES (default: boto3's)")
    parser.add_argument("--configuration-set", help="SES configuration set for bounce/complaint events")
    parser.add_argument("--outbox", default=os.path.join(HERE, "outbox"), help="dry-run folder")
    parser.add_argument("--state", default=os.path.join(HERE, "alert_state.json"), help="what was alerted and when")
    parser.add_argument("--comment-state", default=os.path.join(HERE, "comment_state.json"), help="which ping was written on each load")
    parser.add_argument("--sent-log", default=os.path.join(HERE, "sent_log.jsonl"), help="one line per email sent")
    parser.add_argument("--repeat-hours", type=float, default=DEFAULT_REPEAT_HOURS,
                        help="alert the same load again after this long (default 4; 0 = every run)")
    parser.add_argument("--save-html", metavar="DIR", help="archive the fetched grid page and GPS pages here")
    parser.add_argument("--from-html", metavar="FILE", help="parse a saved grid page instead of fetching (offline)")
    parser.add_argument("--gps-dir", metavar="DIR", help="offline GPS pages, gps_<load>.html (with --from-html)")
    parser.add_argument("--now", help="pretend it is this time (ISO, with offset), for tests")
    parser.add_argument("--list", action="store_true", help="print the loads and their GPS state and stop; nothing composed")
    parser.add_argument("--json", metavar="FILE", help="also write the run summary (or, with --list, the loads) here")
    args = parser.parse_args(argv)

    now = datetime.fromisoformat(args.now) if args.now else datetime.now().replace(microsecond=0)
    now = now.astimezone() if now.tzinfo is None else now
    statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
    exclude_customers = tuple(_addresses(args.exclude_customer))
    redirect_to = _addresses(args.redirect_to) or None
    only_loads = set(_addresses(args.only_load)) if args.only_load else None
    record = args.send and not redirect_to
    try:
        report_hours = parse_report_hours(args.report_hours)
    except ValueError as exc:
        parser.error(str(exc))
    report_to = _addresses(args.report_to)
    report_statuses = tuple(_addresses(args.report_status))
    report_when = ("every run" if report_hours == "all"
                   else ("at " + ", ".join(f"{h:02d}:00" for h in report_hours) if report_hours else "never"))

    def log(message: str) -> None:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} tracking-update: {message}", flush=True)

    if args.from_html:
        with open(args.from_html, encoding="utf-8") as handle:
            page_html = handle.read()
        grid = parse_grid(page_html)
        gps_fetch = dir_fetcher(args.gps_dir) if args.gps_dir else None
        poster = None
        location_writer = None
    else:
        session = CargoTelSession(min_interval=0.5)
        page_html, grid = fetch_grid_html(session, now, saved_id=args.saved_id, days_ahead=args.days_ahead, save_html=args.save_html)
        gps_fetch = gps_fetcher(session, now, args.gps_days)
        poster = CommentPoster(session, now, saved_id=args.saved_id, days_ahead=args.days_ahead, log=log)
        location_writer = LocationWriter(session, BASE_URL, log=log)
    save_gps = os.path.join(args.save_html, "gps") if args.save_html else None

    if args.list:
        loads, warnings, counts = select_loads(grid, now, days_ahead=args.days_ahead, statuses=statuses, exclude_customers=exclude_customers)
        for warning in warnings:
            log(warning)
        if only_loads is not None:
            loads = [l for l in loads if l.load_id in only_loads]
        errors = check_gps(loads, gps_fetch, now, stale_hours=args.stale_hours, max_checks=args.max_gps, save_html=save_gps, log=log)
        print_table(loads, now)
        by_state = {kind: sum(1 for l in loads if l.tracking == kind) for kind in (ACTIVE, STALE, NEVER, UNKNOWN)}
        comments = plan_comments(loads, CommentState(args.comment_state))
        log(f"{counts['rows']} loads, {counts['excluded']} of an excluded customer, {len(loads)} with a pickup ETA before "
            f"{eta_cutoff(now, args.days_ahead):%m/%d/%Y}; "
            f"{by_state[ACTIVE]} active, {by_state[STALE]} stale, {by_state[NEVER]} never pinged, {by_state[UNKNOWN]} unreadable; "
            f"{len(comments)} comment(s) would be added; {len(errors)} GPS error(s)")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as handle:
                json.dump({"now": now.isoformat(), "counts": counts, "by_state": by_state, "comments": comments,
                           "loads": [l.to_record(now) for l in loads]}, handle, indent=2)
        return 0

    reps = load_rep_details(args.reps)
    if not reps:
        log(f"warning: no reps read from {args.reps}; every alert will go to the escalation list")
    transport = SesTransport(args.region, args.configuration_set) if args.send else OutboxTransport(args.outbox)
    log(f"{'SENDING via SES' if args.send else 'dry run to ' + args.outbox + '/'} as {args.from_addr}; "
        f"comments {'POSTED to CargoTel' if args.post_comments else 'to the outbox only'}; stale after {args.stale_hours:g} h; "
        f"GPS window {args.gps_days} d; repeat after {args.repeat_hours:g} h; Stellantis -> {args.stellantis_to}; "
        f"escalate -> {args.escalate_to}; excluded customers: {', '.join(exclude_customers) or 'none'}"
        f"; report {report_when} -> {', '.join(report_to) or 'nobody'}" + (" (forced this run)" if args.report else "")
        + (f", also scanning {', '.join(report_statuses)}" if report_statuses else "")
        + ("; ALERTS TO THE REPS OFF (--no-alerts)" if args.no_alerts else "")
        + (f"; MacroPoint locations from {args.macropoint_dir} {'WRITTEN to CargoTel' if args.post_locations else 'to the outbox only'}"
           if args.macropoint_dir else "")
        + (f"; TEST COPIES redirected to {', '.join(redirect_to)}, nothing recorded" if redirect_to else "")
        + (f"; only {args.only_rep}" if args.only_rep else "")
        + (f"; only load(s) {', '.join(sorted(only_loads))}" if only_loads else ""))
    summary = run_once(
        page_html, grid, now=now, reps=reps, gps_fetch=gps_fetch, state=AlertState(args.state),
        comment_state=CommentState(args.comment_state), transport=transport, poster=poster, from_addr=args.from_addr,
        record=record, post_comments=args.post_comments, sent_log=args.sent_log if record else None, outbox=args.outbox,
        days_ahead=args.days_ahead, statuses=statuses, exclude_customers=exclude_customers, stale_hours=args.stale_hours,
        gps_days=args.gps_days, repeat_hours=args.repeat_hours, stellantis_to=_addresses(args.stellantis_to),
        escalate=_addresses(args.escalate_to),
        default_manager=_addresses(args.default_manager), report_to=report_to, report_hours=report_hours,
        report_quiet_hours=args.report_quiet_hours, report_statuses=report_statuses, force_report=args.report,
        report_steady=SteadyRule(args.report_steady_hours, args.report_steady_pings, args.report_steady_gap_hours),
        send_alerts=not args.no_alerts, max_gps=args.max_gps,
        mp_pings=load_pings(args.macropoint_dir) if args.macropoint_dir else None,
        location_state=LocationState(args.location_state), location_writer=location_writer, post_locations=args.post_locations,
        mp_max_age_hours=args.mp_max_age_hours, mp_same_place_hours=args.mp_same_place_hours, mp_match_phone=not args.mp_no_phone_match,
        mp_comments=not args.no_mp_comments, gps_fallback_hours=args.gps_fallback_hours,
        save_gps=save_gps,
        redirect_to=redirect_to, only_rep=args.only_rep, only_loads=only_loads, checkpoint=checkpoint, log=log,
    )
    log(f"done: {summary['selected']} loads checked, {summary['by_state'][STALE] + summary['by_state'][NEVER]} not pinging, "
        f"{summary['comments_planned']} comment(s) planned, {summary['comments_written']} written, {summary['comments_failed']} failed, "
        f"{len(summary['emails'])} email(s), {summary['skipped_repeat']} load(s) already alerted, {len(summary['gps_errors'])} GPS error(s)"
        + (f"; report: {len(summary['report']['never'])} never pinged, {len(summary['report']['quiet'])} quiet" if summary.get("report") else "")
        + (f"; locations: {summary['locations']['from_cargotel']} from the GPS link, {summary['locations']['from_macropoint']} from MacroPoint, "
           f"{summary['locations']['written']} written, {summary['locations']['failed']} failed"
           if args.macropoint_dir else ""))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
