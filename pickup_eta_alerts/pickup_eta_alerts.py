"""Pickup-ETA alerts: email the dispatch rep and the sales rep about the Spot Loads -
Tracking loads that have no pickup ETA or no scheduled pickup date.

What it does, once per run:

1. Pull Load Tracking > Spot Load Tracking > Spot Loads - Tracking (saved search 1823)
   with every row. The search holds the Dispatched and In-Route spot loads (CargoTel
   statuses O and G) whose carrier was added in the last 90 days. Its grid shows the
   dispatch rep, the status and the date it was set, the carrier and driver with phones,
   the "PickUp Scheduled Date" and a "Pickup ETA (Edit)" widget: five ``<select>`` boxes
   (month, day, year, hour, minute) whose SELECTED options are the stored ETA. Nothing
   selected means no ETA, which matches the Pickup ETA field on the load page.
2. Keep the loads that break a rule, once they are ``--min-age-hours`` (1) past their
   status date so a load dispatched minutes ago is not nagged while the rep is still
   setting it up:

   * no pickup ETA;
   * pickup ETA passed: the load is still Dispatched (``--stale-statuses``) more than
     ``--stale-eta-hours`` (2) after its pickup ETA, so the rep must either mark it
     picked up or enter a new ETA. An ETA entered as a date only counts as the whole
     day. ``--skip-stale-check`` turns this off;
   * no scheduled pickup date (``--skip-sched-check`` turns this off).

   Loads whose pickup is today (pickup ETA or scheduled pickup date on the run day) are
   left alone until tomorrow (``--include-same-day`` turns that off).
3. Sales reps: the search is asked for the "Sales Rep 1" / "Sales Rep 2" columns as well
   (``Menu__ViewFields``, which CargoTel honours on a GET), so every row carries them.
   Should those columns ever be missing, the kept loads' pages (``loadmaint.mcgi``) are
   read instead, through a cache. A load with no sales rep gets the rep its customer has
   on its other loads (the current grid first, then ``customer_reps.json`` built up over
   earlier runs), marked as inferred, so it joins that rep's email instead of going out
   on its own (``--no-infer-sales-rep`` turns that off). The dispatch rep comes straight
   from the grid.
4. Send one email per (dispatch rep, sales rep) pair from the vehicles@ group: To both
   reps, Cc Justice Cromwell and Stuart Mitchell (``--cc``) plus the secondary sales rep.
   Pairs that resolve to the same recipients are merged into one email. When neither rep
   can be addressed (no rep on the load, or no address in ``reps.csv``) the Cc list gets
   the email as To, with the rep names in the body.
5. Remember what was sent (``alert_state.json``): a load is repeated to the same people
   only after ``--repeat-hours`` (24), or at once when what is missing or who is
   responsible changed. Dry runs (no ``--send``) write the emails to ``outbox/`` and
   leave the state alone.

Usage::

    python pickup_eta_alerts.py --list                   # what is flagged, nothing composed
    python pickup_eta_alerts.py                          # dry run: emails to outbox/
    python pickup_eta_alerts.py --send                   # really send through SES
    python pickup_eta_alerts.py --from-html pages/tracking.html --reps-json reps_by_load.json   # offline

The CargoTel modules (``cargotel_search``, ``suggest_reloads``, ``send_drafts``) live one
folder up and the email plumbing (transports, rep cache, test-copy redirect) is shared
with ``due_date_alerts`` next door; both paths are added to ``sys.path`` so this runs from
here and from the flat Lambda zip alike.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
for _path in (PARENT, os.path.join(PARENT, "due_date_alerts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from cargotel_search import BASE_URL, CargoTelSession, Grid, GridRow, SearchParams, parse_grid  # noqa: E402
from due_date_alerts import (  # noqa: E402
    DEFAULT_CC,
    DEFAULT_FROM,
    DEFAULT_MAX_LOOKUPS,
    DEFAULT_REP_CACHE_HOURS,
    Email,
    FileTransport,
    RepCache,
    SesTransport,
    _dedupe,
    _first_name,
    _fmt_when,
    append_log,
    page_lookup,
    redirect,
)
from send_drafts import _name_key, load_rep_details  # noqa: E402
from suggest_reloads import DispatchedLoad, nice_name  # noqa: E402

__all__ = [
    "AlertState",
    "CustomerReps",
    "MailGroup",
    "OutboxTransport",
    "PairGroup",
    "TrackedLoad",
    "address",
    "all_tracked",
    "attach_reps",
    "compose",
    "customer_key",
    "group_by_reps",
    "infer_sales_reps",
    "mail_groups",
    "main",
    "parse_pickup_eta",
    "parse_stamp",
    "run_once",
    "select_loads",
    "tracked_load_from_row",
]

SEARCH_ID = "1823"
SEARCH_NAME = "Spot Loads - Tracking"
SEARCH_PATH = "Load Tracking > Spot Load Tracking > Spot Loads - Tracking"
DEFAULT_MIN_AGE_HOURS = 1.0
DEFAULT_REPEAT_HOURS = 24.0
#: The statuses the saved search returns; ``--status`` narrows the run to some of them.
DEFAULT_STATUSES = ("Dispatched", "In-Route")
#: Hours after the pickup ETA (or after the end of its day, for a date-only ETA) before a
#: load that has not moved on is flagged as past its pickup.
DEFAULT_STALE_ETA_HOURS = 2.0
#: The "pickup ETA passed" rule only applies to loads in these statuses. In-Route is left
#: out: CargoTel keeps a load In-Route from the moment the driver sets off until it is
#: Delivered, so a past pickup ETA on an In-Route load is normal.
DEFAULT_STALE_STATUSES = ("Dispatched",)
LOAD_URL = f"{BASE_URL}/backoffice/loadmaint.mcgi?load_id="
#: Group key part for loads with nobody in that role.
UNASSIGNED = ""
#: Columns added to the saved view on the GET. Ask for ONLY the extra ones: CargoTel puts
#: them first and appends the saved view's own columns after them. Asking for the full
#: list instead duplicates every saved column, and the duplicate "Pickup ETA (Edit)" cell
#: comes back empty (the widget is rendered once), which read as "no widget" on every
#: row (seen 2026-10-13).
EXTRA_VIEW_FIELDS = ("sales_rep1", "sales_rep2", "consignor")

REASON_NO_ETA = "no pickup ETA"
REASON_NO_SCHED = "no scheduled pickup date"
REASON_STALE_ETA = "pickup ETA passed"

_STAMP_RE = re.compile(r"^\s*(\d{1,2}/\d{1,2}/\d{4})(?:\s+(\d{1,2}:\d{2}))?")
_ETA_SELECT_RE = re.compile(
    r'<select\s+name="Control__setpickupeta__\d+__(month|day|year|hour|minute)_pickup_eta"[^>]*>(.*?)</select>', re.S | re.I)
_SELECTED_RE = re.compile(r'<option\s+value="([^"]*)"[^>]*?\bselected\b', re.I)


def parse_stamp(text: str | None) -> datetime | None:
    """``"10/10/2026 14:26"`` or ``"11/24/2026 13:26 *Calculated"`` -> datetime; anything
    else -> ``None``."""

    if not text:
        return None
    match = _STAMP_RE.match(text)
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)} {match.group(2) or '00:00'}", "%m/%d/%Y %H:%M")
    except ValueError:
        return None


def parse_pickup_eta(cell_html: str | None) -> tuple[datetime | None, bool]:
    """The stored pickup ETA behind the "Pickup ETA (Edit)" widget: ``(eta, found)``.

    ``found`` is False when the cell holds no widget at all (the column was removed from
    the saved view, or the markup changed), in which case "no ETA" must not be concluded.
    An ETA entered as a date only comes back at 00:00, as CargoTel stores it.
    """

    parts: dict[str, str] = {}
    for part, body in _ETA_SELECT_RE.findall(cell_html or ""):
        chosen = _SELECTED_RE.search(body)
        parts[part] = chosen.group(1).strip() if chosen else ""
    if not all(k in parts for k in ("month", "day", "year")):
        return None, False
    if not (parts["month"] and parts["day"] and parts["year"]):
        return None, True
    try:
        return datetime(int(parts["year"]), int(parts["month"]), int(parts["day"]),
                        int(parts.get("hour") or 0), int(parts.get("minute") or 0)), True
    except ValueError:
        return None, True


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


# --------------------------------------------------------------------------- #
# The loads                                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class TrackedLoad:
    load_id: str
    kind: str  # "load" (bundle parent, several orders) or "order" (single)
    customer: str
    dispatch_rep: str | None  # as the grid shows it ("KYLE LINK")
    status: str
    status_at: datetime | None  # "Dispatched Status Date": when the status was set
    status_text: str
    origin: str
    dest: str
    carrier: str
    carrier_phone: str
    driver: str
    driver_phone: str
    model: str
    vins: list[str]
    po_number: str
    pickup_eta: datetime | None
    eta_known: bool  # False when the grid cell had no ETA widget to read
    sched_pickup: datetime | None
    sched_pickup_text: str
    due_text: str
    priority_comment: str
    reasons: list[str] = field(default_factory=list)
    sales_rep: str | None = None
    sales_rep2: str | None = None
    rep_source: str = ""  # grid | page | cache | error
    sales_rep_inferred: bool = False  # the load had none; taken from the customer's other loads
    inferred_from: str = ""  # "3 other loads of Merchants Automotive" / "earlier loads of ..."

    @property
    def url(self) -> str:
        return LOAD_URL + self.load_id

    @property
    def customer_key(self) -> str:
        return customer_key(self.customer)

    @property
    def units(self) -> int:
        return max(1, len(self.vins))

    @property
    def lane(self) -> str:
        return f"{self.origin or '?'} -> {self.dest or '?'}"

    @property
    def dispatch_key(self) -> str:
        return _name_key(self.dispatch_rep) if self.dispatch_rep else UNASSIGNED

    @property
    def sales_key(self) -> str:
        return _name_key(self.sales_rep) if self.sales_rep else UNASSIGNED

    @property
    def group_key(self) -> tuple[str, str]:
        return self.dispatch_key, self.sales_key

    def age_hours(self, now: datetime) -> float | None:
        if self.status_at is None:
            return None
        return (now - self.status_at).total_seconds() / 3600

    @property
    def eta_is_date_only(self) -> bool:
        return self.pickup_eta is not None and not (self.pickup_eta.hour or self.pickup_eta.minute)

    @property
    def eta_deadline(self) -> datetime | None:
        """When the pickup should have happened: the ETA itself, or the end of its day
        when it was entered as a date only (CargoTel stores that as 00:00)."""

        if self.pickup_eta is None:
            return None
        return self.pickup_eta + timedelta(days=1) if self.eta_is_date_only else self.pickup_eta

    def eta_overdue_hours(self, now: datetime) -> float | None:
        deadline = self.eta_deadline
        if deadline is None:
            return None
        return (now - deadline).total_seconds() / 3600

    def to_record(self, now: datetime) -> dict[str, Any]:
        record = asdict(self)
        for key in ("status_at", "pickup_eta", "sched_pickup"):
            value = getattr(self, key)
            record[key] = value.isoformat(timespec="minutes") if value else None
        age = self.age_hours(now)
        record["age_hours"] = round(age, 1) if age is not None else None
        record["units"] = self.units
        record["url"] = self.url
        return record


def _customer(section: str | None) -> str:
    return re.sub(r"^Customer:\s*", "", (section or "").strip(), flags=re.I)


def customer_key(name: str | None) -> str:
    """``"MERCHANTS AUTOMOTIVE GROUP HOLDINGS LLC"``, ``"Merchants Automotive Group Holdings Llc"``
    and ``"Merchants Automotive Group Holdings LLC (EM)"`` are the same customer."""

    text = re.sub(r"\s*\([^)]*\)\s*$", "", (name or "").strip())
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _plain(text: str) -> str:
    return " ".join((text or "").split())


def tracked_load_from_row(row: GridRow) -> TrackedLoad:
    """One grid row -> a :class:`TrackedLoad` (no rules applied yet). When the grid carries
    the Sales Rep columns the reps are taken from it (``rep_source == "grid"``, also when
    they are empty); otherwise they are left unknown for :func:`attach_reps`."""

    eta_cell = row.cells.get("pickup_eta_only")
    eta, found = parse_pickup_eta(eta_cell.html if eta_cell else "")
    sched_text = row.text("sched_pickup")
    kind = "load" if row.link_class == "LinkBundleParent" else "order"
    reps: dict[str, Any] = {}
    if "sales_rep1" in row.cells:
        reps = {"sales_rep": _plain(row.text("sales_rep1")) or None, "sales_rep2": _plain(row.text("sales_rep2")) or None,
                "rep_source": "grid"}
    return TrackedLoad(
        load_id=row.load_id or "",
        kind=kind,
        customer=_customer(row.section),
        dispatch_rep=_plain(row.text("dispatch_rep")) or None,
        status=_plain(row.text("status")),
        status_at=parse_stamp(row.text("statuspendingpickup")),
        status_text=_plain(row.text("statuspendingpickup")),
        origin=_plain(row.text("originCityState")),
        dest=_plain(row.text("DestinationCityState")),
        carrier=_plain(row.text("carriername")),
        carrier_phone=_plain(row.text("carrierphone")),
        driver=_plain(row.text("driver_name")),
        driver_phone=_plain(row.text("driver_cell_phone")),
        model=_plain(row.text("model")),
        vins=[v for v in row.lines("vins") if v.strip()],
        po_number=_plain(row.text("cons_ref_load")),
        pickup_eta=eta,
        eta_known=found,
        sched_pickup=parse_stamp(sched_text),
        sched_pickup_text=_plain(sched_text),
        due_text=_plain(row.text("duedate")).replace("*Calculated", "(calculated)"),
        priority_comment=_plain(row.text("PriorityComment")),
        **reps,
    )


def all_tracked(grid: Grid) -> list[TrackedLoad]:
    """Every load on the grid (bundle members excluded), rules not applied. Used to learn
    which sales rep a customer's other loads carry."""

    return [tracked_load_from_row(row) for row in grid.rows if row.load_id and row.link_class != "LinkBundleChild"]


