"""
Sales rep sync: give every unassigned load the Default Primary Rep of its Bill To customer.

CargoTel keeps a "Default Primary Rep" on each customer (Administrative Functions > Client
Management > Customer Admin > Customer List) but does not copy it onto the loads, so loads
arrive with no Sales Rep 1 and every job here that emails or Cc's the sales rep holds them.
This job closes the gap:

1. Pull the Customer List (``ClientSearch.mcgi`` saved view 65, all rows): client id, name,
   key and Default Primary Rep.
2. Pull Bot Pipelines > Sales Rep Edit (``Search.mcgi`` saved search 2080: Sales Rep =
   Unassigned, carrier added in the last 90 days, pickup ETA set, Ford and Mercedes left
   out, grouped by customer) with the Bill To and Customer id columns added on the GET.
3. For every load whose Bill To id is on the Customer List and whose customer has a
   Default Primary Rep that exists in the screen's "Primary Sales Rep (Bulk)" dropdown,
   plan a change. A load is skipped, and listed, when its Bill To is not on the list, the
   customer has no default rep, the rep is not in the dropdown, or the load already
   carries a different rep (``--overwrite`` changes those too).
4. With ``--apply``, press the screen's own bulk widget: one POST per rep, the whole
   ``Menu_alt`` form exactly as the browser sends it, with the boxes of that rep's loads
   ticked and the rep chosen in the dropdown. The re-rendered screen that comes back is
   the check: a load that has left the unassigned-only screen, or shows the rep in
   Sales Rep 1, is verified; anything else is reported as failed and retried next run.
   When the last loads are assigned the screen comes back as a bare "No Results Found"
   page (no grid, no form): that counts as every posted load having left the screen.

Without ``--apply`` nothing is written: the plan goes to the console and to ``outbox/``.

Usage::

    python sales_rep_sync/sales_rep_sync.py --list                    # what would change
    python sales_rep_sync/sales_rep_sync.py                           # dry run, report to outbox/
    python sales_rep_sync/sales_rep_sync.py --apply --only-load 354657  # one load, for real
    python sales_rep_sync/sales_rep_sync.py --apply                   # the whole screen
    python sales_rep_sync/sales_rep_sync.py --from-html screen.html --customers-html list.html --list
"""

from __future__ import annotations

import argparse
import collections
import datetime as dt
import html as html_lib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from cargotel_search import (  # noqa: E402
    _BROWSER_HEADERS,
    BASE_URL,
    CargoTelSession,
    Grid,
    GridRow,
    SearchParams,
    is_signin_page,
    parse_grid,
)

CUSTOMER_LIST_ID = "65"
CUSTOMER_LIST_URL = f"{BASE_URL}/backoffice/ClientSearch.mcgi"
CUSTOMER_LIST_PATH = "Administrative Functions > Client Management > Customer Admin > Customer List"
SCREEN_ID = "2080"
SCREEN_URL = f"{BASE_URL}/backoffice/Search.mcgi"
SCREEN_PATH = "Bot Pipelines > Sales Rep Edit"
LOAD_URL = f"{BASE_URL}/backoffice/loadmaint.mcgi?load_id="

#: Columns added on the GET; CargoTel puts them first and appends the saved view's own.
EXTRA_VIEW_FIELDS = ("bill_id", "billtoname", "cons_id", "consignor", "original_consignor")
#: Columns the screen must carry (the saved search's own).
REQUIRED_COLUMNS = ("sales_rep_bulk", "sales_rep1")
#: The bulk widget: one dropdown for the form, one checkbox per row named ``sales_rep_bulk//<order id>``.
BULK_SELECT = "Control__sales_rep_bulk__sales_rep"
BULK_BOX = "sales_rep_bulk//"
UNASSIGN = "[NONE]"
DEFAULT_MAX_CHANGES = 200

SET = "set"
SKIP = "skip"
DRY_RUN = "dry-run"
VERIFIED = "verified"
FAILED = "failed"

