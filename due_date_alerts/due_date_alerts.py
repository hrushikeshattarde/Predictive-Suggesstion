"""Due-date alerts: email each sales rep the Spot Load Map loads that are due within 48 hours.

What it does, once per run:

1. Pull Dispatch > General Dispatch > Spot Load Map (saved search 1786) with every row.
   The grid has a "Due Date" column (``10/16/2026 11:33 *Calculated`` when CargoTel
   derived it from the pickup date and the customer's transit time, ``10/16/2026 00:00``
   when somebody typed it) but no sales rep column.
2. Keep the loads whose due date is at most ``--hours`` (48) away, including the ones
   already past due (``--no-overdue`` drops those).
3. Read each kept load's sales rep from its load page (``loadmaint.mcgi``, the
   ``sales_rep`` / ``sales_rep2`` pickers), through a small cache so a load's page is
   fetched once every ``--rep-cache-hours`` rather than every run.
4. Group the loads by primary sales rep and send one email per rep from the vehicles@
   group, Cc Justice Cromwell and Stuart Mitchell (``--cc``). Loads with no sales rep, or
   whose rep has no address in ``reps.csv``, go to the Cc list directly so somebody can
   route them.
5. Remember what was sent (``alert_state.json``): a load is repeated to the same rep only
   after ``--repeat-hours`` (24), or sooner if its due date or its rep changed. Dry runs
   (no ``--send``) write the emails to ``outbox/`` and leave the state alone.

Usage::

    python due_date_alerts.py --list                     # what is due, nothing composed
    python due_date_alerts.py                            # dry run: emails to outbox/
    python due_date_alerts.py --send                     # really send through SES
    python due_date_alerts.py --from-html pages/spot.html --reps-json reps_by_load.json   # offline

The CargoTel modules (``cargotel_search``, ``suggest_reloads``, ``send_drafts``) live one
folder up; this file adds that folder to ``sys.path`` so it runs from here and from the
flat Lambda zip alike.
"""

from __future__ import annotations

import argparse
import html as html_lib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from cargotel_search import BASE_URL, CargoTelSession, Grid, GridRow, SearchParams, parse_grid  # noqa: E402
from send_drafts import _name_key, load_rep_details  # noqa: E402
from suggest_reloads import DispatchedLoad, fetch_dispatched_load, nice_name  # noqa: E402

__all__ = [
    "AlertState",
    "DueLoad",
    "Email",
    "FileTransport",
    "RepCache",
    "RepGroup",
    "SesTransport",
    "address",
    "compose",
    "due_loads_from_grid",
    "group_by_rep",
    "main",
    "parse_due",
    "redirect",
    "run_once",
]

SPOT_LOAD_MAP_ID = "1786"
SPOT_LOAD_MAP_NAME = "Spot Load Map"
DEFAULT_HOURS = 48.0
DEFAULT_REPEAT_HOURS = 24.0
DEFAULT_REP_CACHE_HOURS = 12.0
DEFAULT_MAX_LOOKUPS = 150
DEFAULT_FROM = "Circle Logistics <vehicles@circledelivers.com>"
#: Copied on every alert. Justice Cromwell's address is the one in reps.csv (from the 8x8
#: directory); Stuart Mitchell's is the MAIN email on his TransportPro user record
#: (user 1349, VP of Finished Vehicles).
DEFAULT_CC = ("justice.cromwell@circledelivers.com", "smitchell@circledelivers.com")
LOAD_URL = f"{BASE_URL}/backoffice/loadmaint.mcgi?load_id="
#: Group key for loads with no sales rep on the load page.
UNASSIGNED = ""

_DUE_RE = re.compile(r"^\s*(\d{1,2}/\d{1,2}/\d{4})(?:\s+(\d{1,2}:\d{2}))?\s*(\*\s*Calculated)?\s*$", re.I)


def parse_due(text: str | None) -> tuple[datetime | None, bool]:
    """``"10/16/2026 11:33 *Calculated"`` -> ``(datetime, True)``; ``"10/16/2026 00:00"``
    -> ``(datetime, False)``; anything else -> ``(None, False)``."""

    if not text:
        return None, False
    match = _DUE_RE.match(text)
    if not match:
        return None, False
    day, clock, calculated = match.groups()
    try:
        when = datetime.strptime(f"{day} {clock or '00:00'}", "%m/%d/%Y %H:%M")
    except ValueError:
        return None, False
    return when, bool(calculated)


def _money(text: str) -> str:
    try:
        return f"${float(text.replace(',', '')):,.2f}"
    except (TypeError, ValueError):
        return text or ""


def _int(text: Any, default: int = 0) -> int:
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return default