def select_loads(
    grid: Grid,
    now: datetime,
    *,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    statuses: tuple[str, ...] | list[str] = DEFAULT_STATUSES,
    check_sched: bool = True,
    check_stale: bool = True,
    stale_eta_hours: float = DEFAULT_STALE_ETA_HOURS,
    stale_statuses: tuple[str, ...] | list[str] = DEFAULT_STALE_STATUSES,
    include_same_day: bool = False,
) -> tuple[list[TrackedLoad], list[str], dict[str, int]]:
    """The loads that break a rule, oldest status first, plus warnings and counts
    (``rows``, ``in_status``, ``too_new``, ``same_day``, ``no_widget``). A load whose
    pickup ETA or scheduled pickup date falls on the run day is a same-day load and is
    left alone (``include_same_day`` keeps it).

    Refuses to run when the grid lacks the column a rule reads: with the "Pickup ETA
    (Edit)" column gone from the saved view, every load would look like it had no ETA.
    """

    keys = set(grid.column_keys)
    if "pickup_eta_only" not in keys:
        raise RuntimeError(f"the {SEARCH_NAME} grid has no 'Pickup ETA (Edit)' column (pickup_eta_only); "
                           "refusing to treat every load as missing an ETA. Check the saved view in CargoTel.")
    if check_sched and "sched_pickup" not in keys:
        raise RuntimeError(f"the {SEARCH_NAME} grid has no 'PickUp Scheduled Date' column (sched_pickup); "
                           "add it back to the saved view or run with --skip-sched-check")

    wanted = {s.strip().lower() for s in statuses if s.strip()}
    stale_wanted = {s.strip().lower() for s in stale_statuses if s.strip()}
    stale_grace = timedelta(hours=stale_eta_hours)
    min_age = timedelta(hours=min_age_hours)
    today = now.date()
    counts = {"rows": 0, "in_status": 0, "too_new": 0, "same_day": 0, "no_widget": 0}
    warnings: list[str] = []
    picked: list[TrackedLoad] = []
    for row in grid.rows:
        if not row.load_id or row.link_class == "LinkBundleChild":
            continue  # bundle members are tracked through their parent load
        counts["rows"] += 1
        load = tracked_load_from_row(row)
        if wanted and load.status.lower() not in wanted:
            continue
        counts["in_status"] += 1
        if not load.eta_known:
            counts["no_widget"] += 1
            warnings.append(f"load {load.load_id}: no Pickup ETA widget in its grid cell; ETA rule skipped")
        if load.eta_known and load.pickup_eta is None:
            load.reasons.append(REASON_NO_ETA)
        if (check_stale and load.pickup_eta is not None and load.status.lower() in stale_wanted
                and now - load.eta_deadline >= stale_grace):
            load.reasons.append(REASON_STALE_ETA)
        if check_sched and load.sched_pickup is None:
            load.reasons.append(REASON_NO_SCHED)
            if load.sched_pickup_text:
                warnings.append(f"load {load.load_id}: unreadable scheduled pickup date {load.sched_pickup_text!r}")
        if not load.reasons:
            continue
        if not include_same_day and today in {d.date() for d in (load.pickup_eta, load.sched_pickup) if d}:
            counts["same_day"] += 1
            continue
        if load.status_at is None:
            warnings.append(f"load {load.load_id}: no status date ({load.status_text!r}); age unknown, included")
        elif now - load.status_at < min_age:
            counts["too_new"] += 1
            continue
        picked.append(load)
    if counts["in_status"] and counts["no_widget"] == counts["in_status"]:
        raise RuntimeError(f"none of the {counts['in_status']} loads has a Pickup ETA widget in its grid cell; the page is not the "
                           "grid this job expects (column list changed?), refusing to run rather than alert nothing")
    picked.sort(key=lambda l: (l.dispatch_key or "~", l.status_at or datetime.max, l.load_id))
    return picked, warnings, counts