_SELECT_RE = re.compile(r'<select\b[^>]*\bname="' + re.escape(BULK_SELECT) + r'"[^>]*>(.*?)</select>', re.S | re.I)
_OPTION_RE = re.compile(r"<option\b([^>]*)>([^<]*)", re.I)
_BOX_RE = re.compile(r'<input\b[^>]*\bname="' + re.escape(BULK_BOX) + r'(\d+)"[^>]*>', re.I)
_FORM_START_RE = re.compile(r'<form\s+name="Menu_alt"', re.I)
_FORM_END_RE = re.compile(r"</form\s*>", re.I)
_SERVER_ERROR_RE = re.compile(r"An Error was encountered:(.{0,300})", re.S)
_NO_RESULTS_RE = re.compile(r"""<div\s+id=['"]no_results_found['"]\s*>""", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


def log(message: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {message}", file=sys.stderr, flush=True)


def norm_name(text: str | None) -> str:
    """"CROMWELL, JUSTICE", "Justice Cromwell" and "JUSTICE  CROMWELL" all become
    "JUSTICE CROMWELL", so the Customer List's LAST, FIRST matches the dropdown's labels."""

    text = html_lib.unescape(text or "")
    if "," in text:
        last, first = text.split(",", 1)
        text = f"{first} {last}"
    return " ".join(re.sub(r"[.\s]+", " ", text).upper().split())


def _clean(text: str | None) -> str:
    text = " ".join(html_lib.unescape(text or "").split())
    return "" if text.upper() in ("", "N/A", "NA", "-") else text


# --------------------------------------------------------------------------- #
# The Customer List                                                            #
# --------------------------------------------------------------------------- #


@dataclass
class Customer:
    id: str
    name: str
    key: str = ""
    #: As the list shows it ("LINK, KYLE"); empty when the customer has none.
    default_rep: str = ""


def parse_customers(html: str, status: int | None = None) -> dict[str, Customer]:
    """``{client id: Customer}`` from the Customer List grid."""

    grid = parse_grid(html, status)
    if grid.signin_page:
        raise RuntimeError("CargoTel answered the Customer List with the sign-in page; the S3 cookie is stale")
    keys = {c.key for c in grid.columns}
    missing = [k for k in ("Id", "Client", "defaultPrimaryrep") if k not in keys]
    if missing:
        raise RuntimeError(
            f"the Customer List is missing column(s) {', '.join(missing)}; was the saved view "
            f"({CUSTOMER_LIST_PATH}) changed?"
        )
    if grid.is_partial:
        raise RuntimeError(
            f"the Customer List came back as rows {grid.row_from}-{grid.row_to} of {grid.total_rows}; "
            "the job asks for all rows (Menu__perPage=All) and needs them"
        )
    out: dict[str, Customer] = {}
    for row in grid.rows:
        cid = row.text("Id").strip()
        if not cid.isdigit():
            continue
        out[cid] = Customer(cid, _clean(row.text("Client")), _clean(row.text("Key")), _clean(row.text("defaultPrimaryrep")))
    return out


def fetch_customers_html(session: CargoTelSession) -> str:
    params = {"expand_saved": CUSTOMER_LIST_ID, "expand_saved_type": "priv", "Menu__perPage": "All"}
    status, html = session.get(CUSTOMER_LIST_URL, params)
    if is_signin_page(html, status):
        session.cookie(refresh=True)
        status, html = session.get(CUSTOMER_LIST_URL, params)
    if status != 200:
        raise RuntimeError(f"ClientSearch.mcgi returned HTTP {status} for the Customer List")
    return html


# --------------------------------------------------------------------------- #
# The Sales Rep Edit screen                                                    #
# --------------------------------------------------------------------------- #


@dataclass
class ScreenLoad:
    load_id: str
    bill_id: str
    cons_id: str
    #: The best name on the row: Bill To, else Original Consignor, else Customer, else the section.
    customer: str
    #: Sales Rep 1 as shown ("" on the unassigned screen).
    sales_rep: str = ""
    carrier: str = ""
    origin: str = ""
    destination: str = ""
    pickup_eta: str = ""
    section: str = ""


def load_from_row(row: GridRow) -> ScreenLoad | None:
    if not row.load_id:
        return None
    section = re.sub(r"^\s*Customer:\s*", "", row.section or "").strip()
    customer = (_clean(row.text("billtoname")) or _clean(row.text("original_consignor"))
                or _clean(row.text("consignor")) or section)
    return ScreenLoad(
        load_id=row.load_id,
        bill_id=row.text("bill_id").strip(),
        cons_id=row.text("cons_id").strip(),
        customer=customer,
        sales_rep=_clean(row.text("sales_rep1")),
        carrier=_clean(row.text("carriername")),
        origin=_clean(row.text("originCityState")),
        destination=_clean(row.text("destination")),
        pickup_eta=_clean(row.text("pickiup_eta_datetime")),
        section=section,
    )


def is_empty_screen(html: str) -> bool:
    """A search with no rows: CargoTel renders a bare ``No Results Found`` page with no grid,
    no ``Menu_alt`` form and no bulk dropdown. The POST that assigns the last loads on the
    screen comes back like this (seen live 2026-11-09 15:03 ET)."""

    return bool(_NO_RESULTS_RE.search(html))


def screen_problem(html: str, status: int | None = None) -> str | None:
    """Why a page is not the Sales Rep Edit screen (None when it is, empty or not)."""

    if is_signin_page(html, status):
        return "sign-in page (the S3 cookie is stale)"
    if status is not None and status != 200:
        return f"HTTP {status}"
    error = _SERVER_ERROR_RE.search(html)
    if error:
        return "CargoTel reported: " + " ".join(_TAG_RE.sub(" ", error.group(1)).split())
    if is_empty_screen(html):
        return None
    if not _FORM_START_RE.search(html):
        return "no Menu_alt form on the page"
    if not _SELECT_RE.search(html):
        return f'no "Primary Sales Rep (Bulk)" dropdown on the page; add the column to {SCREEN_PATH}'
    return None


def parse_screen(html: str, status: int | None = None) -> tuple[Grid, list[ScreenLoad]]:
    problem = screen_problem(html, status)
    if problem:
        raise RuntimeError(f"not the Sales Rep Edit screen: {problem}")
    grid = parse_grid(html, status)
    if not grid.rows and (grid.no_results or is_empty_screen(html) or not grid.columns):
        return grid, []
    keys = {c.key for c in grid.columns}
    missing = [k for k in ("bill_id",) + REQUIRED_COLUMNS if k not in keys]
    if missing:
        raise RuntimeError(f"the screen is missing column(s) {', '.join(missing)}; was {SCREEN_PATH} changed?")
    loads = [load for load in (load_from_row(row) for row in grid.rows) if load is not None]
    return grid, loads


def parse_rep_options(html: str) -> dict[str, str]:
    """The "Primary Sales Rep (Bulk)" dropdown as ``{normalised name: CargoTel user id}``,
    without the Unassign entry."""

    match = _SELECT_RE.search(html)
    if not match:
        raise RuntimeError(f'no "Primary Sales Rep (Bulk)" dropdown on the screen; add the column to {SCREEN_PATH}')
    out: dict[str, str] = {}
    for attrs, label in _OPTION_RE.findall(match.group(1)):
        value = re.search(r'value="([^"]*)"', attrs)
        rep_id = html_lib.unescape(value.group(1)) if value else ""
        name = norm_name(label)
        if rep_id and rep_id != UNASSIGN and name:
            out[name] = rep_id
    return out


def menu_alt_form(page_html: str) -> str:
    match = _FORM_START_RE.search(page_html)
    if not match:
        raise RuntimeError("no Menu_alt form on the screen; cannot press the bulk widget")
    end = _FORM_END_RE.search(page_html, match.start())
    return page_html[match.start(): end.start() if end else len(page_html)]


def parse_boxes(form_html: str) -> set[str]:
    """The order ids that have a "Primary Sales Rep (Bulk)" checkbox on the screen."""

    return set(_BOX_RE.findall(form_html))


def fetch_screen_html(session: CargoTelSession) -> str:
    spec = SearchParams(SCREEN_ID, per_page="All", extra={"Menu__ViewFields": list(EXTRA_VIEW_FIELDS)})  # type: ignore[dict-item]
    status, html, params = session.fetch_search_html(spec)
    problem = screen_problem(html, status)
    if problem:
        raise RuntimeError(f"GET Search.mcgi {params}: {problem}")
    return html


# --------------------------------------------------------------------------- #
# The plan                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class Decision:
    load: ScreenLoad
    action: str                        # SET or SKIP
    reason: str = ""                   # why skipped, or how an unusual match was made
    customer: Customer | None = None
    rep_name: str = ""                 # the dropdown label, e.g. "KYLE LINK"
    rep_id: str = ""
    result: str = ""                   # DRY_RUN, VERIFIED or FAILED once run
    note: str = ""                     # what the check saw

    def record(self) -> dict[str, Any]:
        load = self.load
        return {
            "load_id": load.load_id, "bill_id": load.bill_id, "cons_id": load.cons_id, "customer": load.customer,
            "customer_list_name": self.customer.name if self.customer else "",
            "default_rep": self.customer.default_rep if self.customer else "",
            "current_rep": load.sales_rep, "rep_name": self.rep_name, "rep_id": self.rep_id,
            "action": self.action, "result": self.result, "reason": self.reason, "note": self.note,
            "carrier": load.carrier, "origin": load.origin, "destination": load.destination,
            "pickup_eta": load.pickup_eta,
        }


def plan(loads: list[ScreenLoad], customers: dict[str, Customer], options: dict[str, str], *,
         overwrite: bool = False, boxes: set[str] | None = None) -> list[Decision]:
    """One decision per load on the screen, in screen order."""

    out: list[Decision] = []
    for load in loads:
        customer = customers.get(load.bill_id)
        how = ""
        if customer is None and load.cons_id and load.cons_id != load.bill_id:
            customer = customers.get(load.cons_id)
            how = f"matched by customer id {load.cons_id} (bill to {load.bill_id or '?'} is not on the Customer List)"
        if customer is None:
            out.append(Decision(load, SKIP, reason=f"bill to {load.bill_id or '?'} ({load.customer}) is not on the Customer List"))
            continue
        if not customer.default_rep:
            out.append(Decision(load, SKIP, customer=customer, reason=f"{customer.name} has no Default Primary Rep on the Customer List"))
            continue
        want = norm_name(customer.default_rep)
        rep_id = options.get(want)
        if not rep_id:
            out.append(Decision(load, SKIP, customer=customer, rep_name=want,
                                reason=f'default rep "{customer.default_rep}" of {customer.name} is not in the Primary Sales Rep dropdown'))
            continue
        current = norm_name(load.sales_rep)
        if current == want:
            out.append(Decision(load, SKIP, customer=customer, rep_name=want, rep_id=rep_id, reason="already set"))
            continue
        if current and not overwrite:
            out.append(Decision(load, SKIP, customer=customer, rep_name=want, rep_id=rep_id,
                                reason=f'Sales Rep 1 is "{load.sales_rep}"; not changed without --overwrite'))
            continue
        if boxes is not None and load.load_id not in boxes:
            out.append(Decision(load, SKIP, customer=customer, rep_name=want, rep_id=rep_id,
                                reason="no Primary Sales Rep (Bulk) checkbox on the row"))
            continue
        out.append(Decision(load, SET, customer=customer, rep_name=want, rep_id=rep_id, reason=how))
    return out


def _matches(needle: str, *haystack: str) -> bool:
    needle = norm_name(needle)
    return any(needle == norm_name(h) or (needle and needle in norm_name(h)) for h in haystack if h)


def select(decisions: list[Decision], *, only_load: list[str] | None = None, only_rep: str | None = None,
           only_customer: str | None = None, max_changes: int = DEFAULT_MAX_CHANGES) -> list[Decision]:
    """Turn the planned changes outside the ``--only-*`` filters, and beyond ``max_changes``,
    into skips (so the report still lists them)."""

    kept = 0
    for decision in decisions:
        if decision.action != SET:
            continue
        load = decision.load
        if only_load and load.load_id not in only_load:
            decision.action, decision.reason = SKIP, "left out by --only-load"
        elif only_rep and not _matches(only_rep, decision.rep_name, decision.customer.default_rep if decision.customer else ""):
            decision.action, decision.reason = SKIP, f"left out by --only-rep {only_rep}"
        elif only_customer and not (load.bill_id == only_customer or load.cons_id == only_customer
                                    or _matches(only_customer, load.customer, decision.customer.name if decision.customer else "")):
            decision.action, decision.reason = SKIP, f"left out by --only-customer {only_customer}"
        elif max_changes >= 0 and kept >= max_changes:
            decision.action, decision.reason = SKIP, f"over --max-changes {max_changes}; next run"
        else:
            kept += 1
    return decisions


def groups(decisions: list[Decision]) -> list[tuple[str, str, list[Decision]]]:
    """The planned changes by rep: ``(rep name, rep id, decisions)``, reps in name order."""

    by_rep: dict[tuple[str, str], list[Decision]] = collections.OrderedDict()
    for decision in sorted((d for d in decisions if d.action == SET), key=lambda d: (d.rep_name, d.load.load_id)):
        by_rep.setdefault((decision.rep_name, decision.rep_id), []).append(decision)
    return [(name, rep_id, members) for (name, rep_id), members in by_rep.items()]


# --------------------------------------------------------------------------- #
# The bulk POST                                                                #
# --------------------------------------------------------------------------- #

# form_fields and session_post mirror tracking_update_alerts (the dispatch-comment write,
# verified live on 2026-11-06): CargoTel only honours the widget when the WHOLE Menu_alt
# form is posted the way a browser would. Copied rather than imported so this job does not
# drag the alerting jobs' import chain along.

_FIELD_RE = re.compile(r"<(input|select|textarea)\b([^>]*)>(?:(.*?)</\1\s*>)?", re.I | re.S)
_ANY_OPTION_RE = re.compile(r"<option\b([^>]*)>", re.I)
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
            options = _ANY_OPTION_RE.findall(body)
            chosen = [o for o in options if re.search(r"\bselected\b", o, re.I)]
            if chosen:
                out.extend((name, _attr(o, "value") or "") for o in chosen)
            elif options and not re.search(r"\bmultiple\b", attrs, re.I):
                out.append((name, _attr(options[0], "value") or ""))
        else:  # textarea
            out.append((name, html_lib.unescape(_TAG_RE.sub("", body))))
    return out


def build_bulk_post(page_html: str, rep_id: str, load_ids: list[str]) -> list[tuple[str, str]]:
    """The POST body that gives ``load_ids`` the primary sales rep ``rep_id`` through the
    screen's own widget: the ``Menu_alt`` form of the fetched page, every field as the
    browser would send it, with those loads' boxes ticked, the rep chosen in the dropdown
    and the Submit button pressed. Raises when a load has no box on the page."""

    if not rep_id or rep_id == UNASSIGN:
        raise ValueError("refusing to post without a rep (the Unassign entry is never chosen)")
    if not load_ids:
        raise ValueError("no loads to change")
    form = menu_alt_form(page_html)
    wanted = set(load_ids)
    missing = sorted(wanted - parse_boxes(form))
    if missing:
        raise RuntimeError("no Primary Sales Rep (Bulk) box on the screen for load(s) " + ", ".join(missing))

    def tick(match: re.Match[str]) -> str:
        tag = match.group(0)
        if match.group(1) in wanted and not re.search(r"\bchecked\b", tag, re.I):
            tag = re.sub(r"\s*/?>$", " checked>", tag)
        return tag

    form = _BOX_RE.sub(tick, form)
    data: list[tuple[str, str]] = []
    select_seen = False
    for name, value in form_fields(form):
        if name == "Menu__is_submitted":
            continue
        if name == BULK_SELECT:
            value, select_seen = rep_id, True
        data.append((name, value))
    if not select_seen:
        raise RuntimeError("the Menu_alt form has no Primary Sales Rep dropdown")
    ticked = [v for n, v in data if n.startswith(BULK_BOX)]
    if sorted(ticked) != sorted(wanted):
        raise RuntimeError(f"ticked {sorted(ticked)} but wanted {sorted(wanted)}")
    data.append(("Menu__is_submitted", "Submit"))
    return data


def session_post(session: CargoTelSession, url: str, data: list[tuple[str, str]], timeout: float = 300.0) -> tuple[int, str]:
    """POST a form with the session cookie (``CargoTelSession`` only knows GET)."""

    import requests

    headers = {**_BROWSER_HEADERS, "cookie": f"cgt-browser-session={session.cookie()}", "origin": BASE_URL, "referer": url}
    response = requests.post(url, headers=headers, data=data, timeout=timeout)
    return response.status_code, response.text


def verify(grid_after: Grid, decisions: list[Decision]) -> None:
    """Mark each planned change against the screen CargoTel rendered after the POST: a
    load that has left the (unassigned-only) screen, or shows the rep in Sales Rep 1, is
    verified; a load still there with another rep (or none) failed."""

    rows = {row.load_id: row for row in grid_after.rows if row.load_id}
    for decision in decisions:
        if decision.action != SET:
            continue
        row = rows.get(decision.load.load_id)
        if row is None:
            decision.result, decision.note = VERIFIED, "left the unassigned screen"
        elif norm_name(row.text("sales_rep1")) == decision.rep_name:
            decision.result, decision.note = VERIFIED, "Sales Rep 1 shows the rep"
        else:
            decision.result = FAILED
            decision.note = f'still on the screen with Sales Rep 1 "{_clean(row.text("sales_rep1")) or "(none)"}"'


Poster = Callable[[list[tuple[str, str]]], tuple[int, str]]


def live_poster(session: CargoTelSession) -> Poster:
    return lambda data: session_post(session, SCREEN_URL, data)


def apply_changes(page_html: str, decisions: list[Decision], post: Poster, *, save_pages: str | None = None,
                  now: dt.datetime | None = None) -> str:
    """Press the widget once per rep, chaining each POST off the page the previous one
    rendered (fresh state tokens, like a browser). Stops at the first unusable response:
    the screen's state is then unknown and the next run starts over. Returns the last
    page CargoTel rendered."""

    now = now or dt.datetime.now()
    html = page_html
    grid, _ = parse_screen(page_html)
    for index, (rep_name, rep_id, members) in enumerate(groups(decisions), start=1):
        on_screen = {row.load_id for row in grid.rows if row.load_id}
        gone = [d for d in members if d.load.load_id not in on_screen]
        if gone:
            # Someone else assigned or moved these since the page was fetched: not ours to claim.
            for d in gone:
                d.action, d.reason = SKIP, "left the unassigned screen before this rep's POST"
            log(f"  {rep_name}: {len(gone)} load(s) already off the screen: {', '.join(d.load.load_id for d in gone)}")
            members = [d for d in members if d.load.load_id in on_screen]
            if not members:
                continue
        ids = [d.load.load_id for d in members]
        try:
            data = build_bulk_post(html, rep_id, ids)
        except (RuntimeError, ValueError) as exc:
            for d in members:
                d.result, d.note = FAILED, str(exc)
            log(f"  {rep_name}: not posted: {exc}")
            continue
        log(f"POST {rep_name} ({rep_id}) on {len(ids)} load(s): {', '.join(ids)} ({len(data)} form fields)")
        status, body = post(data)
        if save_pages:
            os.makedirs(save_pages, exist_ok=True)
            path = os.path.join(save_pages, f"after_{now:%Y%m%d_%H%M}_{index}_{rep_id}.html")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
        problem = screen_problem(body, status)
        if problem:
            for d in members:
                d.result, d.note = FAILED, f"response was not the screen: {problem}"
            log(f"  response not usable ({problem}); stopping, the next run retries")
            break
        html = body
        grid, _ = parse_screen(body, status)
        verify(grid, members)
        done = sum(1 for d in members if d.result == VERIFIED)
        log(f"  {done} of {len(members)} verified; screen now {grid.summary()}")
    return html


# --------------------------------------------------------------------------- #
# One run                                                                      #
# --------------------------------------------------------------------------- #


def append_log(path: str | None, entry: dict[str, Any]) -> None:
    if not path:
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def summarize(decisions: list[Decision]) -> dict[str, Any]:
    skipped = collections.Counter(re.sub(r"\d{4,}", "N", d.reason) for d in decisions if d.action == SKIP)
    return {
        "loads": len(decisions),
        "set": sum(1 for d in decisions if d.action == SET),
        "verified": sum(1 for d in decisions if d.result == VERIFIED),
        "failed": sum(1 for d in decisions if d.result == FAILED),
        "skipped": sum(skipped.values()),
        "skip_reasons": dict(skipped.most_common()),
        "by_rep": {name: len(members) for name, _, members in groups(decisions)},
    }


def print_table(decisions: list[Decision]) -> None:
    print(f"{'load':>7s} {'bill to':>7s} {'customer':38s} {'current rep':18s} {'-> rep':18s} {'action':8s} {'result':9s} reason")
    for d in decisions:
        load = d.load
        print(f"{load.load_id:>7s} {load.bill_id:>7s} {load.customer[:38]:38s} {(load.sales_rep or '-')[:18]:18s} "
              f"{(d.rep_name or '-')[:18]:18s} {d.action:8s} {(d.result or '-'):9s} {d.reason or d.note}")


def run_once(
    *,
    session: CargoTelSession | None = None,
    now: dt.datetime | None = None,
    apply: bool = False,
    overwrite: bool = False,
    only_load: list[str] | None = None,
    only_rep: str | None = None,
    only_customer: str | None = None,
    max_changes: int = DEFAULT_MAX_CHANGES,
    outbox: str | None = None,
    reports: str | None = None,
    changes_log: str | None = None,
    save_pages: str | None = None,
    screen_html: str | None = None,
    customers_html: str | None = None,
    post: Poster | None = None,
    quiet: bool = False,
) -> dict[str, Any]:
    now = now or dt.datetime.now()
    if (screen_html is None or customers_html is None or (apply and post is None)) and session is None:
        session = CargoTelSession()

    if customers_html is None:
        customers_html = fetch_customers_html(session)  # type: ignore[arg-type]
    customers = parse_customers(customers_html)
    with_rep = sum(1 for c in customers.values() if c.default_rep)
    log(f"Customer List: {len(customers)} customers, {with_rep} with a Default Primary Rep")

    if screen_html is None:
        screen_html = fetch_screen_html(session)  # type: ignore[arg-type]
    if save_pages:
        os.makedirs(save_pages, exist_ok=True)
        for name, text in (("customers", customers_html), ("screen", screen_html)):
            with open(os.path.join(save_pages, f"{name}_{now:%Y%m%d_%H%M}.html"), "w", encoding="utf-8") as fh:
                fh.write(text)
    grid, loads = parse_screen(screen_html)
    if loads:
        options = parse_rep_options(screen_html)
        boxes = parse_boxes(menu_alt_form(screen_html))
    else:
        options, boxes = {}, set()   # the empty screen has neither the dropdown nor the form
    log(f"{SCREEN_PATH}: {grid.summary()}; {len(loads)} loads, {len(boxes)} with a bulk box; "
        f"dropdown: {', '.join(sorted(options)) or '-'}")
    if grid.is_partial:
        log(f"WARNING: the screen is paged ({grid.row_from}-{grid.row_to} of {grid.total_rows}); only this page is handled")

    decisions = plan(loads, customers, options, overwrite=overwrite, boxes=boxes)
    select(decisions, only_load=only_load, only_rep=only_rep, only_customer=only_customer, max_changes=max_changes)
    planned = groups(decisions)
    log(f"plan: {sum(len(m) for _, _, m in planned)} change(s) across {len(planned)} rep(s): "
        + "; ".join(f"{name} x{len(m)}" for name, _, m in planned) + f"; {sum(1 for d in decisions if d.action == SKIP)} skipped")

    if apply and planned:
        if post is None:
            post = live_poster(session)  # type: ignore[arg-type]
        apply_changes(screen_html, decisions, post, save_pages=save_pages, now=now)
        for d in decisions:
            if d.result in (VERIFIED, FAILED):
                append_log(changes_log, {"at": now.isoformat(timespec="seconds"), **d.record()})
    else:
        for d in decisions:
            if d.action == SET:
                d.result = DRY_RUN

    summary = summarize(decisions)
    summary.update({"at": now.isoformat(timespec="seconds"), "apply": bool(apply), "overwrite": bool(overwrite),
                    "customers": len(customers), "customers_with_rep": with_rep, "screen_rows": len(grid.rows)})
    folder = reports if apply else outbox
    if folder:
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, f"sales_rep_sync_{now:%Y-%m-%d_%H%M}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({**summary, "decisions": [d.record() for d in decisions]}, fh, indent=1, ensure_ascii=False)
        summary["report"] = path
    if not quiet:
        print_table(decisions)
    log(f"done: {summary['set']} planned, {summary['verified']} verified, {summary['failed']} failed, "
        f"{summary['skipped']} skipped" + ("" if apply else " (dry run, nothing written)"))
    return summary


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _ids(text: str | None) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="press the bulk widget in CargoTel (otherwise a dry run)")
    parser.add_argument("--overwrite", action="store_true",
                        help="also change loads whose Sales Rep 1 differs from the customer's default (default: fill empty only)")
    parser.add_argument("--list", action="store_true", help="print the plan only; no report file")
    parser.add_argument("--only-load", metavar="ID[,ID]", help="only these order ids")
    parser.add_argument("--only-rep", metavar="NAME", help="only loads going to this rep (name or part of it)")
    parser.add_argument("--only-customer", metavar="ID|NAME", help="only loads of this customer (id, name or part of it)")
    parser.add_argument("--max-changes", type=int, default=DEFAULT_MAX_CHANGES, metavar="N",
                        help=f"change at most N loads per run; the rest wait (default {DEFAULT_MAX_CHANGES}; -1 = no cap)")
    parser.add_argument("--outbox", default=os.path.join(HERE, "outbox"), help="dry-run reports (JSON)")
    parser.add_argument("--reports", default=os.path.join(HERE, "reports"), help="reports of runs with --apply (JSON)")
    parser.add_argument("--changes-log", default=os.path.join(HERE, "changes.jsonl"), help="one JSON line per load changed or failed")
    parser.add_argument("--save-pages", metavar="DIR", help="archive the fetched pages and every POST response")
    parser.add_argument("--from-html", metavar="FILE", help="offline: a saved Sales Rep Edit page instead of fetching")
    parser.add_argument("--customers-html", metavar="FILE", help="offline: a saved Customer List page instead of fetching")
    parser.add_argument("--now", metavar="YYYY-MM-DDTHH:MM", help="pretend it is this time (for offline runs)")
    args = parser.parse_args(argv)

    now = dt.datetime.strptime(args.now, "%Y-%m-%dT%H:%M") if args.now else dt.datetime.now()
    screen_html = customers_html = None
    if args.from_html:
        with open(args.from_html, encoding="utf-8") as fh:
            screen_html = fh.read()
    if args.customers_html:
        with open(args.customers_html, encoding="utf-8") as fh:
            customers_html = fh.read()
    if args.apply and args.from_html:
        print("--apply posts to the live screen; it cannot be combined with --from-html", file=sys.stderr)
        return 2

    summary = run_once(
        now=now, apply=args.apply, overwrite=args.overwrite, only_load=_ids(args.only_load) or None,
        only_rep=args.only_rep, only_customer=args.only_customer, max_changes=args.max_changes,
        outbox=None if args.list else args.outbox, reports=None if args.list else args.reports,
        changes_log=None if args.list else args.changes_log, save_pages=args.save_pages,
        screen_html=screen_html, customers_html=customers_html,
    )
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
