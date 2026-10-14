"""
Duplicate-order check: VINs that have more than one order on the same lane, emailed to
Justice Cromwell every Monday morning.

1. Pull CargoTel's ``Orders > Duplicate check`` saved search (id 1863: Stellantis orders in
   status Active / In-Route / Dispatched / On Hold / Delivered, "Delivered Date Or Not
   Delivered" = *Last 90 days*, grouped by VIN) with the VIN, date, carrier and rep
   columns added on the GET (``Menu__ViewFields``).
2. Narrow it to the last ``--days`` (default 7) the way the CargoTel dropdown would:
   keep an order when it is not delivered yet, or was delivered within the window.
3. Report every VIN that appears on two or more distinct order numbers with the same
   origin and the same destination.
4. Write the CSV and email the list (dry run to ``outbox/`` unless ``--send``).

The 90-day window is the saved search's own setting and the 7-day one is applied here,
locally. Neither is sent as a GET parameter: ``Menu__DateMne_DeliveredOr=Last 7 days`` on
the URL makes CargoTel die with ``Can't locate object method "hour" via package
"Last 7 days"`` (Listable/Filters.pm), which surfaces as a gzip ContentDecodingError. The
window CargoTel actually applied is read back from the form it echoes with the grid and
checked against ``--expect-window``; if someone edits the saved search the run stops
instead of reporting against the wrong data.

Match levels:

* ``same facility`` - origin and destination strings identical (facility + city).
* ``same city``     - only the "City, ST" part of origin and destination matches; the
  facility names differ. Reported in a second table so the reader can judge.

VINs whose extra orders are on a different lane (out-and-back test-fleet moves) are not
duplicates and are left out.

Usage::

    python duplicate_check/duplicate_check.py                         # last 7 days, email to outbox/
    python duplicate_check/duplicate_check.py --send                  # email through SES
    python duplicate_check/duplicate_check.py --days 0 --no-email     # the full 90-day list as CSV
    python duplicate_check/duplicate_check.py --from-json grid.json   # offline, from --save-json
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import html as html_lib
import json
import os
import re
import sys
from typing import Any

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (ROOT, os.path.join(ROOT, "due_date_alerts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from cargotel_search import BASE_URL, CargoTelSession, Grid, SearchParams, parse_grid  # noqa: E402
from due_date_alerts import DEFAULT_FROM, Email, FileTransport, SesTransport, append_log  # noqa: E402

SAVED_ID = "1863"  # Orders > Duplicate check
SEARCH_NAME = "Duplicate check"
SEARCH_PATH = "Orders > Duplicate check"
DEFAULT_WINDOW = "Last 90 days"
DEFAULT_DAYS = 7
DEFAULT_TO = ("justice.cromwell@circledelivers.com",)
DEFAULT_CC: tuple[str, ...] = ()
ORDER_URL = f"{BASE_URL}/backoffice/loadmaint.mcgi?load_id="

#: Columns added on the GET; CargoTel puts them first and appends the saved view's own.
EXTRA_VIEW_FIELDS = (
    "Vins17",          # Vin Full
    "bundleparent",    # Load ID the order rides on
    "LONG_Created",    # Date Created
    "act_pickup",      # PickUp Actual Date
    "deliver",         # Delivered Date
    "carriername",     # Carrier Name
    "sales_rep1",      # Sales Rep 1
    "originzip",
    "destinationzip",
)

OUT_COLUMNS = [
    "vin", "match", "group", "orders_in_group", "order_id", "load_id", "cons_ref", "status",
    "origin", "destination", "origin_zip", "dest_zip", "created", "actual_pickup",
    "delivered", "carrier", "sales_rep",
]

_WINDOW_SELECT_RE = re.compile(r'name="Menu__DateMne_DeliveredOr"[^>]*>(.*?)</SELECT>', re.S | re.I)
_SELECTED_OPTION_RE = re.compile(r'<OPTION value="([^"]*)"[^>]*\bSELECTED\b', re.I)
_SERVER_ERROR_RE = re.compile(r"An Error was encountered:(.{0,300})", re.S)
_DATE_FORMATS = ("%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y")


def log(message: str) -> None:
    print(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {message}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Fetch                                                                        #
# --------------------------------------------------------------------------- #


def applied_window(html: str) -> str | None:
    """The "Delivered Date Or Not Delivered" window CargoTel echoes in the search form."""

    form = html[: html.find("det_header_row")] if "det_header_row" in html else html
    select = _WINDOW_SELECT_RE.search(form)
    if not select:
        return None
    chosen = _SELECTED_OPTION_RE.search(select.group(1))
    return chosen.group(1) if chosen else None


def fetch_grid(expect_window: str, save_json: str | None = None) -> Grid:
    spec = SearchParams(
        SAVED_ID,
        per_page="All",
        extra={"Menu__ViewFields": list(EXTRA_VIEW_FIELDS)},  # type: ignore[dict-item]
    )
    session = CargoTelSession()
    status, html, params = session.fetch_search_html(spec)
    if status != 200:
        raise RuntimeError(f"Search.mcgi returned HTTP {status}")
    error = _SERVER_ERROR_RE.search(html)
    if error:
        raise RuntimeError("CargoTel reported: " + " ".join(re.sub(r"<[^>]+>", " ", error.group(1)).split()))
    grid = parse_grid(html, status)
    if grid.signin_page:
        raise RuntimeError("CargoTel answered with the sign-in page; the S3 cookie is stale")
    window = applied_window(html)
    if window != expect_window:
        raise RuntimeError(
            f"saved search {SAVED_ID} applied the window {window!r}, expected {expect_window!r}; "
            f"fix the saved search in CargoTel ({SEARCH_PATH}) or pass --expect-window"
        )
    log(f"GET Search.mcgi {params} -> {len(html)} chars; window {window!r}; {grid.summary()}")
    if save_json:
        os.makedirs(os.path.dirname(os.path.abspath(save_json)), exist_ok=True)
        with open(save_json, "w", encoding="utf-8") as fh:
            json.dump({"columns": [c.__dict__ for c in grid.columns], "window": window, "rows": grid.records()}, fh)
    return grid


# --------------------------------------------------------------------------- #
# Analysis                                                                     #
# --------------------------------------------------------------------------- #


def _text(rec: dict[str, Any], key: str) -> str:
    value = rec.get(key, "")
    if isinstance(value, list):
        value = " ".join(value)
    return " ".join(str(value or "").split())


def _vin(rec: dict[str, Any]) -> str:
    vin = _text(rec, "Vins17") or _text(rec, "vins17")
    if not vin:
        vin = _text(rec, "section").replace("Vins:", "").strip()
    return vin.upper()


def _city(place: str) -> str:
    return place.split("|")[-1].strip().lower()


def parse_date(value: str) -> dt.datetime | None:
    value = (value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def in_window(rec: dict[str, Any], now: dt.datetime, days: int) -> bool:
    """What CargoTel's "Delivered Date Or Not Delivered: Last N days" keeps: an order with
    no delivered date, or one delivered within the last ``days`` days. ``days <= 0`` keeps
    everything the saved search returned."""

    if days <= 0:
        return True
    delivered = parse_date(_text(rec, "deliver"))
    if delivered is None:
        return True
    return delivered >= now - dt.timedelta(days=days)


def find_duplicates(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One output row per order, for every VIN with two or more distinct order numbers on
    the same lane. Exact-lane groups come first for a VIN, then city-only groups."""

    by_vin: dict[str, list[dict[str, Any]]] = collections.defaultdict(list)
    for rec in records:
        vin = _vin(rec)
        if vin:
            by_vin[vin].append(rec)

    out: list[dict[str, Any]] = []
    group_no = 0
    for vin in sorted(by_vin):
        recs = by_vin[vin]
        if len({_text(r, "load_id") for r in recs}) < 2:
            continue
        exact: dict[tuple[str, str], dict[str, dict[str, Any]]] = collections.defaultdict(dict)
        for r in recs:
            exact[(_text(r, "origin").lower(), _text(r, "destination").lower())][_text(r, "load_id")] = r
        claimed: set[str] = set()
        lanes: list[tuple[str, dict[str, dict[str, Any]]]] = []
        for orders in exact.values():
            if len(orders) > 1:
                lanes.append(("same facility", orders))
                claimed.update(orders)
        city: dict[tuple[str, str], dict[str, dict[str, Any]]] = collections.defaultdict(dict)
        for r in recs:
            if _text(r, "load_id") not in claimed:
                city[(_city(_text(r, "origin")), _city(_text(r, "destination")))][_text(r, "load_id")] = r
        for orders in city.values():
            if len(orders) > 1:
                lanes.append(("same city", orders))
        for match, orders in lanes:
            group_no += 1
            for order_id in sorted(orders):
                r = orders[order_id]
                out.append({
                    "vin": vin,
                    "match": match,
                    "group": group_no,
                    "orders_in_group": len(orders),
                    "order_id": order_id,
                    "load_id": _text(r, "bundleparent"),
                    "cons_ref": _text(r, "cons_ref"),
                    "status": _text(r, "status"),
                    "origin": _text(r, "origin"),
                    "destination": _text(r, "destination"),
                    "origin_zip": _text(r, "originzip"),
                    "dest_zip": _text(r, "destinationzip"),
                    "created": _text(r, "LONG_Created"),
                    "actual_pickup": _text(r, "act_pickup"),
                    "delivered": _text(r, "deliver"),
                    "carrier": _text(r, "carriername"),
                    "sales_rep": _text(r, "sales_rep1"),
                })
    return out