# --------------------------------------------------------------------------- #
# Reps                                                                         #
# --------------------------------------------------------------------------- #


def attach_reps(loads: list[TrackedLoad], lookup: Callable[[str], DispatchedLoad] | None, cache: RepCache, now: datetime,
                *, max_lookups: int = DEFAULT_MAX_LOOKUPS, log: Callable[[str], None] = lambda m: None) -> list[str]:
    """Fill ``sales_rep`` / ``sales_rep2`` on every load that did not get them from the
    grid: from the cache when fresh, else from ``lookup(load_id)`` (the load page). The
    grid's dispatch rep is kept; the page's only fills it in when the grid had none.
    Returns the lookup errors; a load whose page could not be read keeps
    ``rep_source == "error"`` and is alerted with no sales rep rather than dropped."""

    errors: list[str] = []
    lookups = 0
    for load in loads:
        if load.rep_source == "grid":
            continue
        entry = cache.get(load.load_id, now)
        source = "cache"
        if entry is None:
            if lookup is None:
                load.rep_source = "error"
                errors.append(f"load {load.load_id}: no rep lookup available")
                continue
            if lookups >= max_lookups:
                load.rep_source = "error"
                errors.append(f"load {load.load_id}: lookup cap of {max_lookups} reached")
                continue
            lookups += 1
            try:
                entry = cache.put(load.load_id, lookup(load.load_id), now)
                source = "page"
            except Exception as exc:  # noqa: BLE001 - one bad page must not sink the run
                load.rep_source = "error"
                errors.append(f"load {load.load_id}: {exc}")
                log(f"rep lookup failed for {load.load_id}: {exc}")
                continue
        load.sales_rep = entry.get("sales_rep") or None
        load.sales_rep2 = entry.get("sales_rep2") or None
        if not load.dispatch_rep and entry.get("dispatch_rep"):
            load.dispatch_rep = entry["dispatch_rep"]
        if not load.customer and entry.get("customer"):
            load.customer = entry["customer"]
        load.rep_source = source
    return errors


class CustomerReps:
    """``customer_reps.json``: which sales reps each customer's loads have carried, learnt
    from every grid the job has seen: ``{customer key: {rep key: {"name", "count", "last"}}}``."""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self.customers: dict[str, dict[str, dict[str, Any]]] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.customers = data.get("customers", {}) if isinstance(data, dict) else {}

    def learn(self, loads: list[TrackedLoad], now: datetime) -> None:
        stamp = now.isoformat(timespec="seconds")
        for load in loads:
            if not load.sales_rep or load.sales_rep_inferred or not load.customer_key:
                continue
            reps = self.customers.setdefault(load.customer_key, {})
            rec = reps.setdefault(_name_key(load.sales_rep), {"name": nice_name(load.sales_rep), "count": 0, "last": stamp})
            rec["count"] = int(rec.get("count", 0)) + 1
            rec["last"] = max(str(rec.get("last", "")), stamp)

    def best(self, customer: str) -> tuple[str, int] | None:
        """``(name, count)`` of the rep most often seen on this customer's loads."""

        reps = self.customers.get(customer_key(customer), {})
        if not reps:
            return None
        key = max(reps, key=lambda k: (int(reps[k].get("count", 0)), str(reps[k].get("last", ""))))
        return str(reps[key].get("name") or key), int(reps[key].get("count", 0))

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"updated": datetime.now().isoformat(timespec="seconds"), "customers": self.customers}, handle, indent=2)
        os.replace(tmp, self.path)