def hours_text(hours: float) -> str:
    """``-50.5`` -> ``"2 d 3 h past due"``; ``31.2`` -> ``"1 d 7 h left"``; ``48`` -> ``"2 d left"``; ``0.4`` -> ``"due now"``."""

    whole = int(abs(hours) + 0.5)
    if whole == 0:
        return "due now"
    days, rem = divmod(whole, 24)
    if not days:
        span = f"{rem} h"
    elif not rem:
        span = f"{days} d"
    else:
        span = f"{days} d {rem} h"
    return f"{span} past due" if hours < 0 else f"{span} left"


# --------------------------------------------------------------------------- #
# The loads                                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class DueLoad:
    load_id: str
    kind: str  # "load" (bundle parent, several orders) or "order" (single)
    customer: str
    origin_name: str
    origin_city: str
    origin_zip: str
    dest_name: str
    dest_city: str
    dest_zip: str
    pickup: str
    due: datetime
    due_text: str
    due_calculated: bool
    units: int
    model: str
    price: str
    carrier: str
    miles: str
    problems: str
    comments: str
    po_number: str = ""
    sales_rep: str | None = None
    sales_rep2: str | None = None
    dispatch_rep: str | None = None
    rep_source: str = ""  # page | cache | error

    @property
    def url(self) -> str:
        return LOAD_URL + self.load_id

    def hours_left(self, now: datetime) -> float:
        return (self.due - now).total_seconds() / 3600

    def is_overdue(self, now: datetime) -> bool:
        return self.due < now

    @property
    def rep_key(self) -> str:
        return _name_key(self.sales_rep) if self.sales_rep else UNASSIGNED

    @property
    def lane(self) -> str:
        origin = " ".join(p for p in (self.origin_city, self.origin_zip) if p)
        dest = " ".join(p for p in (self.dest_city, self.dest_zip) if p)
        return f"{origin} -> {dest}"

    def to_record(self, now: datetime) -> dict[str, Any]:
        record = asdict(self)
        record["due"] = self.due.isoformat(timespec="minutes")
        record["hours_left"] = round(self.hours_left(now), 1)
        record["url"] = self.url
        return record


def _first_last(lines: list[str]) -> tuple[str, str]:
    """``["Wabash - Pa", "Jonestown, PA"]`` -> ``("Wabash - Pa", "Jonestown, PA")``; a
    single line is both the name and the place."""

    lines = [line.strip() for line in lines if line.strip()]
    if not lines:
        return "", ""
    if len(lines) == 1:
        return lines[0], lines[0]
    return lines[0], lines[-1]


def _customer(section: str | None) -> str:
    text = (section or "").strip()
    return re.sub(r"^Customer:\s*", "", text, flags=re.I)


def due_load_from_row(row: GridRow) -> tuple[DueLoad | None, str | None]:
    """One grid row -> a :class:`DueLoad`, or ``(None, reason)`` when it has no usable
    due date."""

    due_text = row.text("duedate")
    due, calculated = parse_due(due_text)
    if due is None:
        return None, f"load {row.load_id}: no due date ({due_text!r})"
    origin_name, origin_city = _first_last(row.lines("origin"))
    dest_name, dest_city = _first_last(row.lines("destination"))
    kind = "load" if (row.load.get("type") == "BundleParent" or row.link_class == "LinkBundleParent") else "order"
    return DueLoad(
        load_id=row.load_id or "",
        kind=kind,
        customer=_customer(row.section),
        origin_name=origin_name,
        origin_city=origin_city,
        origin_zip=row.text("originzip") or (row.origin.zip if row.origin else "") or "",
        dest_name=dest_name,
        dest_city=dest_city,
        dest_zip=row.text("destinationzip") or (row.dest.zip if row.dest else "") or "",
        pickup=row.text("pickup"),
        due=due,
        due_text=due_text,
        due_calculated=calculated,
        units=_int(row.load.get("det_cnt"), default=max(1, len(row.lines("vins")))),
        model=row.text("model"),
        price=row.text("consignorprice"),
        carrier=row.text("carriername"),
        miles=str(row.load.get("miles") or ""),
        problems=row.text("problemsYN"),
        comments=row.text("bol_comments"),
        po_number=row.text("cons_ref_load"),
    ), None


def due_loads_from_grid(grid: Grid, now: datetime, hours: float = DEFAULT_HOURS, *, include_overdue: bool = True) -> tuple[list[DueLoad], list[str]]:
    """The loads due within ``hours`` of ``now`` (past-due ones included unless told
    otherwise), earliest first, plus warnings for rows that could not be read."""

    picked: list[DueLoad] = []
    warnings: list[str] = []
    for row in grid.rows:
        if not row.load_id:
            continue
        if row.link_class == "LinkBundleChild":
            continue  # members are shown under their parent load
        load, problem = due_load_from_row(row)
        if load is None:
            warnings.append(problem or f"load {row.load_id}: unreadable")
            continue
        left = load.hours_left(now)
        if left > hours:
            continue
        if left < 0 and not include_overdue:
            continue
        picked.append(load)
    picked.sort(key=lambda l: (l.due, l.load_id))
    return picked, warnings