def write_csv(path: str, rows: list[dict[str, Any]]) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=OUT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


# --------------------------------------------------------------------------- #
# The email                                                                    #
# --------------------------------------------------------------------------- #


def _fmt_when(now: dt.datetime) -> str:
    return f"{now:%a %m/%d/%Y} {now.strftime('%I:%M %p').lstrip('0')} ET"


def _short_date(value: str) -> str:
    parsed = parse_date(value)
    return f"{parsed:%m/%d}" if parsed else (value or "-")


def _first_name(address_or_name: str) -> str:
    name = address_or_name.split("@")[0].split(".")[0]
    return name[:1].upper() + name[1:] if name else "there"


def _groups(rows: list[dict[str, Any]], match: str) -> list[list[dict[str, Any]]]:
    groups: dict[int, list[dict[str, Any]]] = collections.OrderedDict()
    for row in rows:
        if row["match"] == match:
            groups.setdefault(row["group"], []).append(row)
    return list(groups.values())


def _text_group(group: list[dict[str, Any]]) -> str:
    first = group[0]
    lines = [f"VIN {first['vin']}  |  {first['origin']} -> {first['destination']}"]
    for row in group:
        lines.append(
            f"  order {row['order_id']}" + (f" (load {row['load_id']})" if row["load_id"] else "")
            + f"  {row['status']}  created {row['created'] or '-'}"
            + (f"  picked up {row['actual_pickup']}" if row["actual_pickup"] else "")
            + (f"  delivered {row['delivered']}" if row["delivered"] else "")
            + (f"  {row['carrier']}" if row["carrier"] else "")
            + (f"  ref {row['cons_ref']}" if row["cons_ref"] else "")
        )
        if row["match"] == "same city" and (row["origin"] != first["origin"] or row["destination"] != first["destination"]):
            lines.append(f"      lane on this order: {row['origin']} -> {row['destination']}")
    return "\n".join(lines)