def infer_sales_reps(loads: list[TrackedLoad], everything: list[TrackedLoad], history: CustomerReps | None,
                     *, log: Callable[[str], None] = lambda m: None) -> int:
    """Give every load with no sales rep the rep its customer has on its other loads: the
    other loads on this grid first (most frequent rep, latest status date on a tie), then
    the history. Marks them ``sales_rep_inferred``. Returns how many were filled in."""

    filled = 0
    for load in loads:
        if load.sales_rep or not load.customer_key:
            continue
        others = [o for o in everything if o.load_id != load.load_id and o.customer_key == load.customer_key and o.sales_rep]
        chosen = None
        if others:
            tally: dict[str, list[Any]] = {}
            for other in others:
                rec = tally.setdefault(_name_key(other.sales_rep), [0, datetime.min, other.sales_rep])
                rec[0] += 1
                rec[1] = max(rec[1], other.status_at or datetime.min)
            count, _latest, name = max(tally.values(), key=lambda r: (r[0], r[1]))
            chosen = (name, f"{count} other load{'s' if count != 1 else ''} of {load.customer} on the tracking page")
        elif history is not None:
            found = history.best(load.customer)
            if found:
                chosen = (found[0], f"{found[1]} earlier load{'s' if found[1] != 1 else ''} of {load.customer}")
        if chosen is None:
            continue
        load.sales_rep, load.inferred_from = chosen
        load.sales_rep_inferred = True
        filled += 1
        log(f"load {load.load_id}: no sales rep; {nice_name(load.sales_rep)} taken from {load.inferred_from}")
    return filled


@dataclass
class PairGroup:
    key: tuple[str, str]  # (dispatch key, sales key); "" for nobody
    dispatch_rep: str | None  # "Kyle Link"
    sales_rep: str | None
    loads: list[TrackedLoad] = field(default_factory=list)

    @property
    def unassigned(self) -> bool:
        return self.key == (UNASSIGNED, UNASSIGNED)


def group_by_reps(loads: list[TrackedLoad]) -> list[PairGroup]:
    """One group per (dispatch rep, sales rep) pair, by dispatch rep then sales rep,
    the groups with nobody in a role after the others."""

    groups: dict[tuple[str, str], PairGroup] = {}
    for load in loads:
        group = groups.get(load.group_key)
        if group is None:
            group = groups[load.group_key] = PairGroup(
                key=load.group_key,
                dispatch_rep=nice_name(load.dispatch_rep) if load.dispatch_rep else None,
                sales_rep=nice_name(load.sales_rep) if load.sales_rep else None,
            )
        group.loads.append(load)
    return sorted(groups.values(), key=lambda g: (g.dispatch_rep or "~", g.sales_rep or "~"))


# --------------------------------------------------------------------------- #
# The email                                                                    #
# --------------------------------------------------------------------------- #


def address(group: PairGroup, reps: dict[str, dict[str, str]], cc: list[str]) -> tuple[list[str], list[str], bool, list[str]]:
    """``(to, cc, routed, notes)``: the dispatch rep and the sales rep get To, the managers
    (and the secondary sales rep) Cc. When nobody can be addressed the managers get To
    (``routed``). ``notes`` says who could not be addressed and why."""

    to: list[str] = []
    notes: list[str] = []
    ids = [l.load_id for l in group.loads]
    which = "the load" if not ids else (f"load {ids[0]}" if len(ids) == 1 else f"loads {', '.join(ids)}")
    for role, name in (("dispatch rep", group.dispatch_rep), ("sales rep", group.sales_rep)):
        if not name:
            notes.append(f"no {role} on {which}")
            continue
        email = reps.get(_name_key(name), {}).get("email", "")
        if email:
            to.append(email)
        else:
            notes.append(f"{role} {name} has no email in reps.csv")
    to = _dedupe(to)
    if not to:
        return _dedupe(list(cc)), [], True, notes
    extra = [reps.get(_name_key(l.sales_rep2), {}).get("email", "") for l in group.loads if l.sales_rep2]
    return to, _dedupe(list(cc) + extra, exclude=to), False, notes


@dataclass
class MailGroup:
    """One email: every pair group that resolves to the same To and Cc lists, merged, so
    a rep gets one email per run (a load with no sales rep would otherwise give its
    dispatch rep a second email with the same recipients)."""

    to: list[str]
    cc: list[str]
    routed: bool
    notes: list[str] = field(default_factory=list)
    dispatch_reps: list[str] = field(default_factory=list)  # nice names, first seen first
    sales_reps: list[str] = field(default_factory=list)
    greet_names: list[str] = field(default_factory=list)  # first names of the reps in To
    loads: list[TrackedLoad] = field(default_factory=list)
    pair_keys: set[tuple[str, str]] = field(default_factory=set)

    @property
    def unassigned(self) -> bool:
        return not self.dispatch_reps and not self.sales_reps

    @property
    def key(self) -> str:
        return f"{'+'.join(self.dispatch_reps) or 'nobody'}|{'+'.join(self.sales_reps) or 'nobody'}"

    @property
    def label(self) -> str:
        return f"{', '.join(self.dispatch_reps) or 'no dispatch rep'} / {', '.join(self.sales_reps) or 'no sales rep'}"

    @property
    def who(self) -> str:
        return ", ".join(self.dispatch_reps) or ", ".join(self.sales_reps)

    def matches(self, wanted: str) -> bool:
        if wanted == "unassigned":
            return self.unassigned
        return any(wanted in pair for pair in self.pair_keys)


def mail_groups(loads: list[TrackedLoad], reps: dict[str, dict[str, str]], cc: list[str]) -> list[MailGroup]:
    """Address every (dispatch rep, sales rep) group and merge the ones with the same
    recipients, in first-seen order (dispatch rep, then sales rep; nobody last)."""

    out: list[MailGroup] = []
    by_recipients: dict[tuple[bool, frozenset[str], frozenset[str]], MailGroup] = {}
    for pair in group_by_reps(loads):
        to, cc_list, routed, notes = address(pair, reps, cc)
        key = (routed, frozenset(a.lower() for a in to), frozenset(a.lower() for a in cc_list))
        group = by_recipients.get(key)
        if group is None:
            group = by_recipients[key] = MailGroup(to=list(to), cc=list(cc_list), routed=routed)
            out.append(group)
        for names, name in ((group.dispatch_reps, pair.dispatch_rep), (group.sales_reps, pair.sales_rep)):
            if name and name not in names:
                names.append(name)
        for name in (pair.dispatch_rep, pair.sales_rep):
            first = _first_name(name)
            if name and reps.get(_name_key(name), {}).get("email") and first not in group.greet_names:
                group.greet_names.append(first)
        for note in notes:
            if note not in group.notes:
                group.notes.append(note)
        group.loads.extend(pair.loads)
        group.pair_keys.add(pair.key)
    for group in out:
        inferred = [l for l in group.loads if l.sales_rep_inferred]
        if inferred:
            by_rep: dict[str, list[str]] = {}
            for load in inferred:
                by_rep.setdefault(nice_name(load.sales_rep or ""), []).append(load.load_id)
            group.notes.append("; ".join(
                f"{name} is not set as sales rep on load{'s' if len(ids) != 1 else ''} {', '.join(ids)} but is on the customer's other loads, "
                "so this comes to them too (please set it on the load)" for name, ids in by_rep.items()))
    return out


