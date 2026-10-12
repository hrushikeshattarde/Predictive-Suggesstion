"""
Dispatch watcher: polls CargoTel's Assigned Carrier Report and drafts a reload-suggestion
email for each truck as soon as a new load is dispatched and the carrier has accepted
the offer (``--trigger accepted``, the default), or when a truck has dropped all of its
loads (``--trigger delivered``). Drafts are files on disk; nothing is sent here.

What "accepted" means in CargoTel
---------------------------------
A load offered to a carrier sits on "Dispatched - Waiting Carrier Acceptance" (saved
search 1543) until the carrier accepts; only then does it appear on the Assigned
Carrier Report (1587) with status Dispatched. So a new Dispatched row on 1587 that is
not on 1543 is an accepted dispatch. Belt and braces: the load page then lists an
"E-Signed Carrier Agreement" attachment; ``--require-esign`` insists on it (off by
default, because a load dispatched outside the e-signature flow never gets one).

Trucks and events
-----------------
* A truck is the carrier plus the driver name on the load. The driver is never emailed;
  the name only tells one of a carrier's trucks apart from another. A truck with no
  driver name is handled per load.
* ``accepted``: new Dispatched (or In-Route) rows whose dispatched time is within
  ``--max-age-hours``, minus the waiting list, grouped by truck; the suggester runs on
  the truck's most recently dispatched load and the email says "delivers on ...".
* ``delivered``: a truck with a Delivered load (with a delivery date) and nothing still
  Dispatched or In-Route; the suggester runs on the last delivered load and the email
  says "you dropped ...".
* The event key is (load, carrier, driver). A JSON state file remembers handled keys, so
  polling the same rows again does nothing. Failures leave the event for the next pass,
  up to ``--max-attempts``; ``--max-events`` caps the work per pass.

Usage::

    python dispatch_watcher.py --dry-run                 # list accepted dispatches in the last 24 h
    python dispatch_watcher.py --seed                    # remember them without drafting
    python dispatch_watcher.py                           # one pass (cron, Task Scheduler, EventBridge)
    python dispatch_watcher.py --loop --interval 900     # keep polling every 15 minutes
    python dispatch_watcher.py --trigger delivered       # the empty-truck trigger instead
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable

from cargotel_search import CargoTelSession, Grid, SearchParams, parse_grid
from suggest_reloads import (
    DEFAULT_MAX,
    DEFAULT_MILES,
    DEFAULT_MIN,
    DEFAULT_SAVED_ID,
    NotAcceptedError,
    draft_for_load,
    email_text,
)

ASSIGNED_CARRIER_REPORT = "1587"
WAITING_ACCEPTANCE_REPORT = "1543"
ACTIVE_STATUSES = ("Dispatched", "In-Route")
DONE_STATUS = "Delivered"
DEFAULT_STATE = "watcher_state.json"
DEFAULT_OUT_DIR = "drafts"
DEFAULT_MAX_EVENTS = 20
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_MAX_AGE_HOURS = 24

_DATE_FORMATS = ("%m/%d/%Y %H:%M", "%m/%d/%Y", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_when(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "x"


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def _to_int(value: str) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


# --------------------------------------------------------------------------- #
# Events                                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class TruckEvent:
    """One truck to email. ``load_id`` is the load whose destination is where the truck
    will be (accepted: the newest dispatched load; delivered: the last delivered one)."""

    carrier: str
    driver: str
    load_id: str
    trigger: str  # "accepted" or "delivered"
    when_text: str | None  # dispatched or delivered time as shown, "10/12/2026 09:17"
    destination: str = ""
    origin: str = ""
    units: int = 0
    #: Every load of this truck in the report window that the trigger counted, oldest first.
    loads: list[str] = field(default_factory=list)

    @property
    def key(self) -> str:
        return f"{self.load_id}|{_norm(self.carrier)}|{_norm(self.driver)}"

    def when(self) -> datetime | None:
        return parse_when(self.when_text)

    @property
    def truck(self) -> str:
        return f"{self.carrier} / {self.driver}" if self.driver else self.carrier


def _trucks(grid: Grid) -> dict[tuple[str, str], list]:
    trucks: dict[tuple[str, str], list] = collections.OrderedDict()
    for row in grid.rows:
        if row.link_class == "LinkBundleChild":
            continue
        carrier = row.text("carriername")
        if not row.load_id or not carrier:
            continue
        trucks.setdefault((carrier, row.text("driver_name")), []).append(row)
    return trucks


def accepted_events_from_grid(grid: Grid, waiting_ids: set[str] | None = None) -> tuple[list[TruckEvent], collections.Counter]:
    """New accepted dispatches, one event per truck, oldest dispatch first.

    ``waiting_ids`` are the loads still on the waiting-for-acceptance search; they are
    left out even if the report shows them. Stats: ``trucks``, ``waiting_excluded``,
    ``no_driver``, ``undated``.
    """

    waiting = {str(i) for i in (waiting_ids or set())}
    stats: collections.Counter = collections.Counter()
    events: list[TruckEvent] = []
    for (carrier, driver), rows in _trucks(grid).items():
        active = [r for r in rows if r.text("status") in ACTIVE_STATUSES]
        kept = []
        for r in active:
            if r.load_id in waiting:
                stats["waiting_excluded"] += 1
            elif not parse_when(r.text("statuspendingpickup")):
                stats["undated"] += 1
            else:
                kept.append(r)
        if not kept:
            continue
        kept.sort(key=lambda r: (parse_when(r.text("statuspendingpickup")), r.load_id))
        if not driver:
            for r in kept:
                events.append(TruckEvent(carrier, "", r.load_id, "accepted", r.text("statuspendingpickup"), r.text("destination"), r.text("origin"), _to_int(r.text("units")), [r.load_id]))
            stats["no_driver"] += len(kept)
            continue
        last = kept[-1]
        events.append(TruckEvent(carrier, driver, last.load_id, "accepted", last.text("statuspendingpickup"), last.text("destination"), last.text("origin"),
                                 sum(_to_int(r.text("units")) for r in kept), [r.load_id for r in kept]))
        stats["trucks"] += 1
    events.sort(key=lambda e: (e.when() or datetime.min, e.load_id))
    return events, stats


def delivered_events_from_grid(grid: Grid) -> tuple[list[TruckEvent], collections.Counter]:
    """Empty trucks: a Delivered load with a date and nothing still active. Stats:
    ``trucks`` (empty), ``busy``, ``no_driver``, ``undated``."""

    stats: collections.Counter = collections.Counter()
    events: list[TruckEvent] = []
    for (carrier, driver), rows in _trucks(grid).items():
        active = [r for r in rows if r.text("status") in ACTIVE_STATUSES]
        done = [r for r in rows if r.text("status") == DONE_STATUS]
        dated = [r for r in done if parse_when(r.text("deliver"))]
        stats["undated"] += len(done) - len(dated)
        if not dated:
            continue
        if active:
            stats["busy"] += 1
            continue
        dated.sort(key=lambda r: (parse_when(r.text("deliver")), r.load_id))
        if not driver:
            for r in dated:
                events.append(TruckEvent(carrier, "", r.load_id, "delivered", r.text("deliver"), r.text("destination"), r.text("origin"), _to_int(r.text("units")), [r.load_id]))
            stats["no_driver"] += len(dated)
            continue
        last = dated[-1]
        events.append(TruckEvent(carrier, driver, last.load_id, "delivered", last.text("deliver"), last.text("destination"), last.text("origin"),
                                 sum(_to_int(r.text("units")) for r in dated), [r.load_id for r in dated]))
        stats["trucks"] += 1
    events.sort(key=lambda e: (e.when() or datetime.min, e.load_id))
    return events, stats


def fetch_grid(session: CargoTelSession, report_id: str, per_page: str = "1000") -> tuple[Grid, str]:
    status, html, _ = session.fetch_search_html(SearchParams(report_id, per_page=per_page))
    grid = parse_grid(html, status)
    if grid.signin_page:
        raise RuntimeError("CargoTel answered with the sign-in page; the session cookie is stale")
    if status != 200:
        raise RuntimeError(f"saved search {report_id} returned HTTP {status}")
    return grid, html


# --------------------------------------------------------------------------- #
# State                                                                        #
# --------------------------------------------------------------------------- #


class WatcherState:
    """``{"created": ..., "updated": ..., "events": {key: record}}`` on disk."""

    def __init__(self, path: str) -> None:
        self.path = path
        self.created: str = datetime.now().isoformat(timespec="seconds")
        self.events: dict[str, dict[str, Any]] = {}
        self.fresh = True
        if os.path.exists(path):
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            self.created = data.get("created", self.created)
            self.events = data.get("events", {})
            self.fresh = False

    def save(self) -> None:
        payload = {"created": self.created, "updated": datetime.now().isoformat(timespec="seconds"), "events": self.events}
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        os.replace(tmp, self.path)

    def record(self, key: str) -> dict[str, Any] | None:
        return self.events.get(key)

    def mark(self, event: TruckEvent, outcome: str, **extra: Any) -> dict[str, Any]:
        rec = self.events.get(event.key) or {"attempts": 0}
        rec.update(asdict(event))
        rec.update({"outcome": outcome, "handled_at": datetime.now().isoformat(timespec="seconds")})
        rec.update(extra)
        self.events[event.key] = rec
        return rec


HANDLED = {"drafted", "no_picks", "seeded", "old"}


# --------------------------------------------------------------------------- #
# One pass                                                                     #
# --------------------------------------------------------------------------- #


def write_draft(out_dir: str, event: TruckEvent, result: dict[str, Any]) -> tuple[str, str]:
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"{event.load_id}_{slug(event.carrier)}")
    with open(base + ".txt", "w", encoding="utf-8") as handle:
        handle.write(email_text(result["email"]))
    payload = {k: v for k, v in result.items() if k not in ("picks_objects", "load")}
    payload["event"] = asdict(event)
    with open(base + ".json", "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, default=str)
    return base + ".txt", base + ".json"


def run_once(
    events: list[TruckEvent],
    state: WatcherState,
    draft: Callable[[TruckEvent], dict[str, Any]] | None,
    *,
    out_dir: str = DEFAULT_OUT_DIR,
    since: datetime | None = None,
    seed: bool = False,
    dry_run: bool = False,
    max_events: int = DEFAULT_MAX_EVENTS,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    log: Callable[[str], None] = print,
) -> collections.Counter:
    """Handle every event not yet in ``state``. Returns counts by outcome."""

    counts: collections.Counter = collections.Counter()
    worked = 0
    for event in events:
        rec = state.record(event.key)
        if rec and rec.get("outcome") in HANDLED:
            counts["already_handled"] += 1
            continue
        if rec and rec.get("attempts", 0) >= max_attempts:
            counts["given_up"] += 1
            continue
        if seed:
            state.mark(event, "seeded")
            counts["seeded"] += 1
            continue
        when = event.when()
        if since is not None and when is not None and when < since:
            state.mark(event, "old")
            counts["old"] += 1
            continue
        verb = "dispatched" if event.trigger == "accepted" else "dropped"
        if dry_run:
            counts["new"] += 1
            log(f"new: {event.truck} {verb} {len(event.loads)} load(s), {event.load_id} -> {event.destination}, {event.when_text or '?'}")
            continue
        if worked >= max_events:
            counts["deferred"] += 1
            continue
        worked += 1
        if draft is None:
            raise ValueError("a draft callable is required unless --dry-run or --seed")
        try:
            result = draft(event)
        except NotAcceptedError as exc:
            attempts = (rec or {}).get("attempts", 0) + 1
            state.mark(event, "waiting_esign", attempts=attempts, error=str(exc)[:300])
            counts["waiting_esign"] += 1
            log(f"waiting: {event.truck}, load {event.load_id}: {exc} [attempt {attempts}/{max_attempts}]")
            continue
        except Exception as exc:  # noqa: BLE001 - any failure must not poison the run
            attempts = (rec or {}).get("attempts", 0) + 1
            state.mark(event, "error", attempts=attempts, error=str(exc)[:300])
            counts["error"] += 1
            log(f"error: {event.truck}, load {event.load_id}: {exc} [attempt {attempts}/{max_attempts}]")
            continue
        picks = result.get("picks") or []
        search = result.get("search") or {}
        if not picks:
            state.mark(event, "no_picks", attempts=0, picks=0, search_rows=search.get("rows"))
            counts["no_picks"] += 1
            log(f"no picks: {event.truck} -> {event.destination}: {search.get('rows', 0)} rows in radius, nothing to offer")
            continue
        txt_path, json_path = write_draft(out_dir, event, result)
        state.mark(event, "drafted", attempts=0, picks=len(picks), draft=txt_path, json=json_path,
                   built_loads=search.get("built_loads"), single_orders=search.get("single_orders"),
                   to=result["email"]["to"], subject=result["email"]["subject"])
        counts["drafted"] += 1
        log(f"drafted: {event.truck} -> {event.destination}: {len(picks)} picks "
            f"({search.get('built_loads', 0)} built loads, {search.get('single_orders', 0)} orders in radius) -> {txt_path}")
    return counts


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch CargoTel for accepted dispatches (or emptied trucks) and draft reload emails (never sends).")
    parser.add_argument("--trigger", choices=("accepted", "delivered"), default="accepted", help="accepted: new dispatch with the offer accepted (default); delivered: truck dropped everything")
    parser.add_argument("--state", default=DEFAULT_STATE, help=f"JSON state file (default {DEFAULT_STATE})")
    parser.add_argument("--out-dir", default=DEFAULT_OUT_DIR, help=f"where drafts go (default {DEFAULT_OUT_DIR}/)")
    parser.add_argument("--report-id", default=ASSIGNED_CARRIER_REPORT, help="Assigned Carrier Report saved search")
    parser.add_argument("--waiting-id", default=WAITING_ACCEPTANCE_REPORT, help="Waiting Carrier Acceptance saved search; '' to skip")
    parser.add_argument("--require-esign", action="store_true", help="also insist on an E-Signed Carrier Agreement on the load page")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS, help="ignore dispatches/deliveries older than this (default 24)")
    parser.add_argument("--since", help='handle only events on/after this time, "10/12/2026 09:00" (overrides --max-age-hours)')
    parser.add_argument("--seed", action="store_true", help="mark every current event as seen, draft nothing")
    parser.add_argument("--dry-run", action="store_true", help="list events, draft nothing, do not touch state")
    parser.add_argument("--loop", action="store_true", help="keep polling")
    parser.add_argument("--interval", type=int, default=900, help="seconds between polls with --loop (default 900)")
    parser.add_argument("--max-events", type=int, default=DEFAULT_MAX_EVENTS, help="drafts per pass (default 20)")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS)
    parser.add_argument("--miles", type=int, default=DEFAULT_MILES, choices=(10, 25, 50, 75, 150, 300))
    parser.add_argument("--saved-id", default=DEFAULT_SAVED_ID, help="Load Build search for candidates")
    parser.add_argument("--min", dest="min_n", type=int, default=DEFAULT_MIN)
    parser.add_argument("--max", dest="max_n", type=int, default=DEFAULT_MAX)
    parser.add_argument("--show-pay", action="store_true")
    parser.add_argument("--save-html", metavar="DIR", help="archive fetched pages here")
    args = parser.parse_args(argv)

    state = WatcherState(args.state)
    if args.since:
        since = parse_when(args.since)
        if since is None:
            parser.error(f"--since: could not parse {args.since!r}")
    else:
        since = datetime.now() - timedelta(hours=args.max_age_hours)
    session = CargoTelSession()

    def draft(event: TruckEvent) -> dict[str, Any]:
        return draft_for_load(session, event.load_id, miles=args.miles, saved_id=args.saved_id, min_n=args.min_n,
                              max_n=args.max_n, show_pay=args.show_pay, save_html_dir=args.save_html,
                              delivered_at=event.when_text if event.trigger == "delivered" else None,
                              trip_loads=event.loads, require_esign=args.require_esign and event.trigger == "accepted")

    def log(message: str) -> None:
        print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}", flush=True)

    while True:
        started = time.monotonic()
        try:
            grid, report_html = fetch_grid(session, args.report_id)
            waiting_ids: set[str] = set()
            if args.trigger == "accepted" and args.waiting_id:
                waiting_grid, _ = fetch_grid(session, args.waiting_id)
                waiting_ids = {r.load_id for r in waiting_grid.rows if r.load_id}
        except Exception as exc:  # noqa: BLE001
            log(f"report fetch failed: {exc}")
            if not args.loop:
                return 2
            time.sleep(args.interval)
            continue
        if args.save_html:
            os.makedirs(args.save_html, exist_ok=True)
            with open(os.path.join(args.save_html, f"report_{args.report_id}_{datetime.now():%Y%m%d_%H%M%S}.html"), "w", encoding="utf-8") as handle:
                handle.write(report_html)
        if args.trigger == "accepted":
            events, stats = accepted_events_from_grid(grid, waiting_ids)
            log(f"report {args.report_id}: {len(grid.rows)} rows; accepted dispatches on {stats['trucks']} trucks, "
                f"{stats['no_driver']} without a driver name, {stats['waiting_excluded']} still waiting acceptance ({len(waiting_ids)} on the waiting list), "
                f"{stats['undated']} undated; state has {len(state.events)} handled; since {since:%m/%d/%Y %H:%M}")
        else:
            events, stats = delivered_events_from_grid(grid)
            log(f"report {args.report_id}: {len(grid.rows)} rows; trucks empty {stats['trucks']}, still busy {stats['busy']}, "
                f"no-driver deliveries {stats['no_driver']}, undated {stats['undated']}; state has {len(state.events)} handled; since {since:%m/%d/%Y %H:%M}")
        counts = run_once(
            events, state, None if (args.dry_run or args.seed) else draft,
            out_dir=args.out_dir, since=since, seed=args.seed, dry_run=args.dry_run,
            max_events=args.max_events, max_attempts=args.max_attempts, log=log,
        )
        if not args.dry_run:
            state.save()
        log("pass done: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) + f" ({time.monotonic() - started:.0f}s)")
        if not args.loop:
            return 0
        time.sleep(max(args.interval - (time.monotonic() - started), 5))


if __name__ == "__main__":
    sys.exit(main())