def _html_table(groups: list[list[dict[str, Any]]], *, show_lane_per_order: bool) -> str:
    esc = html_lib.escape
    headers = ["VIN", "Order", "Load", "Status", "Created", "Picked up", "Delivered", "Carrier", "Cons ref"]
    if show_lane_per_order:
        headers.insert(1, "Lane")
    th = "".join(f'<th style="text-align:left;padding:4px 8px;border-bottom:2px solid #999;white-space:nowrap">{h}</th>' for h in headers)
    body: list[str] = []
    for group in groups:
        first = group[0]
        if not show_lane_per_order:
            body.append(f'<tr><td colspan="{len(headers)}" style="padding:8px 8px 2px;font-weight:bold;border-top:1px solid #ddd">'
                        f"{esc(first['origin'])} &rarr; {esc(first['destination'])}</td></tr>")
        for i, row in enumerate(group):
            cells = [
                f'<td style="padding:4px 8px;font-family:Consolas,monospace">{esc(row["vin"]) if i == 0 else ""}</td>',
                f'<td style="padding:4px 8px"><a href="{ORDER_URL}{esc(row["order_id"])}">{esc(row["order_id"])}</a></td>',
                f'<td style="padding:4px 8px">{esc(row["load_id"] or "-")}</td>',
                f'<td style="padding:4px 8px">{esc(row["status"] or "-")}</td>',
                f'<td style="padding:4px 8px;white-space:nowrap">{esc(row["created"] or "-")}</td>',
                f'<td style="padding:4px 8px;white-space:nowrap">{esc(_short_date(row["actual_pickup"]))}</td>',
                f'<td style="padding:4px 8px;white-space:nowrap">{esc(_short_date(row["delivered"]))}</td>',
                f'<td style="padding:4px 8px">{esc(row["carrier"] or "-")}</td>',
                f'<td style="padding:4px 8px">{esc(row["cons_ref"] or "-")}</td>',
            ]
            if show_lane_per_order:
                cells.insert(1, f'<td style="padding:4px 8px">{esc(row["origin"])} &rarr; {esc(row["destination"])}</td>')
            style = ' style="border-top:1px solid #ddd"' if (show_lane_per_order and i == 0) else ""
            body.append(f"<tr{style}>{''.join(cells)}</tr>")
    return f'<table style="border-collapse:collapse;font-size:13px"><tr>{th}</tr>{"".join(body)}</table>'


