"""
Reload suggestions for a dispatched CargoTel load (dry run: drafts the email, never sends).

The spec: once a load is dispatched, find the open loads and orders whose origin lies
within 150 miles of that load's DESTINATION zip, and email the carrier's dispatcher and
Circle's dispatch rep 5 to 10 of them. Built loads are offered first; single orders only
when no built load qualifies (or to top the list up to five).

Pipeline::

    loadmaint.mcgi?load_id=<id>  ->  destination zip, delivery time, carrier, dispatch rep
    Search.mcgi 1788 (General Load Build w/ Map), radius row 1 = dest zip, 150 mi, all rows
    group bundle members by their parent load; single orders stay single
    rank (deadhead miles, direction, age, priced), loads first, cap 5..10
    render the draft email (text) + JSON for the sender that comes later

Usage::

    python suggest_reloads.py 344141                       # live, prints the draft
    python suggest_reloads.py 344141 --out draft.txt --json picks.json --save-html pages/
    python suggest_reloads.py --zip 60477                  # demo: skip the load page
    python suggest_reloads.py --from-load-html load.html --from-search-html search.html

Everything CargoTel-facing goes through ``cargotel_search`` (same S3 cookie as
``pull_load_detail.py``). Parsing is standard library only.
"""

from __future__ import annotations

import argparse
import collections
import html as html_lib
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from cargotel_search import (
    BASE_URL,
    CargoTelSession,
    Grid,
    GridRow,
    SearchParams,
    haversine_miles,
    parse_grid,
)

LOADMAINT_URL = f"{BASE_URL}/backoffice/loadmaint.mcgi"

#: The saved search the spec names: Dispatch > General Dispatch > General Load Build w/ Map.
DEFAULT_SAVED_ID = "1788"
DEFAULT_MILES = 150
DEFAULT_MIN = 5
DEFAULT_MAX = 10

#: Ranking is a miles-equivalent score, lower is better: deadhead miles from the delivery
#: point, plus a penalty when the candidate heads away from where the truck wants to go,
#: minus small bonuses for old inventory and for having a price on it.
DIRECTION_PENALTY = {"home": 0, "back": 40, "other": 80}
AGE_BONUS_PER_DAY = 2.0
AGE_BONUS_CAP_DAYS = 10
PRICED_BONUS = 10.0


# --------------------------------------------------------------------------- #
# The dispatched load (loadmaint.mcgi)                                         #
# --------------------------------------------------------------------------- #


@dataclass
class Stop:
    name: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    when: str | None = None  # as shown, e.g. "12-Oct-2026 Mon 11 AM EST"
    date: str | None = None  # ISO date parsed from ``when``

    @property
    def city_state(self) -> str:
        return ", ".join(p for p in (self.city, self.state) if p)


@dataclass
class Carrier:
    client_id: str | None = None
    name: str | None = None
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    city: str | None = None
    state: str | None = None


@dataclass
class DispatchedLoad:
    load_id: str
    customer: str | None = None
    origin: Stop = field(default_factory=Stop)
    destination: Stop = field(default_factory=Stop)
    carrier: Carrier = field(default_factory=Carrier)
    dispatch_rep_id: str | None = None
    dispatch_rep: str | None = None
    #: Primary and secondary sales reps on the load (``sales_rep`` / ``sales_rep2``). The
    #: email is Cc'd to them and names the primary one as the contact.
    sales_rep_id: str | None = None
    sales_rep: str | None = None
    sales_rep2_id: str | None = None
    sales_rep2: str | None = None
    vehicles: list[dict[str, str]] = field(default_factory=list)
    #: True when the Print Docs menu lists an "E-Signed Carrier Agreement": the carrier
    #: accepted the offer through CargoTel's e-signature flow.
    esigned: bool = False

    @property
    def contact_rep(self) -> str | None:
        """Who the carrier should call and who signs: the sales rep assigned to the load.
        Nobody else; a load without a sales rep is not emailed."""

        return self.sales_rep

    @property
    def contact_rep_display(self) -> str | None:
        return nice_name(self.contact_rep) if self.contact_rep else None

    @property
    def cc_reps(self) -> list[str]:
        """Rep names to Cc: the primary sales rep, then the secondary when set. De-duplicated."""

        out: list[str] = []
        for name in (self.sales_rep, self.sales_rep2):
            if name and nice_name(name).lower() not in {nice_name(o).lower() for o in out}:
                out.append(name)
        return out

    @property
    def units(self) -> int:
        return len(self.vehicles)

    @property
    def is_dispatched(self) -> bool:
        return bool(self.carrier.client_id)

    def vehicle_summary(self) -> str:
        if not self.vehicles:
            return ""
        models = collections.Counter(v.get("description", "") for v in self.vehicles)
        if len(models) > 3:
            return f"{self.units} vehicles"
        return "; ".join(f"{n} x {m}" if n > 1 else m for m, n in models.most_common())

    @property
    def rep_display(self) -> str | None:
        return nice_name(self.dispatch_rep) if self.dispatch_rep else None