# --------------------------------------------------------------------------- #
# Sales reps                                                                   #
# --------------------------------------------------------------------------- #


class RepCache:
    """``rep_cache.json``: ``{load_id: {"sales_rep", "sales_rep2", "dispatch_rep", "at"}}``.
    A load page takes three to four seconds to fetch, so each load is looked up once per
    ``max_age_hours`` rather than every run."""

    def __init__(self, path: str | None, max_age_hours: float = DEFAULT_REP_CACHE_HOURS) -> None:
        self.path = path
        self.max_age = timedelta(hours=max_age_hours)
        self.entries: dict[str, dict[str, Any]] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.entries = data.get("loads", {}) if isinstance(data, dict) else {}

    def get(self, load_id: str, now: datetime) -> dict[str, Any] | None:
        entry = self.entries.get(load_id)
        if not entry:
            return None
        try:
            at = datetime.fromisoformat(entry["at"])
        except (KeyError, ValueError):
            return None
        if now - at > self.max_age:
            return None
        return entry

    def put(self, load_id: str, load: DispatchedLoad, now: datetime) -> dict[str, Any]:
        entry = {
            "sales_rep": load.sales_rep,
            "sales_rep2": load.sales_rep2,
            "dispatch_rep": load.dispatch_rep,
            "customer": load.customer,
            "at": now.isoformat(timespec="seconds"),
        }
        self.entries[load_id] = entry
        return entry

    def prune(self, now: datetime, keep_days: int = 30) -> None:
        cutoff = now - timedelta(days=keep_days)
        for load_id in list(self.entries):
            try:
                if datetime.fromisoformat(self.entries[load_id]["at"]) < cutoff:
                    del self.entries[load_id]
            except (KeyError, ValueError):
                del self.entries[load_id]

    def save(self) -> None:
        if not self.path:
            return
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump({"updated": datetime.now().isoformat(timespec="seconds"), "loads": self.entries}, handle, indent=2)
        os.replace(tmp, self.path)


def attach_reps(loads: list[DueLoad], lookup: Callable[[str], DispatchedLoad] | None, cache: RepCache, now: datetime,
                *, max_lookups: int = DEFAULT_MAX_LOOKUPS, log: Callable[[str], None] = lambda m: None) -> list[str]:
    """Fill ``sales_rep`` / ``sales_rep2`` / ``dispatch_rep`` on every load, from the cache
    when fresh, else from ``lookup(load_id)`` (the load page). Returns the lookup errors;
    a load whose page could not be read keeps ``rep_source == "error"`` and is treated as
    unassigned, so it is still reported rather than dropped."""

    errors: list[str] = []
    lookups = 0
    for load in loads:
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
        load.dispatch_rep = entry.get("dispatch_rep") or None
        if not load.customer and entry.get("customer"):
            load.customer = entry["customer"]
        load.rep_source = source
    return errors


def page_lookup(session: CargoTelSession) -> Callable[[str], DispatchedLoad]:
    """A ``lookup(load_id)`` over the shared session that retries once with a fresh cookie
    when the page comes back without the load form (stale session)."""

    def lookup(load_id: str) -> DispatchedLoad:
        try:
            return fetch_dispatched_load(session, load_id)[0]
        except RuntimeError as exc:
            if "stale" not in str(exc):
                raise
            session.cookie(refresh=True)
            return fetch_dispatched_load(session, load_id)[0]

    return lookup


@dataclass
class RepGroup:
    key: str  # _name_key(sales rep) or UNASSIGNED
    name: str | None  # "Kyle Link", None when unassigned
    loads: list[DueLoad] = field(default_factory=list)

    @property
    def unassigned(self) -> bool:
        return self.key == UNASSIGNED


def group_by_rep(loads: list[DueLoad]) -> list[RepGroup]:
    """One group per primary sales rep, alphabetical, the unassigned group last."""

    groups: dict[str, RepGroup] = {}
    for load in loads:
        key = load.rep_key
        group = groups.get(key)
        if group is None:
            group = groups[key] = RepGroup(key=key, name=nice_name(load.sales_rep) if load.sales_rep else None)
        group.loads.append(load)
    ordered = sorted((g for g in groups.values() if not g.unassigned), key=lambda g: g.name or "")
    if UNASSIGNED in groups:
        ordered.append(groups[UNASSIGNED])
    return ordered