#: The email's sections, in order. A load goes under the first section whose reason it
#: carries; its action line still lists everything it needs.
SECTIONS = (
    (REASON_NO_ETA, "No pickup ETA", "with no pickup ETA",
     "Get the pickup ETA from the carrier and enter it on the load."),
    (REASON_STALE_ETA, "Pickup ETA passed, status not updated", "past the pickup ETA",
     "The pickup ETA has come and gone and the load is still in the same status: "
     "mark it picked up (update the status) or enter a new pickup ETA."),
    (REASON_NO_SCHED, "No scheduled pickup date", "not scheduled",
     "Enter the scheduled pickup date on the load."),
)


def _subject_counts(loads: list[TrackedLoad]) -> str:
    """``"10 loads with no pickup ETA"`` or ``"44 loads (10 with no pickup ETA, 34 past the pickup ETA)"``."""

    n = len(loads)
    head = f"{n} load{'s' if n != 1 else ''}"
    parts = [(sum(1 for l in loads if reason in l.reasons), phrase) for reason, _title, phrase, _hint in SECTIONS]
    parts = [(count, phrase) for count, phrase in parts if count]
    if len(parts) == 1 and parts[0][0] == n:
        return f"{head} {parts[0][1]}"
    return f"{head} ({', '.join(f'{count} {phrase}' for count, phrase in parts)})"


def _sectioned(loads: list[TrackedLoad]) -> list[tuple[str, str, list[TrackedLoad]]]:
    """``[(title, hint, loads), ...]`` for the sections that have loads."""

    out = []
    placed: set[str] = set()
    for reason, title, _phrase, hint in SECTIONS:
        members = [l for l in loads if reason in l.reasons and l.load_id not in placed]
        if members:
            placed.update(l.load_id for l in members)
            out.append((title, hint, members))
    return out


def _fmt_stamp(when: datetime | None, fallback: str = "none") -> str:
    if when is None:
        return fallback
    return when.strftime("%a %m/%d %H:%M") if (when.hour or when.minute) else when.strftime("%a %m/%d")


def _status_text(load: TrackedLoad, now: datetime) -> str:
    age = load.age_hours(now)
    since = f" since {_fmt_stamp(load.status_at)} ({ago_text(age)})" if age is not None else ""
    return f"{load.status or 'status ?'}{since}"


def _action(load: TrackedLoad, now: datetime) -> str:
    """What the rep has to do with this load, from its reasons."""

    parts = []
    if REASON_NO_ETA in load.reasons:
        parts.append("enter the pickup ETA")
    if REASON_STALE_ETA in load.reasons:
        overdue = load.eta_overdue_hours(now) or 0.0
        when = "the end of " + _fmt_stamp(load.pickup_eta) if load.eta_is_date_only else _fmt_stamp(load.pickup_eta)
        parts.append(f"pickup ETA passed ({when}, {ago_text(overdue)}): mark picked up or set a new ETA")
    if REASON_NO_SCHED in load.reasons:
        parts.append("enter the scheduled pickup date")
    return "; ".join(parts)


def _text_block(load: TrackedLoad, now: datetime, *, show_reps: bool) -> str:
    head = (f"{'Load' if load.kind == 'load' else 'Order'} {load.load_id}  |  {load.customer or 'customer ?'}  |  "
            f"{_status_text(load, now)}  |  ACTION: {_action(load, now)}")
    lines = [head, f"  {load.lane}"]
    carrier = load.carrier or "carrier ?"
    if load.carrier_phone:
        carrier += f" {load.carrier_phone}"
    if load.driver:
        carrier += f"  |  driver {nice_name(load.driver)}" + (f" {load.driver_phone}" if load.driver_phone else "")
    lines.append("  " + carrier)
    lines.append(f"  scheduled pickup {_fmt_stamp(load.sched_pickup)}  |  pickup ETA {_fmt_stamp(load.pickup_eta)}"
                 + (" (passed)" if REASON_STALE_ETA in load.reasons else "")
                 + (f"  |  due {load.due_text}" if load.due_text else ""))
    detail = [f"{load.units} unit{'s' if load.units != 1 else ''}" + (f": {load.model}" if load.model else "")]
    if load.po_number:
        detail.append(f"PO {load.po_number}")
    if load.priority_comment:
        detail.append(f"note: {load.priority_comment}")
    lines.append("  " + "  |  ".join(detail))
    if show_reps:
        lines.append(f"  dispatch rep: {nice_name(load.dispatch_rep) if load.dispatch_rep else 'none'}; "
                     f"sales rep: {nice_name(load.sales_rep) if load.sales_rep else 'none'}"
                     + (" (inferred)" if load.sales_rep_inferred else ""))
    if load.sales_rep_inferred and not show_reps:
        lines.append(f"  sales rep not set on the load; {nice_name(load.sales_rep or '')} taken from {load.inferred_from}")
    lines.append("  " + load.url)
    return "\n".join(lines)


def _html_rows(loads: list[TrackedLoad], now: datetime, *, show_reps: bool) -> str:
    esc = html_lib.escape
    out = []
    for load in loads:
        carrier = esc(load.carrier or "?") + (f"<br><small>{esc(load.carrier_phone)}</small>" if load.carrier_phone else "")
        if load.driver:
            carrier += f"<br><small>driver {esc(nice_name(load.driver))}" + (f" {esc(load.driver_phone)}" if load.driver_phone else "") + "</small>"
        what = f"{load.units} x {esc(load.model)}" if load.model else str(load.units)
        if load.po_number:
            what += f"<br><small>PO {esc(load.po_number)}</small>"
        if load.pickup_eta is None:
            eta_cell = '<b style="color:#b00020">none</b>'
        elif REASON_STALE_ETA in load.reasons:
            eta_cell = f'<b style="color:#b00020">{esc(_fmt_stamp(load.pickup_eta))}</b><br><small>passed {esc(ago_text(load.eta_overdue_hours(now) or 0.0))}</small>'
        else:
            eta_cell = esc(_fmt_stamp(load.pickup_eta))
        cells = [
            f'<a href="{esc(load.url)}">{esc(load.load_id)}</a>' + ("" if load.kind == "load" else "<br><small>order</small>"),
            esc(load.customer or "") + ("<br><small>sales rep not set on the load</small>" if load.sales_rep_inferred else ""),
            esc(load.status or "?") + (f"<br><small>{esc(_fmt_stamp(load.status_at))}, {esc(ago_text(load.age_hours(now)))}</small>"
                                       if load.status_at else ""),
            f"{esc(load.origin)}<br>&rarr; {esc(load.dest)}",
            carrier,
            esc(_fmt_stamp(load.sched_pickup)) if load.sched_pickup else '<b style="color:#b00020">none</b>',
            eta_cell,
            esc(load.due_text),
            what,
            "<b>" + esc(_action(load, now)) + "</b>" + (f"<br><small>{esc(load.priority_comment)}</small>" if load.priority_comment else ""),
        ]
        if show_reps:
            cells.append(esc(nice_name(load.dispatch_rep) if load.dispatch_rep else "none") + "<br><small>sales: "
                         + esc(nice_name(load.sales_rep) if load.sales_rep else "none")
                         + (" (inferred)" if load.sales_rep_inferred else "") + "</small>")
        out.append("<tr>" + "".join(
            f'<td style="padding:4px 8px;border-bottom:1px solid #ddd;vertical-align:top">{c}</td>' for c in cells) + "</tr>")
    return "\n".join(out)