def nice_name(name: str) -> str:
    """CargoTel's ``LAST, FIRST`` in capitals -> ``First Last``. Runs of apostrophes are
    collapsed: the user list stores ``O''''''''NEILL`` for O'Neill."""

    name = re.sub(r"'{2,}", "'", name)
    if "," in name:
        last, first = (p.strip() for p in name.split(",", 1))
        name = f"{first} {last}"

    def cap(word: str) -> str:
        if not (word.isupper() or word.islower()):
            return word
        return "-".join("'".join(p.capitalize() for p in part.split("'")) for part in word.split("-"))

    return " ".join(cap(w) for w in name.split())


_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_WHEN_RE = re.compile(r"^(\d{1,2}-[A-Za-z]{3}-\d{4})(?:\s+[A-Za-z]{3})?(?:\s+\d{1,2}(?::\d{2})?\s*[AP]M)?(?:\s+[A-Z]{2,4})?$")
_CITY_STATE_ZIP_RE = re.compile(r"^(.*?),\s*([A-Z]{2})\b\s*(\d{5})?")
_SELCAR_RE = re.compile(r'name="selCar_\d+"\s+value="(\d*)"')
_SELCAR_NAME_RE = re.compile(r'selected_carrier_name="([^"]*)"')
_SELECTED_OPTION_RE = re.compile(r'<option value="(\d+)"[^>]*\bselected\b[^>]*>([^<]*)', re.I)


def _selected_user(html: str, select_name: str) -> tuple[str | None, str | None]:
    """``(id, "LAST, FIRST")`` of the selected option in ``<select name=...>``, for the
    rep pickers on the load page (``ass_user`` = dispatch rep, ``sales_rep``,
    ``sales_rep2``). CargoTel stores O'Neill as ``O''''''''NEILL``; collapsed here."""

    match = re.search(r'<select name="%s"[^>]*>(.*?)</select>' % re.escape(select_name), html, re.S | re.I)
    if not match:
        return None, None
    selected = _SELECTED_OPTION_RE.search(match.group(1))
    if not selected or selected.group(1) == "0":
        return None, None
    return selected.group(1), re.sub(r"'{2,}", "'", " ".join(html_lib.unescape(selected.group(2)).split()))
#: "Automobile <VIN> | 2027 FIAT TOPOLINO DOLCE VITA |" once <br> is turned into "|"; the
#: description always starts with the model year, which is what anchors it.
_VEHICLE_RE = re.compile(r"Automobile\s+([A-HJ-NPR-Z0-9]{17})\s*\|?\s*((?:19|20)\d{2}\s[^|<]+?)\s*(?:\||$)", re.M)
_MAILTO_RE = re.compile(r'mailto:([^"\'>]+)', re.I)


def _cell_segments(html: str, cell_id: str) -> list[str]:
    """Visible text of ``<td id="<cell_id>">`` split on its ``<br>`` breaks."""

    match = re.search(r"""id=['"]%s['"][^>]*>(.*?)</td>""" % re.escape(cell_id), html, re.S | re.I)
    if match is None:
        return []
    inner = _BR_RE.sub("|", match.group(1))
    text = html_lib.unescape(_TAG_RE.sub(" ", inner)).replace("\xa0", " ")
    return [" ".join(seg.split()) for seg in text.split("|") if seg.strip()]