# --------------------------------------------------------------------------- #
# The email                                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class Email:
    key: str
    rep: str | None
    to: list[str]
    cc: list[str]
    subject: str
    text: str
    html: str
    load_ids: list[str]
    routed: bool = False  # True when the rep could not be addressed and the Cc list got it

    def dump(self, from_addr: str) -> str:
        head = [f"From: {from_addr}", f"To: {', '.join(self.to)}"]
        if self.cc:
            head.append(f"Cc: {', '.join(self.cc)}")
        head.append(f"Subject: {self.subject}")
        return "\n".join(head) + "\n\n" + self.text + "\n"


def _dedupe(addresses: list[str], *, exclude: list[str] = ()) -> list[str]:
    seen = {a.lower() for a in exclude}
    out: list[str] = []
    for address in addresses:
        address = (address or "").strip()
        if address and address.lower() not in seen:
            seen.add(address.lower())
            out.append(address)
    return out


def redirect(email: Email, to: list[str]) -> Email:
    """A test copy: the same email, sent only to ``to``, with a note on top saying where it
    would have gone. Used to show someone the real thing before the reps get it."""

    would = f"To: {', '.join(email.to) or '(nobody)'}" + (f"; Cc: {', '.join(email.cc)}" if email.cc else "")
    note = f"TEST COPY. This alert would have been sent {would}."
    html = email.html
    marker = html.find(">", html.find("<body")) + 1
    html = html[:marker] + f'<p style="background:#fff4ce;padding:6px 8px"><b>{html_lib.escape(note)}</b></p>' + html[marker:]
    return Email(key=email.key, rep=email.rep, to=list(to), cc=[], subject="[Test copy] " + email.subject,
                 text=note + "\n\n" + email.text, html=html, load_ids=list(email.load_ids), routed=email.routed)


def address(group: RepGroup, reps: dict[str, dict[str, str]], cc: list[str]) -> tuple[list[str], list[str], bool]:
    """``(to, cc, routed)``. The rep gets To and the managers Cc; when the rep has no
    address (or the loads have no rep) the managers get To, so the alert is never lost."""

    rep = reps.get(group.key) if group.key else None
    rep_email = (rep or {}).get("email", "")
    if not rep_email:
        return _dedupe(list(cc)), [], True
    extra: list[str] = []
    for load in group.loads:
        if load.sales_rep2:
            second = reps.get(_name_key(load.sales_rep2), {}).get("email", "")
            if second:
                extra.append(second)
    return [rep_email], _dedupe(list(cc) + extra, exclude=[rep_email]), False


def _first_name(name: str | None) -> str:
    return (name or "").split()[0] if name else ""


def _fmt_due(load: DueLoad) -> str:
    stamp = load.due.strftime("%a %m/%d %H:%M")
    return stamp + (" (calculated)" if load.due_calculated else "")


def _fmt_when(now: datetime) -> str:
    return f"{now:%a %m/%d/%Y} {now.strftime('%I:%M %p').lstrip('0')} ET"


def _text_block(load: DueLoad, now: datetime, *, show_dispatch_rep: bool) -> str:
    what = f"{load.units} unit{'s' if load.units != 1 else ''}" + (f": {load.model}" if load.model else "")
    head = (f"{'Load' if load.kind == 'load' else 'Order'} {load.load_id}  |  {load.customer or 'customer ?'}  |  "
            f"due {_fmt_due(load)}  |  {hours_text(load.hours_left(now))}")
    lines = [head, f"  {load.lane}" + (f"  ({load.miles} mi)" if load.miles else "")]
    detail = [what]
    if load.pickup:
        detail.append(f"pickup {load.pickup}")
    if load.price:
        detail.append(f"price {_money(load.price)}")
    if load.carrier:
        detail.append(f"carrier {load.carrier}")
    lines.append("  " + "  |  ".join(detail))
    notes = []
    if load.problems and load.problems.strip().lower() not in ("", "no", "n"):
        notes.append(f"problems flagged: {load.problems}")
    if load.comments:
        notes.append(f"BOL: {load.comments}")
    if show_dispatch_rep:
        notes.append(f"sales rep: {nice_name(load.sales_rep) if load.sales_rep else 'none'}; "
                     f"dispatch rep: {nice_name(load.dispatch_rep) if load.dispatch_rep else 'none'}")
    for note in notes:
        lines.append("  " + note)
    lines.append("  " + load.url)
    return "\n".join(lines)