FOOTNOTE = ("The pickup ETA is the \"Pickup ETA (Edit)\" box on the tracking page (the Pickup ETA field on the load); "
            "an ETA entered as a date only counts as the whole day. The scheduled pickup is the load's PickUp Scheduled Date.")
TRAILER = (f"This alert is generated from {SEARCH_NAME} ({SEARCH_PATH}) and repeats once a day while a load still needs "
           "a pickup update. It stops as soon as the ETA is entered or the status moves on. Reply to this email if a load is mis-assigned.")


def compose(group: MailGroup, *, now: datetime) -> Email:
    """Write the alert for one addressed group, as plain text and HTML: one section per
    rule, each with its instruction line."""

    loads = sorted(group.loads, key=lambda l: (l.status_at or datetime.max, l.load_id))
    if group.unassigned:
        who_text = "no rep assigned"
    elif group.routed:
        who_text = f"{group.who} (no email on file)"
    else:
        who_text = group.who
    subject = f"Pickup ETA alert: {_subject_counts(loads)} - {who_text}"

    names = group.greet_names
    greeting = f"Hi {' and '.join(names) if len(names) <= 2 else ', '.join(names[:-1]) + ' and ' + names[-1]}," if (names and not group.routed) else "Hi all,"
    intro = f"As of {_fmt_when(now)}, these loads on {SEARCH_NAME} ({SEARCH_PATH}) need a pickup update from you."
    if group.routed:
        intro += (" Nobody could be emailed directly (" + "; ".join(group.notes) + "), so this alert comes to you instead; "
                  "the reps are named per load below.")
    elif group.notes:
        intro += " Note: " + "; ".join(group.notes) + "."

    show_reps = group.routed or len(group.dispatch_reps) + len(group.sales_reps) > 2
    sections = _sectioned(loads)
    text_parts = [greeting, "", intro, ""]
    for title, hint, members in sections:
        text_parts += [f"{title.upper()} ({len(members)})", hint, ""]
        text_parts += [_text_block(l, now, show_reps=show_reps) + "\n" for l in members]
    text_parts += [FOOTNOTE, TRAILER]
    text = "\n".join(text_parts)

    esc = html_lib.escape
    headers = ["Load", "Customer", "Status", "Lane", "Carrier", "Scheduled pickup", "Pickup ETA", "Due", "Units", "Action"] + (
        ["Dispatch / sales rep"] if show_reps else [])
    th = "".join(f'<th style="text-align:left;padding:4px 8px;border-bottom:2px solid #999">{h}</th>' for h in headers)
    blocks = []
    for title, hint, members in sections:
        blocks.append(f'<h3 style="margin:16px 0 2px">{esc(title)} ({len(members)})</h3>'
                      f'<p style="margin:0 0 6px;color:#444">{esc(hint)}</p>'
                      f'<table style="border-collapse:collapse;font-size:13px"><tr>{th}</tr>{_html_rows(members, now, show_reps=show_reps)}</table>')
    html = ('<html><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">'
            f"<p>{esc(greeting)}</p><p>{esc(intro)}</p>" + "".join(blocks)
            + f'<p style="font-size:12px;color:#666;margin-top:16px">{esc(FOOTNOTE)}<br>{esc(TRAILER)}</p>'
            "</body></html>")
    return Email(key=group.key, rep=group.who or None, to=list(group.to), cc=list(group.cc), subject=subject, text=text, html=html,
                 load_ids=[l.load_id for l in loads], routed=group.routed)


# --------------------------------------------------------------------------- #
# State                                                                        #
# --------------------------------------------------------------------------- #


class OutboxTransport(FileTransport):
    """Dry run: ``outbox/<stamp>_<dispatch rep>_<sales rep>.txt``. The due-date job names
    its files after the rep alone, which collides here when one dispatch rep has groups
    with several sales reps in the same second."""

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
    """``alert_state.json``: ``{"sent": {load_id: {"reasons", "group", "at"}}}``. A load is
    alerted again after ``repeat_hours``, or at once when what is missing or who is
    responsible changed."""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self.sent: dict[str, dict[str, str]] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.sent = data.get("sent", {}) if isinstance(data, dict) else {}

    @staticmethod
    def _signature(load: TrackedLoad) -> tuple[str, str]:
        return ", ".join(load.reasons), "|".join(load.group_key)

    def should_alert(self, load: TrackedLoad, now: datetime, repeat_hours: float) -> bool:
        rec = self.sent.get(load.load_id)
        if not rec:
            return True
        reasons, group = self._signature(load)
        if rec.get("reasons") != reasons or rec.get("group") != group:
            return True
        try:
            at = datetime.fromisoformat(rec["at"])
        except (KeyError, ValueError):
            return True
        return now - at >= timedelta(hours=repeat_hours)

    def mark(self, load: TrackedLoad, now: datetime) -> None:
        reasons, group = self._signature(load)
        self.sent[load.load_id] = {"reasons": reasons, "group": group, "at": now.isoformat(timespec="seconds")}

    def prune(self, now: datetime, keep_days: int = 60) -> None:
        cutoff = now - timedelta(days=keep_days)
        for load_id in list(self.sent):
            try:
                if datetime.fromisoformat(self.sent[load_id]["at"]) < cutoff:
                    del self.sent[load_id]
            except (KeyError, ValueError):
                del self.sent[load_id]

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"updated": datetime.now().isoformat(timespec="seconds"), "sent": self.sent}, handle, indent=2)
        os.replace(tmp, self.path)


# --------------------------------------------------------------------------- #
# One run                                                                      #
# --------------------------------------------------------------------------- #


def fetch_tracking_grid(session: CargoTelSession, saved_id: str = SEARCH_ID, save_html: str | None = None) -> Grid:
    """The search with every row and the Sales Rep columns added (``Menu__ViewFields``
    repeated once per extra column; CargoTel honoured it on a GET on 2026-10-13)."""

    spec = SearchParams(saved_id, per_page="All", extra={"Menu__ViewFields": list(EXTRA_VIEW_FIELDS)})  # type: ignore[dict-item]
    status, html, _params = session.fetch_search_html(spec)
    if save_html:
        os.makedirs(save_html, exist_ok=True)
        path = os.path.join(save_html, f"tracking_{datetime.now():%Y%m%d-%H%M}.html")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(html)
    if status != 200:
        raise RuntimeError(f"CargoTel Search.mcgi returned HTTP {status} for saved search {saved_id}")
    grid = parse_grid(html, status)
    if grid.signin_page:
        raise RuntimeError("CargoTel answered with the sign-in page twice; the S3 session cookie is stale")
    return grid