def _parse_when(segment: str) -> tuple[str | None, str | None]:
    match = _WHEN_RE.match(segment)
    if match is None:
        return None, None
    try:
        return segment, datetime.strptime(match.group(1), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return segment, None


def _parse_stop(segments: list[str]) -> Stop:
    """``["12-Oct-2026 Mon 11 AM EST", "Bettenhausen Motor Sales Inc (54982)",
    "Orland Hills, IL 60477, 381457000"]`` -> Stop."""

    stop = Stop()
    rest = list(segments)
    if rest:
        when, date = _parse_when(rest[0])
        if when:
            stop.when, stop.date = when, date
            rest = rest[1:]
    for seg in rest:
        match = _CITY_STATE_ZIP_RE.match(seg)
        if match and (match.group(3) or stop.name):
            stop.city = match.group(1).strip() or None
            stop.state = match.group(2)
            stop.zip = match.group(3)
            break
        if stop.name is None:
            stop.name = seg
    return stop


def _parse_carrier(html: str) -> Carrier:
    carrier = Carrier()
    sel = _SELCAR_RE.search(html)
    if sel and sel.group(1):
        carrier.client_id = sel.group(1)
    name = _SELCAR_NAME_RE.search(html)
    if name and name.group(1):
        carrier.name = name.group(1).strip()
    if carrier.name is None:
        # Delivered loads no longer render the carrier-select widget; the compact cell
        # still names the carrier ("R G Motors Transport Llc | Pensacola, FL 32503").
        compact = _cell_segments(html, "min_carrier")
        if compact and not re.match(r"^\[?Edit\]?$", compact[0]):
            carrier.name = compact[0]
    segments = _cell_segments(html, "max_carrier")
    for seg in segments:
        if seg.startswith("ID:") and not carrier.client_id:
            carrier.client_id = seg.split(":", 1)[1].strip() or None
        elif seg.startswith("Phone:"):
            carrier.phone = seg.split(":", 1)[1].strip() or None
        elif seg.startswith("Email:"):
            carrier.email = seg.split(":", 1)[1].strip() or None
        else:
            place = re.match(r"^([A-Z .'&-]+),\s*([A-Z]{2})(?:\s+\d{5}(?:-\d{4})?)?$", seg)
            if place:
                carrier.city, carrier.state = place.group(1).strip().title(), place.group(2)
    if carrier.email is None:
        mail = re.search(r"""id=['"]max_carrier['"][^>]*>(.*?)</td>""", html, re.S | re.I)
        if mail:
            found = _MAILTO_RE.search(mail.group(1))
            if found:
                carrier.email = html_lib.unescape(found.group(1))
    # The person line sits between "Phone:" and the company name.
    plain = [s for s in segments if ":" not in s and s != "[Edit]"]
    if carrier.name:
        plain = [s for s in plain if s.upper() != carrier.name.upper()]
    for seg in plain:
        if seg.strip().lower() in _PLACEHOLDER_CONTACTS:
            continue
        if re.match(r"^[A-Za-z][A-Za-z .'-]+$", seg) and not re.search(r"\d", seg) and "," not in seg:
            carrier.contact_name = seg
            break
    return carrier


#: Names CargoTel shows when no real person is on the carrier record.
_PLACEHOLDER_CONTACTS = {"general contact", "dispatch", "main", "office", "contact"}


def parse_load_page(html: str, load_id: str | None = None) -> DispatchedLoad:
    """Pure function of a ``loadmaint.mcgi`` page."""

    if load_id is None:
        hidden = re.search(r'name="selCar_(\d+)"', html)
        load_id = hidden.group(1) if hidden else "?"
    load = DispatchedLoad(load_id=str(load_id))
    customer = _cell_segments(html, "min_customer")
    load.customer = customer[0] if customer else None
    load.origin = _parse_stop(_cell_segments(html, "min_origin"))
    load.destination = _parse_stop(_cell_segments(html, "min_destination"))
    if load.destination.zip is None:
        # The expanded cell repeats the address with the zip when the compact one lacks it.
        fallback = _parse_stop([s for s in _cell_segments(html, "max_destination") if not s.startswith(("ID:", "Phone:"))])
        if fallback.zip:
            load.destination.zip = fallback.zip
            load.destination.city = load.destination.city or (fallback.city.title() if fallback.city else None)
            load.destination.state = load.destination.state or fallback.state
    load.carrier = _parse_carrier(html)
    load.dispatch_rep_id, load.dispatch_rep = _selected_user(html, "ass_user")
    load.sales_rep_id, load.sales_rep = _selected_user(html, "sales_rep")
    load.sales_rep2_id, load.sales_rep2 = _selected_user(html, "sales_rep2")
    text = html_lib.unescape(_TAG_RE.sub(" ", _BR_RE.sub("|", html)))
    seen: dict[str, str] = {}
    for vin, description in _VEHICLE_RE.findall(text):
        seen.setdefault(vin, " ".join(description.split()))
    load.vehicles = [{"vin": vin, "description": desc} for vin, desc in seen.items()]
    load.esigned = "E-Signed Carrier Agreement" in text
    return load


class NotAcceptedError(RuntimeError):
    """The load has no e-signed carrier agreement yet (offer not accepted)."""


def fetch_dispatched_load(session: CargoTelSession, load_id: str) -> tuple[DispatchedLoad, str]:
    status, html = session.get(LOADMAINT_URL, {"load_id": load_id})
    if status != 200:
        raise RuntimeError(f"loadmaint.mcgi returned HTTP {status} for load {load_id}")
    if "Invalid Order ID" in html:
        raise RuntimeError(f"CargoTel says {load_id} is not a load (Invalid Order ID)")
    if "loadmaint_form__" not in html:
        raise RuntimeError(f"no load form on the page for {load_id}; the session cookie is probably stale")
    return parse_load_page(html, load_id), html


# --------------------------------------------------------------------------- #
# Candidates                                                                   #
# --------------------------------------------------------------------------- #


@dataclass
class Suggestion:
    kind: str  # "load" (built, several orders) or "order" (single)
    load_id: str  # the parent load for a built load
    units: int
    origin: str  # "Elwood, Il 60421"
    origin_zip: str | None
    deadhead_miles: float | None  # from the delivery point to the candidate's origin
    destinations: list[tuple[str, int]]  # [("Rhinelander, WI 54501", 4), ...]
    dest_states: list[str]
    road_miles: int | None  # CargoTel's own miles, the longest member for a built load
    models: list[tuple[str, int]]
    price: float | None  # sum over members when every member is priced
    age_days: int | None
    member_ids: list[str]
    direction: str = "other"
    score: float = 0.0

    def dest_summary(self) -> str:
        return ", ".join(f"{d} ({n})" if len(self.destinations) > 1 else d for d, n in self.destinations)

    def model_summary(self) -> str:
        return ", ".join(f"{n} x {m}" if n > 1 else m for m, n in self.models)


def _to_int(value: str | None) -> int | None:
    try:
        return int(float(value)) if value not in (None, "") else None
    except ValueError:
        return None


def _to_float(value: str | None) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def _suggestion(kind: str, load_id: str, members: list[GridRow], center: tuple[float, float] | None) -> Suggestion:
    first = members[0]
    dests = collections.Counter((m.dest.city_state if m.dest and m.dest.city_state else m.text("destination")) for m in members)
    models = collections.Counter(m.text("model") or "?" for m in members)
    prices = [_to_float(m.load.get("price")) for m in members]
    priced = [p for p in prices if p]
    ages = [a for a in (_to_int(m.text("age_SLL_d_Inventory")) for m in members) if a is not None]
    miles = [r for r in (_to_int(m.load.get("miles")) for m in members) if r is not None]
    deadheads = [m.miles_from(center[0], center[1]) for m in members] if center else []
    deadheads = [d for d in deadheads if d is not None]
    units = len(members) if kind == "load" else max(_to_int(first.load.get("det_cnt")) or 1, 1)
    return Suggestion(
        kind=kind,
        load_id=load_id,
        units=units,
        origin=(first.origin.city_state if first.origin and first.origin.city_state else first.text("origin")),
        origin_zip=first.load.get("orig_zip") or (first.origin.zip if first.origin else None),
        deadhead_miles=round(min(deadheads), 1) if deadheads else None,
        destinations=dests.most_common(),
        dest_states=sorted({m.load.get("dest_state") for m in members if m.load.get("dest_state")}),
        road_miles=max(miles) if miles else None,
        models=models.most_common(),
        price=round(sum(priced), 2) if priced and len(priced) == len(members) else None,
        age_days=min(ages) if ages else None,
        member_ids=[m.load_id or "?" for m in members],
    )


def group_candidates(grid: Grid, exclude_ids: set[str] | None = None) -> tuple[list[Suggestion], list[Suggestion]]:
    """Split the grid into built loads (members grouped by parent) and single orders.

    Rows with a ``carrier_id`` already set, and any ids in ``exclude_ids`` (the dispatched
    load and its bundle), are dropped.
    """

    exclude_ids = {str(i) for i in (exclude_ids or set())}
    center = (grid.radius_center.lat, grid.radius_center.lon) if grid.radius_center else None
    bundles: dict[str, list[GridRow]] = collections.OrderedDict()
    singles: list[GridRow] = []
    for row in grid.rows:
        if row.load.get("carrier_id"):
            continue
        if row.load_id in exclude_ids:
            continue
        if row.link_class == "LinkBundleChild" or row.load.get("type") == "BundleChild":
            cell = row.cells.get("bundle")
            parent = cell.load_links[0] if cell and cell.load_links else (cell.text if cell else None)
            parent = parent or f"bundle-of-{row.load_id}"
            if parent in exclude_ids:
                continue
            bundles.setdefault(parent, []).append(row)
        else:
            singles.append(row)
    loads = [_suggestion("load", parent, members, center) for parent, members in bundles.items()]
    orders = [_suggestion("order", row.load_id or "?", [row], center) for row in singles]
    return loads, orders


def score_suggestion(s: Suggestion, home_state: str | None, back_state: str | None) -> Suggestion:
    if home_state and home_state in s.dest_states:
        s.direction = "home"
    elif back_state and back_state in s.dest_states:
        s.direction = "back"
    else:
        s.direction = "other"
    score = (s.deadhead_miles if s.deadhead_miles is not None else 999.0) + DIRECTION_PENALTY[s.direction]
    if s.age_days is not None:
        score -= min(s.age_days, AGE_BONUS_CAP_DAYS) * AGE_BONUS_PER_DAY
    if s.price:
        score -= PRICED_BONUS
    s.score = round(score, 1)
    return s


def pick_suggestions(
    loads: list[Suggestion],
    orders: list[Suggestion],
    *,
    home_state: str | None = None,
    back_state: str | None = None,
    min_n: int = DEFAULT_MIN,
    max_n: int = DEFAULT_MAX,
) -> list[Suggestion]:
    """Loads first, nearest first; single orders only to top the list up to ``min_n`` or
    when there are no loads at all. Never more than ``max_n``."""

    ranked_loads = sorted((score_suggestion(s, home_state, back_state) for s in loads), key=lambda s: (s.score, s.load_id))
    ranked_orders = sorted((score_suggestion(s, home_state, back_state) for s in orders), key=lambda s: (s.score, s.load_id))
    picks = ranked_loads[:max_n]
    if len(picks) < min_n:
        picks += ranked_orders[: max(min_n - len(picks), 0)]
    return picks[:max_n]


# --------------------------------------------------------------------------- #
# The draft email                                                              #
# --------------------------------------------------------------------------- #


def render_email(
    load: DispatchedLoad,
    picks: list[Suggestion],
    *,
    miles: int = DEFAULT_MILES,
    show_pay: bool = False,
    rep_email: str | None = None,
    delivered_at: str | None = None,
    trip_loads: list[str] | None = None,
    rep_signature: dict[str, str] | None = None,
) -> dict[str, str]:
    """The draft. With ``delivered_at`` the truck has already dropped the load (the
    watcher's trigger) and the intro says so; without it the load is merely dispatched.
    The contact and the signature are the load's sales rep; ``rep_signature`` may add
    ``title`` and ``email`` for that rep (from reps.csv). No phone numbers. A load with no
    sales rep renders with a visible placeholder and ``needs_sales_rep`` set, and must not
    be sent."""

    dest = load.destination
    where = dest.city_state or (dest.zip or "the delivery point")
    place = f"{where} {dest.zip}" if dest.zip and dest.city else where
    subject = f"Next loads near {where} after load {load.load_id}"
    greeting = load.carrier.contact_name or load.carrier.name or "there"
    units = f"{load.units} unit{'s' if load.units != 1 else ''}" if load.units else "your load"
    vehicles = f" ({load.vehicle_summary()})" if load.vehicles else ""
    others = [l for l in (trip_loads or []) if l != load.load_id]
    if delivered_at:
        day = delivered_at.split(" ")[0]
        if others:
            intro = f"You dropped loads {', '.join(others)} and {load.load_id}{vehicles}, the last one in {place} on {day}."
        else:
            intro = f"You dropped load {load.load_id}{vehicles} in {place} on {day}."
    else:
        when = f" on {dest.when}" if dest.when else ""
        intro = f"Load {load.load_id}{vehicles} delivers {units} to {place}{when}."
    if len(picks) == 1:
        offer = f"Here is a Circle load leaving within {miles} miles of there that your truck could take next:"
    else:
        offer = f"Here are {len(picks)} Circle loads leaving within {miles} miles of there that your truck could take next:"
    lines = [
        f"Hi {greeting},",
        "",
        intro,
        offer,
        "",
    ]
    for i, s in enumerate(picks, 1):
        what = "Load" if s.kind == "load" else "Order"
        deadhead = f"{s.deadhead_miles:.0f} mi from {where}" if s.deadhead_miles is not None else "near the delivery"
        road = f", about {s.road_miles} road mi" if s.road_miles else ""
        if show_pay and s.price:
            pay = f" - ${s.price:,.2f}"
        else:
            pay = " - call for rate"
        lines.append(f"{i}. {what} {s.load_id}: {s.units} unit{'s' if s.units != 1 else ''} ({s.model_summary()}) from {s.origin} ({deadhead}) to {s.dest_summary()}{road}{pay}")
    rep = load.contact_rep_display or "[SALES REP NOT ASSIGNED]"
    sig = rep_signature or {}
    title = sig.get("title") or "Sales Rep"
    lines += [
        "",
        f"Reply to this email or call {rep} to book any of them.",
        "",
        rep,
        f"{title}, Circle Logistics",
    ]
    if sig.get("email"):
        lines.append(sig["email"])
    cc_names = ", ".join(nice_name(n) for n in load.cc_reps) or "no sales rep on the load"
    return {
        "to": load.carrier.email or "<carrier dispatcher email: not on the load page>",
        "cc": rep_email or f"<{cc_names}: email from reps.csv>",
        "subject": subject,
        "body": "\n".join(lines),
        "needs_sales_rep": not load.sales_rep,
    }


# --------------------------------------------------------------------------- #
# Orchestration + CLI                                                          #
# --------------------------------------------------------------------------- #


def suggest(
    load: DispatchedLoad,
    grid: Grid,
    *,
    min_n: int = DEFAULT_MIN,
    max_n: int = DEFAULT_MAX,
) -> dict[str, Any]:
    exclude = {load.load_id}
    loads, orders = group_candidates(grid, exclude)
    picks = pick_suggestions(
        loads,
        orders,
        home_state=load.carrier.state,
        back_state=load.origin.state,
        min_n=min_n,
        max_n=max_n,
    )
    return {
        "dispatched": asdict(load),
        "search": {
            "title": grid.title,
            "rows": len(grid.rows),
            "radius_center": asdict(grid.radius_center) if grid.radius_center else None,
            "built_loads": len(loads),
            "single_orders": len(orders),
        },
        "picks": [asdict(p) for p in picks],
        "picks_objects": picks,
    }


def _fill_city_from_center(load: DispatchedLoad, grid: Grid) -> None:
    if not load.destination.city and grid.radius_center:
        match = re.match(r"^\d{5}\s+(.*?),\s*([A-Z]{2})$", grid.radius_center.label)  # "60477 ORLAND HILLS, IL"
        if match:
            load.destination.city, load.destination.state = match.group(1).title(), match.group(2)


def email_text(email: dict[str, Any]) -> str:
    return f"To: {email['to']}\nCc: {email['cc']}\nSubject: {email['subject']}\n\n{email['body']}\n"


def draft_for_load(
    session: CargoTelSession,
    load_id: str,
    *,
    miles: int = DEFAULT_MILES,
    saved_id: str = DEFAULT_SAVED_ID,
    min_n: int = DEFAULT_MIN,
    max_n: int = DEFAULT_MAX,
    show_pay: bool = False,
    rep_email: str | None = None,
    save_html_dir: str | None = None,
    delivered_at: str | None = None,
    trip_loads: list[str] | None = None,
    require_esign: bool = False,
) -> dict[str, Any]:
    """The whole pipeline for one load, live: load page, radius search, picks, draft.
    ``delivered_at`` (and the other ``trip_loads`` the same truck dropped) switch the
    email to the "you dropped" wording. ``require_esign`` refuses a load whose page has no
    e-signed carrier agreement (:class:`NotAcceptedError`). Returns the structured result
    with ``email`` and ``picks_objects``. Raises on a stale session or a load without a
    destination zip, so a caller never records a half-done event as handled."""

    load, load_html = fetch_dispatched_load(session, load_id)
    if require_esign and not load.esigned:
        raise NotAcceptedError(f"load {load_id}: no e-signed carrier agreement on the page yet")
    if save_html_dir:
        os.makedirs(save_html_dir, exist_ok=True)
        with open(os.path.join(save_html_dir, f"load_{load_id}.html"), "w", encoding="utf-8") as handle:
            handle.write(load_html)
    if not load.destination.zip:
        raise RuntimeError(f"load {load_id}: no destination zip on the page")
    spec = SearchParams(saved_id, radius_origin=load.destination.zip, radius_miles=miles, per_page="All")
    status, html, params = session.fetch_search_html(spec)
    if save_html_dir:
        with open(os.path.join(save_html_dir, f"search_{saved_id}_{load.destination.zip}_{miles}.html"), "w", encoding="utf-8") as handle:
            handle.write(html)
    grid = parse_grid(html, status)
    if grid.signin_page:
        raise RuntimeError("CargoTel answered with the sign-in page; the session cookie is stale")
    _fill_city_from_center(load, grid)
    result = suggest(load, grid, min_n=min_n, max_n=max_n)
    picks = result["picks_objects"]
    result["email"] = render_email(load, picks, miles=miles, show_pay=show_pay, rep_email=rep_email,
                                   delivered_at=delivered_at, trip_loads=trip_loads)
    result["load"] = load
    result["search"]["params"] = params
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draft a reload-suggestion email for a dispatched CargoTel load (never sends).")
    parser.add_argument("load_id", nargs="?", help="the dispatched 6-digit load id")
    parser.add_argument("--zip", help="demo mode: search around this zip instead of reading a load page")
    parser.add_argument("--miles", type=int, default=DEFAULT_MILES, choices=(10, 25, 50, 75, 150, 300))
    parser.add_argument("--saved-id", default=DEFAULT_SAVED_ID, help=f"Load Build saved search (default {DEFAULT_SAVED_ID})")
    parser.add_argument("--min", dest="min_n", type=int, default=DEFAULT_MIN)
    parser.add_argument("--max", dest="max_n", type=int, default=DEFAULT_MAX)
    parser.add_argument("--show-pay", action="store_true", help="put the load price in the email instead of 'call for rate'")
    parser.add_argument("--rep-email", help="Cc address for the Circle dispatch rep")
    parser.add_argument("--delivered", metavar="WHEN", help='the load was already dropped at this time, e.g. "10/12/2026 09:17"; switches the wording')
    parser.add_argument("--from-load-html", metavar="FILE", help="offline: parse this saved load page")
    parser.add_argument("--from-search-html", metavar="FILE", help="offline: parse this saved search page")
    parser.add_argument("--save-html", metavar="DIR", help="archive the fetched pages here")
    parser.add_argument("--out", metavar="FILE", help="write the draft email text")
    parser.add_argument("--json", metavar="FILE", help="write the structured result")
    args = parser.parse_args(argv)

    session: CargoTelSession | None = None
    if args.from_load_html:
        with open(args.from_load_html, encoding="utf-8", errors="ignore") as handle:
            load = parse_load_page(handle.read(), args.load_id)
    elif args.zip:
        load = DispatchedLoad(load_id=args.load_id or "demo", destination=Stop(zip=args.zip))
    elif args.load_id:
        session = CargoTelSession()
        load, load_html = fetch_dispatched_load(session, args.load_id)
        if args.save_html:
            os.makedirs(args.save_html, exist_ok=True)
            with open(os.path.join(args.save_html, f"load_{args.load_id}.html"), "w", encoding="utf-8") as handle:
                handle.write(load_html)
    else:
        parser.error("give a load id, or --zip, or --from-load-html")

    dest_zip = load.destination.zip
    if not dest_zip:
        print(f"load {load.load_id}: no destination zip found on the page; cannot search", file=sys.stderr)
        return 2

    if args.from_search_html:
        with open(args.from_search_html, encoding="utf-8", errors="ignore") as handle:
            grid = parse_grid(handle.read())
    else:
        session = session or CargoTelSession()
        spec = SearchParams(args.saved_id, radius_origin=dest_zip, radius_miles=args.miles, per_page="All")
        status, html, params = session.fetch_search_html(spec)
        print(f"GET Search.mcgi {params} -> HTTP {status}, {len(html)} chars", file=sys.stderr)
        if args.save_html:
            os.makedirs(args.save_html, exist_ok=True)
            with open(os.path.join(args.save_html, f"search_{args.saved_id}_{dest_zip}_{args.miles}.html"), "w", encoding="utf-8") as handle:
                handle.write(html)
        grid = parse_grid(html, status)
        if grid.signin_page:
            print("CargoTel answered with the sign-in page; the session cookie is stale", file=sys.stderr)
            return 2

    _fill_city_from_center(load, grid)

    result = suggest(load, grid, min_n=args.min_n, max_n=args.max_n)
    picks = result.pop("picks_objects")
    email = render_email(load, picks, miles=args.miles, show_pay=args.show_pay, rep_email=args.rep_email, delivered_at=args.delivered)
    result["email"] = email

    d = load.destination
    print(f"load {load.load_id}: {load.origin.city_state or '?'} -> {d.city_state or '?'} {d.zip or ''} {d.when or ''}".rstrip())
    carrier = load.carrier
    base = ", ".join(p for p in (carrier.city, carrier.state) if p)
    print(f"  carrier: {carrier.name or '(none assigned)'}" + (f" (id {carrier.client_id}{', ' + base if base else ''}; contact {carrier.contact_name or '?'})" if carrier.client_id else ""))
    vehicles = f" ({load.vehicle_summary()})" if load.vehicles else ""
    sales = ", ".join(nice_name(n) for n in load.cc_reps) or "(none)"
    print(f"  dispatch rep: {load.rep_display or '(unassigned)'}; sales rep: {sales}; units: {load.units}{vehicles}")
    s = result["search"]
    print(f"  search: {s['rows']} rows within {args.miles} mi of {dest_zip}; {s['built_loads']} built loads, {s['single_orders']} single orders; picked {len(picks)}")
    for i, p in enumerate(picks, 1):
        print(f"   {i:>2}. {p.kind:5s} {p.load_id} {p.units}u {p.origin} -> {p.dest_summary()} deadhead {p.deadhead_miles} road {p.road_miles} dir {p.direction} score {p.score}")
    print()
    print(email_text(email), end="")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(email_text(email))
        print(f"wrote {args.out}", file=sys.stderr)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(result, handle, indent=2, default=str)
        print(f"wrote {args.json}", file=sys.stderr)
    return 0 if picks else 1


if __name__ == "__main__":
    sys.exit(main())