def _html_rows(loads: list[DueLoad], now: datetime, *, show_dispatch_rep: bool) -> str:
    esc = html_lib.escape
    out = []
    for load in loads:
        overdue = load.is_overdue(now)
        style = ' style="background:#fdecea"' if overdue else ""
        what = f"{load.units} x {esc(load.model)}" if load.model else str(load.units)
        cells = [
            f'<a href="{esc(load.url)}">{esc(load.load_id)}</a>' + ("" if load.kind == "load" else "<br><small>order</small>"),
            esc(load.customer or ""),
            f"{esc(load.origin_city)} {esc(load.origin_zip)}<br>&rarr; {esc(load.dest_city)} {esc(load.dest_zip)}"
            + (f"<br><small>{esc(load.miles)} mi</small>" if load.miles else ""),
            esc(load.pickup),
            esc(_fmt_due(load)),
            ("<b>" if overdue else "") + esc(hours_text(load.hours_left(now))) + ("</b>" if overdue else ""),
            what,
            esc(_money(load.price)) if load.price else "",
        ]
        if show_dispatch_rep:
            cells.append(esc(nice_name(load.sales_rep) if load.sales_rep else "none") + "<br><small>dispatch: "
                         + esc(nice_name(load.dispatch_rep) if load.dispatch_rep else "none") + "</small>")
        out.append(f"<tr{style}>" + "".join(
            f'<td style="padding:4px 8px;border-bottom:1px solid #ddd;vertical-align:top">{c}</td>' for c in cells) + "</tr>")
    return "\n".join(out)


FOOTNOTE = ("Due dates marked (calculated) are CargoTel's pickup date plus the customer's transit time; "
            "the others were entered by hand.")
TRAILER = (f"This alert is generated from the {SPOT_LOAD_MAP_NAME} and repeats once a day while a load stays on it. "
           "Reply to this email if a load is mis-assigned.")


def compose(group: RepGroup, reps: dict[str, dict[str, str]], *, cc: list[str], now: datetime, hours: float = DEFAULT_HOURS) -> Email:
    """Address and write the alert for one rep group (plain text and HTML)."""

    to, cc_list, routed = address(group, reps, cc)
    loads = sorted(group.loads, key=lambda l: (l.due, l.load_id))
    overdue = [l for l in loads if l.is_overdue(now)]
    soon = [l for l in loads if not l.is_overdue(now)]
    n = len(loads)
    counts = f"{n} load{'s' if n != 1 else ''} due within {int(hours)} hours"
    if overdue:
        counts += f" ({len(overdue)} past due)"
    if group.unassigned:
        who = "no sales rep assigned"
    elif routed:
        who = f"{group.name} (no email on file)"
    else:
        who = group.name or ""
    subject = f"Due-date alert: {counts} - {who}"

    show_dispatch_rep = routed  # the managers need to know who to hand them to
    greeting = f"Hi {_first_name(group.name)}," if (group.name and not routed) else "Hi Justice and Stuart,"
    if group.unassigned:
        intro = (f"As of {_fmt_when(now)}, these loads on the {SPOT_LOAD_MAP_NAME} are due within {int(hours)} hours or already past due "
                 f"and have NO sales rep on the load page, so nobody was alerted directly. Please assign a rep or get them covered.")
    elif routed:
        intro = (f"As of {_fmt_when(now)}, these loads on the {SPOT_LOAD_MAP_NAME} are due within {int(hours)} hours or already past due. "
                 f"Their sales rep is {group.name}, who has no email address in reps.csv, so this alert comes to you instead.")
    else:
        intro = (f"As of {_fmt_when(now)}, these loads on the {SPOT_LOAD_MAP_NAME} (Dispatch > General Dispatch) are due within {int(hours)} hours"
                 + (" or already past due" if overdue else "")
                 + ". Please get them covered or update the due date in CargoTel if it has changed.")

    text_parts = [greeting, "", intro, ""]
    if overdue:
        text_parts += [f"PAST DUE ({len(overdue)})", ""]
        text_parts += [_text_block(l, now, show_dispatch_rep=show_dispatch_rep) + "\n" for l in overdue]
    if soon:
        text_parts += [f"DUE WITHIN {int(hours)} HOURS ({len(soon)})", ""]
        text_parts += [_text_block(l, now, show_dispatch_rep=show_dispatch_rep) + "\n" for l in soon]
    text_parts += [FOOTNOTE, TRAILER]
    text = "\n".join(text_parts)

    esc = html_lib.escape
    headers = ["Load", "Customer", "Lane", "Pickup", "Due", "Time", "Units", "Price"] + (["Sales / dispatch rep"] if show_dispatch_rep else [])
    th = "".join(f'<th style="text-align:left;padding:4px 8px;border-bottom:2px solid #999">{h}</th>' for h in headers)
    table_style = 'style="border-collapse:collapse;font-size:13px"'
    sections = []
    if overdue:
        sections.append(f'<h3 style="color:#b00020;margin:16px 0 4px">Past due ({len(overdue)})</h3>'
                        f'<table {table_style}><tr>{th}</tr>{_html_rows(overdue, now, show_dispatch_rep=show_dispatch_rep)}</table>')
    if soon:
        sections.append(f'<h3 style="margin:16px 0 4px">Due within {int(hours)} hours ({len(soon)})</h3>'
                        f'<table {table_style}><tr>{th}</tr>{_html_rows(soon, now, show_dispatch_rep=show_dispatch_rep)}</table>')
    html = ('<html><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">'
            f"<p>{esc(greeting)}</p><p>{esc(intro)}</p>" + "".join(sections)
            + f'<p style="font-size:12px;color:#666;margin-top:16px">{esc(FOOTNOTE)}<br>{esc(TRAILER)}</p>'
            "</body></html>")
    return Email(key=group.key, rep=group.name, to=to, cc=cc_list, subject=subject, text=text, html=html,
                 load_ids=[l.load_id for l in loads], routed=routed)