def run_once(
    grid: Grid,
    *,
    now: datetime,
    reps: dict[str, dict[str, str]],
    cc: list[str],
    lookup: Callable[[str], DispatchedLoad] | None,
    rep_cache: RepCache,
    state: AlertState,
    transport: Any,
    from_addr: str,
    record: bool,
    sent_log: str | None,
    repeat_hours: float = DEFAULT_REPEAT_HOURS,
    min_age_hours: float = DEFAULT_MIN_AGE_HOURS,
    statuses: tuple[str, ...] | list[str] = DEFAULT_STATUSES,
    check_sched: bool = True,
    check_stale: bool = True,
    stale_eta_hours: float = DEFAULT_STALE_ETA_HOURS,
    stale_statuses: tuple[str, ...] | list[str] = DEFAULT_STALE_STATUSES,
    include_same_day: bool = False,
    infer: bool = True,
    customer_reps: CustomerReps | None = None,
    max_lookups: int = DEFAULT_MAX_LOOKUPS,
    redirect_to: list[str] | None = None,
    only_rep: str | None = None,
    log: Callable[[str], None] = lambda m: None,
) -> dict[str, Any]:
    """Select, look up, infer, group, compose, send. ``record=False`` (dry run) leaves the
    alert state untouched; the rep cache and the customer history are always kept, they
    only save page fetches and improve the inference. ``redirect_to`` sends every email to
    those addresses instead, as a marked test copy that is never recorded; ``only_rep``
    limits the run to the groups with that rep in either role ("unassigned" for the loads
    with nobody)."""

    flagged, warnings, counts = select_loads(grid, now, min_age_hours=min_age_hours, statuses=statuses, check_sched=check_sched,
                                             check_stale=check_stale, stale_eta_hours=stale_eta_hours, stale_statuses=stale_statuses,
                                             include_same_day=include_same_day)
    for warning in warnings:
        log(warning)
    by_reason = {reason: sum(1 for l in flagged if reason in l.reasons) for reason in (REASON_NO_ETA, REASON_STALE_ETA, REASON_NO_SCHED)}
    log(f"{SEARCH_NAME}: {counts['rows']} loads, {counts['in_status']} in status {'/'.join(statuses)}, {len(flagged)} flagged "
        f"({', '.join(f'{n} {r}' for r, n in by_reason.items() if n)}), {counts['same_day']} same-day, {counts['too_new']} too new to alert")
    errors = attach_reps(flagged, lookup, rep_cache, now, max_lookups=max_lookups, log=log)
    rep_cache.prune(now)
    rep_cache.save()
    everything = all_tracked(grid)
    if customer_reps is not None:
        customer_reps.learn(everything, now)
    inferred = infer_sales_reps(flagged, everything, customer_reps, log=log) if infer else 0
    if customer_reps is not None:
        customer_reps.save()

    summary: dict[str, Any] = {"rows": counts["rows"], "in_status": counts["in_status"], "flagged": len(flagged),
                               "by_reason": by_reason, "too_new": counts["too_new"], "same_day": counts["same_day"],
                               "reps_inferred": inferred, "warnings": warnings, "lookup_errors": errors, "emails": [], "skipped_repeat": 0}
    if redirect_to:
        record = False
    wanted = None
    if only_rep:
        wanted = "unassigned" if only_rep.strip().lower() == "unassigned" else _name_key(only_rep)
    for group in mail_groups(flagged, reps, cc):
        if wanted is not None and not group.matches(wanted):
            continue
        fresh = [l for l in group.loads if state.should_alert(l, now, repeat_hours)]
        summary["skipped_repeat"] += len(group.loads) - len(fresh)
        label = group.label
        if not fresh:
            log(f"{label}: {len(group.loads)} load(s), all alerted within the last {int(repeat_hours)} h; nothing sent")
            continue
        email = compose(replace(group, loads=fresh), now=now)
        if redirect_to:
            email = redirect(email, redirect_to)
        if not email.to:
            log(f"{label}: nobody to send to (empty --cc?); held")
            continue
        try:
            message_id = transport.send(email, from_addr)
        except Exception as exc:  # noqa: BLE001 - keep going with the other groups
            log(f"send failed for {label}: {exc}")
            summary.setdefault("send_errors", []).append(f"{label}: {exc}")
            continue
        verb = "sent" if record else ("test copy sent" if redirect_to else "dry run")
        log(f"{verb}: {email.subject} -> to {', '.join(email.to)}"
            + (f" cc {', '.join(email.cc)}" if email.cc else "") + f" [{message_id}]")
        entry = {"at": now.isoformat(timespec="seconds"), "dispatch_reps": group.dispatch_reps, "sales_reps": group.sales_reps,
                 "group": group.key, "to": email.to, "cc": email.cc, "subject": email.subject,
                 "loads": email.load_ids, "message_id": message_id, "sent": record,
                 "redirected_to": list(redirect_to) if redirect_to else None}
        summary["emails"].append(entry)
        if record:
            for load in fresh:
                state.mark(load, now)
            append_log(sent_log, entry)
    if record:
        state.prune(now)
        state.save()
    return summary


def load_reps_by_load(path: str | None) -> Callable[[str], DispatchedLoad] | None:
    """Offline rep lookup from a JSON file ``{load_id: {"sales_rep": "LINK, KYLE", ...}}``."""

    if not path:
        return None
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)

    def lookup(load_id: str) -> DispatchedLoad:
        rec = data.get(load_id)
        if rec is None:
            raise KeyError(f"no entry for load {load_id} in {path}")
        return DispatchedLoad(load_id=load_id, customer=rec.get("customer"), sales_rep=rec.get("sales_rep"),
                              sales_rep2=rec.get("sales_rep2"), dispatch_rep=rec.get("dispatch_rep"))

    return lookup