def compose(rows: list[dict[str, Any]], *, to: list[str], cc: list[str], now: dt.datetime, days: int,
            total_orders: int, total_vins: int) -> Email:
    """The weekly email: one table per match level, plain text and HTML."""

    exact = _groups(rows, "same facility")
    near = _groups(rows, "same city")
    n_vins = len({r["vin"] for r in rows})
    window = f"last {days} days" if days > 0 else "last 90 days"
    scope = (f"the {SEARCH_PATH} search in CargoTel (Stellantis orders, Delivered Date Or Not Delivered: {window})")

    if rows:
        subject = f"Duplicate order check: {n_vins} VIN{'s' if n_vins != 1 else ''} with more than one order on the same lane ({window})"
    else:
        subject = f"Duplicate order check: no duplicate orders found ({window})"

    greeting = f"Hi {_first_name(to[0]) if to else 'there'},"
    if rows:
        intro = (f"As of {_fmt_when(now)}, {scope} shows {n_vins} VIN{'s' if n_vins != 1 else ''} on more than one order number "
                 f"with the same origin and destination. {total_orders} orders on {total_vins} VINs were checked.")
    else:
        intro = (f"As of {_fmt_when(now)}, {scope} shows no VIN on more than one order number with the same origin and "
                 f"destination. {total_orders} orders on {total_vins} VINs were checked.")
    note_exact = "Same origin and destination (facility and city)"
    note_near = "Same cities, different facility names - please review"
    footer = (f"This check runs every Monday at 8:00 AM ET from {SEARCH_PATH}. VINs whose other orders are on a different lane "
              f"(out-and-back moves) are not listed. Order numbers link to the order in CargoTel.")

    text_parts = [greeting, "", intro, ""]
    if exact:
        text_parts += [f"{note_exact.upper()} ({len(exact)} VIN{'s' if len(exact) != 1 else ''})", ""]
        text_parts += [_text_group(g) + "\n" for g in exact]
    if near:
        text_parts += [f"{note_near.upper()} ({len(near)} VIN{'s' if len(near) != 1 else ''})", ""]
        text_parts += [_text_group(g) + "\n" for g in near]
    text_parts += [footer]
    text = "\n".join(text_parts)

    esc = html_lib.escape
    sections = []
    if exact:
        sections.append(f'<h3 style="margin:16px 0 4px">{esc(note_exact)} ({len(exact)} VIN{"s" if len(exact) != 1 else ""})</h3>'
                        + _html_table(exact, show_lane_per_order=False))
    if near:
        sections.append(f'<h3 style="margin:16px 0 4px">{esc(note_near)} ({len(near)} VIN{"s" if len(near) != 1 else ""})</h3>'
                        + _html_table(near, show_lane_per_order=True))
    html = ('<html><body style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222">'
            f"<p>{esc(greeting)}</p><p>{esc(intro)}</p>" + "".join(sections)
            + f'<p style="font-size:12px;color:#666;margin-top:16px">{esc(footer)}</p>'
            "</body></html>")
    return Email(key=f"duplicate-check:{now:%Y-%m-%d}", rep=_first_name(to[0]) if to else "duplicate-check", to=to, cc=cc,
                 subject=subject, text=text, html=html, load_ids=sorted({r["order_id"] for r in rows}))


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _addresses(value: str | None) -> list[str]:
    return [a.strip() for a in (value or "").split(",") if a.strip()]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS, metavar="N",
                        help=f"keep orders delivered in the last N days or not delivered (default {DEFAULT_DAYS}; 0 = the whole saved window)")
    parser.add_argument("--expect-window", default=DEFAULT_WINDOW, metavar="WINDOW",
                        help=f'the "Delivered Date Or Not Delivered" window the saved search must apply (default "{DEFAULT_WINDOW}")')
    parser.add_argument("--from-json", metavar="FILE", help="analyse a grid saved by --save-json instead of fetching")
    parser.add_argument("--save-json", metavar="FILE", help="archive the fetched grid as JSON")
    parser.add_argument("--out", metavar="CSV", help="CSV path (default duplicate_check/duplicate_orders_<today>.csv)")
    parser.add_argument("--now", metavar="YYYY-MM-DDTHH:MM", help="pretend it is this time (for offline runs)")
    parser.add_argument("--to", default=",".join(DEFAULT_TO), help="comma-separated recipients")
    parser.add_argument("--cc", default=",".join(DEFAULT_CC), help="comma-separated Cc addresses")
    parser.add_argument("--from", dest="from_addr", default=os.environ.get("MAIL_FROM", DEFAULT_FROM), help="verified SES sender")
    parser.add_argument("--send", action="store_true", help="send through SES (otherwise the email is written to --outbox)")
    parser.add_argument("--outbox", default=os.path.join(HERE, "outbox"), help="dry-run folder")
    parser.add_argument("--sent-log", default=os.path.join(HERE, "sent_log.jsonl"), help="one JSON line per email")
    parser.add_argument("--configuration-set", default=os.environ.get("SES_CONFIGURATION_SET") or None, help="SES configuration set")
    parser.add_argument("--skip-empty", action="store_true", help="do not email when nothing was found")
    parser.add_argument("--no-email", action="store_true", help="only write the CSV")
    args = parser.parse_args(argv)

    now = dt.datetime.strptime(args.now, "%Y-%m-%dT%H:%M") if args.now else dt.datetime.now()
    window = args.expect_window
    if args.from_json:
        with open(args.from_json, encoding="utf-8") as fh:
            saved = json.load(fh)
        records = saved["rows"]
        window = saved.get("window") or window
    else:
        records = fetch_grid(args.expect_window, args.save_json).records()

    kept = [r for r in records if in_window(r, now, args.days)]
    dups = find_duplicates(kept)
    out = args.out or os.path.join(HERE, f"duplicate_orders_{now:%Y-%m-%d}.csv")
    write_csv(out, dups)

    vins = {d["vin"] for d in dups}
    n_vins = len({_vin(r) for r in kept if _vin(r)})
    print(f"{len(records)} orders in the saved window '{window}', {len(kept)} orders / {n_vins} VINs after the "
          f"{args.days}-day filter; {len(vins)} VINs with duplicate orders on the same lane ({len(dups)} orders) -> {out}")
    for d in dups:
        print(f"  {d['vin']}  [{d['match']}]  order {d['order_id']}  load {d['load_id'] or '-'}  ref {d['cons_ref'] or '-'}  "
              f"{d['status']:10s} created {d['created'] or '-':16s} delivered {d['delivered'] or '-':16s} {d['carrier']}  "
              f"|  {d['origin']} -> {d['destination']}")

    if args.no_email:
        return 0
    if args.skip_empty and not dups:
        print("nothing found and --skip-empty set; no email")
        return 0
    to, cc = _addresses(args.to), _addresses(args.cc)
    if not to:
        print("no recipients (--to); no email", file=sys.stderr)
        return 2
    email = compose(dups, to=to, cc=cc, now=now, days=args.days, total_orders=len(kept), total_vins=n_vins)
    transport: Any = SesTransport(configuration_set=args.configuration_set) if args.send else FileTransport(args.outbox)
    message_id = transport.send(email, args.from_addr)
    append_log(args.sent_log, {
        "at": now.isoformat(timespec="seconds"), "to": to, "cc": cc, "subject": email.subject, "days": args.days,
        "vins": sorted(vins), "orders": email.load_ids, "sent": bool(args.send), "message_id": message_id,
    })
    print(f"{'SENT via SES' if args.send else 'dry run'} -> {', '.join(to)}{' cc ' + ', '.join(cc) if cc else ''}: "
          f"{email.subject} [{message_id}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