# --------------------------------------------------------------------------- #
# State, transports, log                                                       #
# --------------------------------------------------------------------------- #


class AlertState:
    """``alert_state.json``: ``{"sent": {load_id: {"due", "rep_key", "at"}}}``. A load is
    alerted again after ``repeat_hours``, or at once when its due date or rep changed."""

    def __init__(self, path: str | None) -> None:
        self.path = path
        self.sent: dict[str, dict[str, str]] = {}
        if path and os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.sent = data.get("sent", {}) if isinstance(data, dict) else {}

    def should_alert(self, load: DueLoad, now: datetime, repeat_hours: float) -> bool:
        rec = self.sent.get(load.load_id)
        if not rec:
            return True
        if rec.get("due") != load.due.isoformat(timespec="minutes") or rec.get("rep_key", UNASSIGNED) != load.rep_key:
            return True
        try:
            at = datetime.fromisoformat(rec["at"])
        except (KeyError, ValueError):
            return True
        return now - at >= timedelta(hours=repeat_hours)

    def mark(self, load: DueLoad, now: datetime) -> None:
        self.sent[load.load_id] = {"due": load.due.isoformat(timespec="minutes"), "rep_key": load.rep_key,
                                   "at": now.isoformat(timespec="seconds")}

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


class FileTransport:
    """Dry run: every email becomes ``outbox/<stamp>_<rep>.txt``."""

    def __init__(self, outbox: str) -> None:
        self.outbox = outbox

    def send(self, email: Email, from_addr: str) -> str:
        os.makedirs(self.outbox, exist_ok=True)
        who = re.sub(r"[^A-Za-z0-9]+", "-", email.rep or "unassigned").strip("-").lower()
        path = os.path.join(self.outbox, f"{datetime.now():%Y%m%d-%H%M%S}_{who}.txt")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(email.dump(from_addr))
        return f"file:{path}"


class SesTransport:
    """Amazon SES v2 ``SendEmail`` with a text and an HTML part. The From identity must be
    verified in SES (vehicles@ is, in us-east-1)."""

    def __init__(self, region: str | None = None, configuration_set: str | None = None, client: Any = None) -> None:
        if client is None:
            import boto3

            client = boto3.client("sesv2", region_name=region) if region else boto3.client("sesv2")
        self.client = client
        self.configuration_set = configuration_set

    def send(self, email: Email, from_addr: str) -> str:
        request: dict[str, Any] = {
            "FromEmailAddress": from_addr,
            "Destination": {"ToAddresses": email.to, "CcAddresses": email.cc},
            "Content": {"Simple": {
                "Subject": {"Data": email.subject, "Charset": "UTF-8"},
                "Body": {"Text": {"Data": email.text, "Charset": "UTF-8"}, "Html": {"Data": email.html, "Charset": "UTF-8"}},
            }},
        }
        if self.configuration_set:
            request["ConfigurationSetName"] = self.configuration_set
        response = self.client.send_email(**request)
        return str(response.get("MessageId", ""))


def append_log(path: str | None, entry: dict[str, Any]) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, default=str) + "\n")


# --------------------------------------------------------------------------- #
# One run                                                                      #
# --------------------------------------------------------------------------- #