def print_table(loads: list[TrackedLoad], now: datetime) -> None:
    print(f"{'load':<8} {'status':<11} {'since':<30} {'dispatch rep':<18} {'sales rep':<20} {'sched pickup':<16} {'pickup ETA':<16} {'reasons':<40} carrier")
    for load in loads:
        age = load.age_hours(now)
        since = f"{_fmt_stamp(load.status_at, '?')} ({ago_text(age)})" if age is not None else _fmt_stamp(load.status_at, "?")
        sales = (nice_name(load.sales_rep) if load.sales_rep else "-") + ("*" if load.sales_rep_inferred else "")
        print(f"{load.load_id:<8} {load.status[:11]:<11} {since:<30} "
              f"{(nice_name(load.dispatch_rep) if load.dispatch_rep else '-')[:18]:<18} "
              f"{sales[:20]:<20} "
              f"{_fmt_stamp(load.sched_pickup, '-'):<16} {_fmt_stamp(load.pickup_eta, '-'):<16} {', '.join(load.reasons)[:40]:<40} {load.carrier}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--saved-id", default=SEARCH_ID, help=f"CargoTel saved search id (default {SEARCH_ID}, {SEARCH_NAME})")
    parser.add_argument("--status", default=",".join(DEFAULT_STATUSES), help="comma-separated statuses to check (default Dispatched,In-Route)")
    parser.add_argument("--min-age-hours", type=float, default=DEFAULT_MIN_AGE_HOURS,
                        help="leave a load alone until this long after its status date (default 1)")
    parser.add_argument("--skip-sched-check", action="store_true", help="do not flag loads with no scheduled pickup date")
    parser.add_argument("--skip-stale-check", action="store_true", help="do not flag loads whose pickup ETA has passed")
    parser.add_argument("--stale-eta-hours", type=float, default=DEFAULT_STALE_ETA_HOURS,
                        help="hours after the pickup ETA (or the end of its day, for a date-only ETA) before a load still in "
                             "--stale-statuses is flagged (default 2)")
    parser.add_argument("--stale-statuses", default=",".join(DEFAULT_STALE_STATUSES),
                        help="comma-separated statuses the passed-ETA rule applies to (default Dispatched)")
    parser.add_argument("--include-same-day", action="store_true", help="also alert loads whose pickup ETA or scheduled pickup is today")
    parser.add_argument("--no-infer-sales-rep", action="store_true",
                        help="do not fill a missing sales rep from the customer's other loads")
    parser.add_argument("--customer-reps", default=os.path.join(HERE, "customer_reps.json"), help="sales reps seen per customer, learnt each run")
    parser.add_argument("--reps", default=os.path.join(PARENT, "reps.csv"), help="reps.csv: name,email[,title][,phone] (default ../reps.csv)")
    parser.add_argument("--cc", default=",".join(DEFAULT_CC), help="comma-separated addresses copied on every alert")
    parser.add_argument("--from", dest="from_addr", default=os.environ.get("MAIL_FROM", DEFAULT_FROM), help="verified SES sender")
    parser.add_argument("--send", action="store_true", help="really send through SES; otherwise write to --outbox and keep the state untouched")
    parser.add_argument("--redirect-to", metavar="ADDR[,ADDR]", help="send every email to these addresses instead, as a marked test copy; nothing is recorded")
    parser.add_argument("--only-rep", metavar="NAME", help="only the groups with this rep in either role (\"Kyle Link\"; \"unassigned\" for loads with nobody)")
    parser.add_argument("--region", help="AWS region for SES (default: boto3's)")
    parser.add_argument("--configuration-set", help="SES configuration set for bounce/complaint events")
    parser.add_argument("--outbox", default=os.path.join(HERE, "outbox"), help="dry-run folder")
    parser.add_argument("--state", default=os.path.join(HERE, "alert_state.json"), help="what was alerted and when")
    parser.add_argument("--sent-log", default=os.path.join(HERE, "sent_log.jsonl"), help="one line per email sent")
    parser.add_argument("--rep-cache", default=os.path.join(HERE, "rep_cache.json"), help="sales reps read from load pages")
    parser.add_argument("--rep-cache-hours", type=float, default=DEFAULT_REP_CACHE_HOURS, help="re-read a load's reps after this long (default 12)")
    parser.add_argument("--repeat-hours", type=float, default=DEFAULT_REPEAT_HOURS, help="alert the same load again after this long (default 24; 0 = every run)")
    parser.add_argument("--max-lookups", type=int, default=DEFAULT_MAX_LOOKUPS, help="load pages fetched per run at most")
    parser.add_argument("--save-html", metavar="DIR", help="archive the fetched tracking page here")
    parser.add_argument("--from-html", metavar="FILE", help="parse a saved page instead of fetching (offline)")
    parser.add_argument("--reps-json", metavar="FILE", help="offline rep lookup {load_id: {sales_rep, ...}} (with --from-html)")
    parser.add_argument("--now", help="pretend it is this time (ISO), for tests")
    parser.add_argument("--list", action="store_true", help="print the flagged loads and stop; nothing composed")
    parser.add_argument("--json", metavar="FILE", help="also write the run summary (or, with --list, the flagged loads) here")
    args = parser.parse_args(argv)

    now = datetime.fromisoformat(args.now) if args.now else datetime.now().replace(microsecond=0)
    cc = [a.strip() for a in args.cc.split(",") if a.strip()]
    statuses = tuple(s.strip() for s in args.status.split(",") if s.strip())
    redirect_to = [a.strip() for a in (args.redirect_to or "").split(",") if a.strip()] or None
    record = args.send and not redirect_to
    rules = dict(min_age_hours=args.min_age_hours, statuses=statuses, check_sched=not args.skip_sched_check,
                 check_stale=not args.skip_stale_check, stale_eta_hours=args.stale_eta_hours,
                 stale_statuses=tuple(s.strip() for s in args.stale_statuses.split(",") if s.strip()),
                 include_same_day=args.include_same_day)

    def log(message: str) -> None:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} pickup-eta: {message}", flush=True)

    if args.from_html:
        with open(args.from_html, encoding="utf-8") as handle:
            grid = parse_grid(handle.read())
        lookup = load_reps_by_load(args.reps_json)
    else:
        session = CargoTelSession()
        grid = fetch_tracking_grid(session, args.saved_id, args.save_html)
        lookup = page_lookup(session)

    rep_cache = RepCache(args.rep_cache, args.rep_cache_hours)
    customer_reps = CustomerReps(args.customer_reps)
    if args.list:
        flagged, warnings, counts = select_loads(grid, now, **rules)
        for warning in warnings:
            log(warning)
        errors = attach_reps(flagged, lookup, rep_cache, now, max_lookups=args.max_lookups, log=log)
        rep_cache.save()
        if not args.no_infer_sales_rep:
            infer_sales_reps(flagged, all_tracked(grid), customer_reps, log=log)
        print_table(flagged, now)
        log(f"{counts['rows']} loads, {counts['in_status']} in status, {len(flagged)} flagged, {counts['same_day']} same-day, "
            f"{counts['too_new']} too new; * = sales rep taken from the customer's other loads")
        for error in errors:
            log(error)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as handle:
                json.dump({"now": now.isoformat(), "counts": counts, "loads": [l.to_record(now) for l in flagged]}, handle, indent=2)
        return 0

    reps = load_rep_details(args.reps)
    if not reps:
        log(f"warning: no reps read from {args.reps}; every alert will go to the Cc list")
    transport = SesTransport(args.region, args.configuration_set) if args.send else OutboxTransport(args.outbox)
    log(f"{'SENDING via SES' if args.send else 'dry run to ' + args.outbox + '/'} as {args.from_addr}; cc {', '.join(cc) or '(none)'}; "
        f"statuses {'/'.join(statuses)}; min age {args.min_age_hours:g} h; repeat after {args.repeat_hours:g} h"
        + ("; scheduled-date check off" if args.skip_sched_check else "")
        + ("; passed-ETA check off" if args.skip_stale_check else f"; passed ETA after {args.stale_eta_hours:g} h on {args.stale_statuses}")
        + ("; same-day loads included" if args.include_same_day else "; same-day loads skipped")
        + ("; no sales rep inference" if args.no_infer_sales_rep else "")
        + (f"; TEST COPIES redirected to {', '.join(redirect_to)}, nothing recorded" if redirect_to else "")
        + (f"; only {args.only_rep}" if args.only_rep else ""))
    summary = run_once(
        grid, now=now, reps=reps, cc=cc, lookup=lookup, rep_cache=rep_cache, state=AlertState(args.state),
        transport=transport, from_addr=args.from_addr, record=record, sent_log=args.sent_log if record else None,
        repeat_hours=args.repeat_hours, max_lookups=args.max_lookups, redirect_to=redirect_to, only_rep=args.only_rep,
        infer=not args.no_infer_sales_rep, customer_reps=customer_reps, log=log, **rules,
    )
    log(f"done: {summary['flagged']} flagged, {len(summary['emails'])} email(s), {summary['skipped_repeat']} load(s) already alerted, "
        f"{summary['reps_inferred']} sales rep(s) inferred, {len(summary['lookup_errors'])} lookup error(s)")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