def fetch_spot_grid(session: CargoTelSession, saved_id: str = SPOT_LOAD_MAP_ID, save_html: str | None = None) -> Grid:
    status, html, _params = session.fetch_search_html(SearchParams(saved_id, per_page="All"))
    if save_html:
        os.makedirs(save_html, exist_ok=True)
        path = os.path.join(save_html, f"spot_{datetime.now():%Y%m%d-%H%M}.html")
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
    hours: float,
    include_overdue: bool,
    reps: dict[str, dict[str, str]],
    cc: list[str],
    lookup: Callable[[str], DispatchedLoad] | None,
    rep_cache: RepCache,
    state: AlertState,
    repeat_hours: float,
    transport: Any,
    from_addr: str,
    record: bool,
    sent_log: str | None,
    max_lookups: int = DEFAULT_MAX_LOOKUPS,
    redirect_to: list[str] | None = None,
    only_rep: str | None = None,
    log: Callable[[str], None] = lambda m: None,
) -> dict[str, Any]:
    """Select, look up, group, compose, send. ``record=False`` (dry run) leaves the alert
    state untouched; the rep cache is always kept, it only saves page fetches.
    ``redirect_to`` sends every email to those addresses instead, as a marked test copy
    that is never recorded; ``only_rep`` limits the run to one rep's email ("unassigned"
    for the loads with no sales rep)."""

    due, warnings = due_loads_from_grid(grid, now, hours, include_overdue=include_overdue)
    for warning in warnings:
        log(warning)
    log(f"{SPOT_LOAD_MAP_NAME}: {len(grid.rows)} rows, {len(due)} due within {int(hours)} h "
        f"({sum(1 for l in due if l.is_overdue(now))} past due)")
    errors = attach_reps(due, lookup, rep_cache, now, max_lookups=max_lookups, log=log)
    rep_cache.prune(now)
    rep_cache.save()

    summary: dict[str, Any] = {"rows": len(grid.rows), "due": len(due), "overdue": sum(1 for l in due if l.is_overdue(now)),
                               "warnings": warnings, "lookup_errors": errors, "emails": [], "skipped_repeat": 0}
    if redirect_to:
        record = False
    wanted = None
    if only_rep:
        wanted = UNASSIGNED if only_rep.strip().lower() == "unassigned" else _name_key(only_rep)
    for group in group_by_rep(due):
        if wanted is not None and group.key != wanted:
            continue
        fresh = [l for l in group.loads if state.should_alert(l, now, repeat_hours)]
        summary["skipped_repeat"] += len(group.loads) - len(fresh)
        label = group.name or "no sales rep"
        if not fresh:
            log(f"{label}: {len(group.loads)} load(s), all alerted within the last {int(repeat_hours)} h; nothing sent")
            continue
        email = compose(RepGroup(group.key, group.name, fresh), reps, cc=cc, now=now, hours=hours)
        if redirect_to:
            email = redirect(email, redirect_to)
        if not email.to:
            log(f"{label}: nobody to send to (empty --cc?); held")
            continue
        try:
            message_id = transport.send(email, from_addr)
        except Exception as exc:  # noqa: BLE001 - keep going with the other reps
            log(f"send failed for {label}: {exc}")
            summary.setdefault("send_errors", []).append(f"{label}: {exc}")
            continue
        verb = "sent" if record else ("test copy sent" if redirect_to else "dry run")
        log(f"{verb}: {email.subject} -> to {', '.join(email.to)}"
            + (f" cc {', '.join(email.cc)}" if email.cc else "") + f" [{message_id}]")
        entry = {"at": now.isoformat(timespec="seconds"), "rep": group.name, "rep_key": group.key, "to": email.to, "cc": email.cc,
                 "subject": email.subject, "loads": email.load_ids, "message_id": message_id, "sent": record,
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


def print_table(loads: list[DueLoad], now: datetime) -> None:
    print(f"{'load':<8} {'kind':<5} {'due':<28} {'time':<18} {'sales rep':<20} {'customer':<28} lane")
    for load in loads:
        print(f"{load.load_id:<8} {load.kind:<5} {_fmt_due(load):<28} {hours_text(load.hours_left(now)):<18} "
              f"{(nice_name(load.sales_rep) if load.sales_rep else '-'):<20} {load.customer[:28]:<28} {load.lane}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=DEFAULT_HOURS, help="alert window in hours (default 48)")
    parser.add_argument("--no-overdue", action="store_true", help="leave out loads already past their due date")
    parser.add_argument("--saved-id", default=SPOT_LOAD_MAP_ID, help="CargoTel saved search id (default 1786, Spot Load Map)")
    parser.add_argument("--reps", default=os.path.join(PARENT, "reps.csv"), help="reps.csv: name,email[,title][,phone] (default ../reps.csv)")
    parser.add_argument("--cc", default=",".join(DEFAULT_CC), help="comma-separated addresses copied on every alert")
    parser.add_argument("--from", dest="from_addr", default=os.environ.get("MAIL_FROM", DEFAULT_FROM), help="verified SES sender")
    parser.add_argument("--send", action="store_true", help="really send through SES; otherwise write to --outbox and keep the state untouched")
    parser.add_argument("--redirect-to", metavar="ADDR[,ADDR]", help="send every email to these addresses instead, as a marked test copy; nothing is recorded")
    parser.add_argument("--only-rep", metavar="NAME", help="only this rep's email (\"Kyle Link\" or \"LINK, KYLE\"; \"unassigned\" for the loads with no rep)")
    parser.add_argument("--region", help="AWS region for SES (default: boto3's)")
    parser.add_argument("--configuration-set", help="SES configuration set for bounce/complaint events")
    parser.add_argument("--outbox", default=os.path.join(HERE, "outbox"), help="dry-run folder")
    parser.add_argument("--state", default=os.path.join(HERE, "alert_state.json"), help="what was alerted and when")
    parser.add_argument("--sent-log", default=os.path.join(HERE, "sent_log.jsonl"), help="one line per email sent")
    parser.add_argument("--rep-cache", default=os.path.join(HERE, "rep_cache.json"), help="sales reps read from load pages")
    parser.add_argument("--rep-cache-hours", type=float, default=DEFAULT_REP_CACHE_HOURS, help="re-read a load's reps after this long (default 12)")
    parser.add_argument("--repeat-hours", type=float, default=DEFAULT_REPEAT_HOURS, help="alert the same load again after this long (default 24; 0 = every run)")
    parser.add_argument("--max-lookups", type=int, default=DEFAULT_MAX_LOOKUPS, help="load pages fetched per run at most")
    parser.add_argument("--save-html", metavar="DIR", help="archive the fetched Spot Load Map page here")
    parser.add_argument("--from-html", metavar="FILE", help="parse a saved page instead of fetching (offline)")
    parser.add_argument("--reps-json", metavar="FILE", help="offline rep lookup {load_id: {sales_rep, ...}} (with --from-html)")
    parser.add_argument("--now", help="pretend it is this time (ISO), for tests")
    parser.add_argument("--list", action="store_true", help="print the due loads and stop; nothing composed")
    parser.add_argument("--json", metavar="FILE", help="also write the run summary (or, with --list, the due loads) here")
    args = parser.parse_args(argv)

    now = datetime.fromisoformat(args.now) if args.now else datetime.now().replace(microsecond=0)
    cc = [a.strip() for a in args.cc.split(",") if a.strip()]
    redirect_to = [a.strip() for a in (args.redirect_to or "").split(",") if a.strip()] or None
    record = args.send and not redirect_to

    def log(message: str) -> None:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} alerts: {message}", flush=True)

    if args.from_html:
        with open(args.from_html, encoding="utf-8") as handle:
            grid = parse_grid(handle.read())
        lookup = load_reps_by_load(args.reps_json)
    else:
        session = CargoTelSession()
        grid = fetch_spot_grid(session, args.saved_id, args.save_html)
        lookup = page_lookup(session)

    rep_cache = RepCache(args.rep_cache, args.rep_cache_hours)
    if args.list:
        due, warnings = due_loads_from_grid(grid, now, args.hours, include_overdue=not args.no_overdue)
        for warning in warnings:
            log(warning)
        errors = attach_reps(due, lookup, rep_cache, now, max_lookups=args.max_lookups, log=log)
        rep_cache.save()
        print_table(due, now)
        for error in errors:
            log(error)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as handle:
                json.dump({"now": now.isoformat(), "loads": [l.to_record(now) for l in due]}, handle, indent=2)
        return 0

    reps = load_rep_details(args.reps)
    if not reps:
        log(f"warning: no reps read from {args.reps}; every alert will go to the Cc list")
    transport = SesTransport(args.region, args.configuration_set) if args.send else FileTransport(args.outbox)
    log(f"{'SENDING via SES' if args.send else 'dry run to ' + args.outbox + '/'} as {args.from_addr}; cc {', '.join(cc) or '(none)'}; "
        f"window {args.hours:g} h; repeat after {args.repeat_hours:g} h"
        + (f"; TEST COPIES redirected to {', '.join(redirect_to)}, nothing recorded" if redirect_to else "")
        + (f"; only {args.only_rep}" if args.only_rep else ""))
    summary = run_once(
        grid, now=now, hours=args.hours, include_overdue=not args.no_overdue, reps=reps, cc=cc, lookup=lookup,
        rep_cache=rep_cache, state=AlertState(args.state), repeat_hours=args.repeat_hours, transport=transport,
        from_addr=args.from_addr, record=record, sent_log=args.sent_log if record else None,
        max_lookups=args.max_lookups, redirect_to=redirect_to, only_rep=args.only_rep, log=log,
    )
    log(f"done: {summary['due']} due, {len(summary['emails'])} email(s), {summary['skipped_repeat']} load(s) already alerted, "
        f"{len(summary['lookup_errors'])} lookup error(s)")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(summary, handle, indent=2, default=str)
    return 0


if __name__ == "__main__":
    sys.exit(main())
