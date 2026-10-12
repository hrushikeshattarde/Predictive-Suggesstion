"""Unit tests for dispatch_watcher.py (``python -m unittest``). No network: the report grid
is a synthetic page shaped like the live Assigned Carrier Report, and the suggester is
replaced by a fake ``draft`` callable."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cargotel_search import parse_grid  # noqa: E402
from dispatch_watcher import (  # noqa: E402
    TruckEvent,
    WatcherState,
    accepted_events_from_grid,
    delivered_events_from_grid,
    parse_when,
    run_once,
    slug,
)
from suggest_reloads import NotAcceptedError  # noqa: E402

HEADER = """
<tr static="" id="det_header_row" class="unshadow dv_row">
<th class="dv_cell dv_string dv_header oeheader" id="row_number_head">#</th>
<th class="dv_cell dv_cell_status dv_header dv_header_status oeheader"><span class="dv_header_name">Status</span></th>
<th class="dv_cell dv_cell_load_id dv_header dv_header_load_id oeheader"><span class="dv_header_name">Order ID</span></th>
<th class="dv_cell dv_cell_carriername dv_header dv_header_carriername oeheader"><span class="dv_header_name">Carrier Name</span></th>
<th class="dv_cell dv_cell_driver_name dv_header dv_header_driver_name oeheader"><span class="dv_header_name">Driver Name</span></th>
<th class="dv_cell dv_cell_units dv_header dv_header_units oeheader"><span class="dv_header_name">Units</span></th>
<th class="dv_cell dv_cell_origin dv_header dv_header_origin oeheader"><span class="dv_header_name">Origin</span></th>
<th class="dv_cell dv_cell_destination dv_header dv_header_destination oeheader"><span class="dv_header_name">Destination</span></th>
<th class="dv_cell dv_cell_statuspendingpickup dv_header dv_header_statuspendingpickup oeheader"><span class="dv_header_name">Dispatched Status Date</span></th>
<th class="dv_cell dv_cell_deliver dv_header dv_header_deliver oeheader"><span class="dv_header_name">Delivered Date</span></th>
</tr>
"""


def row(n, load_id, status, carrier, driver, dispatched, delivered="", link_class="LinkIndividual", units="1", origin="Elwood, IL", dest="Gaylord, MI"):
    return f"""
<tr id="det_row_{n}"><td class="dv_linenumber">{n}</td>
<td class="dv_cell listable_cell dv_plain dv_cell_status" id="DetCell_{n}_0">{status}</td>
<td class="dv_cell listable_cell dv_number dv_cell_load_id" id="DetCell_{n}_1"><A name="MapId_{load_id}" class="{link_class}" href="/backoffice/loadmaint.mcgi?load_id={load_id}">{load_id}</A><input type='hidden' name='load_id' value="{load_id}"></td>
<td class="dv_cell listable_cell dv_plain dv_cell_carriername" id="DetCell_{n}_2">{carrier}</td>
<td class="dv_cell listable_cell dv_plain dv_cell_driver_name" id="DetCell_{n}_3">{driver}</td>
<td class="dv_cell listable_cell dv_number dv_cell_units" id="DetCell_{n}_4">{units}</td>
<td class="dv_cell listable_cell dv_plain dv_cell_origin" id="DetCell_{n}_5"><b>Yard</b><br>{origin}</td>
<td class="dv_cell listable_cell dv_plain dv_cell_destination" id="DetCell_{n}_6"><b>Dealer</b><br>{dest}</td>
<td class="dv_cell listable_cell dv_plain dv_cell_statuspendingpickup" id="DetCell_{n}_7">{dispatched}</td>
<td class="dv_cell listable_cell dv_plain dv_cell_deliver" id="DetCell_{n}_8">{delivered}</td>
</tr>"""


ROWS = [
    # Truck A (88 Transpoort / Padureanu): two loads, both delivered -> one delivered event on the later one
    row(1, 343514, "Delivered", "88 Transpoort LLC", "PADUREANU, EUGEN", "10/09/2026 09:17", "10/12/2026 08:30", link_class="LinkBundleParent", units="3", origin="Carol Stream, IL", dest="Grapevine, TX"),
    row(2, 343515, "Delivered", "88 Transpoort LLC", "PADUREANU, EUGEN", "10/09/2026 09:17", "10/11/2026 16:00", dest="Dallas, TX"),
    row(3, 343516, "Delivered", "88 Transpoort LLC", "PADUREANU, EUGEN", "10/09/2026 09:17", "10/12/2026 08:30", link_class="LinkBundleChild", dest="Grapevine, TX"),
    # Truck B (Abs Chicago / Smith): delivered one, still carrying another -> busy for delivered; one accepted event (old)
    row(4, 343539, "Delivered", "Abs Chicago Inc", "SMITH, JOHN", "10/08/2026 16:57", "10/12/2026 07:00"),
    row(5, 343540, "In-Route", "Abs Chicago Inc", "SMITH, JOHN", "10/10/2026 10:00", dest="Toledo, OH"),
    # Truck C (Lg2 / Lee): delivered three days ago -> delivered event, old for a 24 h window
    row(6, 343700, "Delivered", "Lg2 Transports LLC", "LEE, KIM", "10/05/2026 11:47", "10/09/2026 12:00", dest="Orland Hills, IL"),
    # Carrier D, no driver name: two delivered loads -> one delivered event each
    row(7, 343800, "Delivered", "No Driver Carriers Inc", "", "10/11/2026 09:00", "10/12/2026 09:00", dest="Eagan, MN"),
    row(8, 343801, "Delivered", "No Driver Carriers Inc", "", "10/11/2026 09:00", "10/12/2026 10:00", dest="Burnsville, MN"),
    # Delivered without a delivery date -> skipped
    row(9, 343900, "Delivered", "Undated Inc", "DOE, JANE", "10/11/2026 09:00", ""),
    # Truck E (Fresh Inc / NEW): two new dispatches today -> one accepted event on the later one
    row(10, 344000, "Dispatched", "Fresh Inc", "NEW, DRIVER", "10/12/2026 11:00", dest="Haverhill, MA"),
    row(12, 344010, "Dispatched", "Fresh Inc", "NEW, DRIVER", "10/12/2026 11:30", dest="Boston, MA", units="2"),
    # Still on the waiting-for-acceptance list -> excluded from accepted events
    row(13, 344020, "Dispatched", "Waiting Carrier LLC", "W, X", "10/12/2026 11:40", dest="Sarasota, FL"),
    # Dispatched, no driver name -> accepted event per load
    row(14, 344030, "Dispatched", "Nodriver Dispatch Inc", "", "10/12/2026 11:45", dest="Seattle, WA"),
    # No carrier -> dropped everywhere
    row(11, 344001, "Delivered", "", "X, Y", "10/12/2026 11:00", "10/12/2026 12:00"),
]

REPORT = ("<html><body><INPUT type=\"hidden\" name=\"Control__keepTitle\" value=\"Assigned Carrier Report\"><table>" + HEADER
          + "".join(ROWS) + f"</table>{len(ROWS)} rows returned Rows 1 - {len(ROWS)} of {len(ROWS)}</body></html>")
WAITING = {"344020"}


def fake_draft_factory(picks_by_load, fail=(), not_accepted=()):
    calls = []

    def draft(event):
        calls.append(event.load_id)
        if event.load_id in fail:
            raise RuntimeError("boom")
        if event.load_id in not_accepted:
            raise NotAcceptedError("no e-signed agreement yet")
        n = picks_by_load.get(event.load_id, 0)
        return {
            "email": {"to": "dispatch@example.com", "cc": "<rep>", "subject": f"Next loads after load {event.load_id}", "body": "..."},
            "picks": [{"load_id": f"9{i}"} for i in range(n)],
            "search": {"rows": 10, "built_loads": 2, "single_orders": 8},
            "dispatched": {"load_id": event.load_id},
            "picks_objects": ["not serializable"],
            "load": object(),
        }

    draft.calls = calls
    return draft


class AcceptedEventTests(unittest.TestCase):
    def test_new_accepted_dispatches_per_truck(self):
        events, stats = accepted_events_from_grid(parse_grid(REPORT), WAITING)
        self.assertEqual([e.load_id for e in events], ["343540", "344010", "344030"])  # oldest dispatch first
        self.assertEqual(stats["trucks"], 2)            # Abs Chicago/Smith, Fresh/New
        self.assertEqual(stats["no_driver"], 1)         # 344030
        self.assertEqual(stats["waiting_excluded"], 1)  # 344020
        fresh = next(e for e in events if e.load_id == "344010")
        self.assertEqual(fresh.trigger, "accepted")
        self.assertEqual(fresh.loads, ["344000", "344010"])
        self.assertEqual(fresh.when_text, "10/12/2026 11:30")
        self.assertEqual(fresh.when(), datetime(2026, 10, 12, 11, 30))
        self.assertEqual(fresh.units, 3)
        self.assertEqual(fresh.destination, "Dealer | Boston, MA")
        self.assertEqual(fresh.key, "344010|fresh inc|new, driver")
        self.assertEqual(fresh.truck, "Fresh Inc / NEW, DRIVER")
        self.assertNotIn("344020", [e.load_id for e in events])

    def test_without_waiting_list_everything_counts(self):
        events, stats = accepted_events_from_grid(parse_grid(REPORT))
        self.assertIn("344020", [e.load_id for e in events])
        self.assertEqual(stats["waiting_excluded"], 0)


class DeliveredEventTests(unittest.TestCase):
    def test_empty_trucks_become_events(self):
        events, stats = delivered_events_from_grid(parse_grid(REPORT))
        self.assertEqual([e.load_id for e in events], ["343700", "343514", "343800", "343801"])
        self.assertEqual(stats["trucks"], 2)       # trucks A and C
        self.assertEqual(stats["busy"], 1)         # truck B
        self.assertEqual(stats["no_driver"], 2)    # carrier D, per load
        self.assertEqual(stats["undated"], 1)
        a = next(e for e in events if e.load_id == "343514")
        self.assertEqual(a.trigger, "delivered")
        self.assertEqual(a.when_text, "10/12/2026 08:30")
        self.assertEqual(a.loads, ["343515", "343514"])  # bundle child skipped, oldest first
        self.assertEqual(a.units, 4)
        self.assertEqual(a.key, "343514|88 transpoort llc|padureanu, eugen")

    def test_parse_when_and_slug(self):
        self.assertEqual(parse_when("10/12/2026 09:17"), datetime(2026, 10, 12, 9, 17))
        self.assertEqual(parse_when("2026-10-12T09:17:00"), datetime(2026, 10, 12, 9, 17))
        self.assertIsNone(parse_when("soon"))
        self.assertEqual(slug("88 Transpoort LLC"), "88-transpoort-llc")


class RunOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_path = os.path.join(self.tmp.name, "state.json")
        self.out = os.path.join(self.tmp.name, "drafts")
        grid = parse_grid(REPORT)
        self.delivered, _ = delivered_events_from_grid(grid)
        self.accepted, _ = accepted_events_from_grid(grid, WAITING)
        self.since = datetime(2026, 10, 11, 12, 0)  # a 24 h window ending 10/12 12:00
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def log(self, message):
        self.logs.append(message)

    def test_accepted_trigger_drafts_new_dispatches(self):
        state = WatcherState(self.state_path)
        draft = fake_draft_factory({"344010": 4, "344030": 0})
        counts = run_once(self.accepted, state, draft, out_dir=self.out, since=self.since, log=self.log)
        self.assertEqual(counts["old"], 1)        # 343540 dispatched 10/10
        self.assertEqual(counts["drafted"], 1)    # Fresh Inc truck
        self.assertEqual(counts["no_picks"], 1)   # 344030
        self.assertEqual(draft.calls, ["344010", "344030"])
        rec = state.record("344010|fresh inc|new, driver")
        self.assertEqual(rec["outcome"], "drafted")
        self.assertEqual(rec["trigger"], "accepted")
        self.assertEqual(rec["loads"], ["344000", "344010"])
        self.assertTrue(any("Fresh Inc / NEW, DRIVER -> Dealer | Boston, MA: 4 picks" in m for m in self.logs))

    def test_not_accepted_is_retried_later(self):
        state = WatcherState(self.state_path)
        draft = fake_draft_factory({"344010": 2}, not_accepted=("344010",))
        counts = run_once(self.accepted, state, draft, out_dir=self.out, since=self.since, log=self.log)
        self.assertEqual(counts["waiting_esign"], 1)
        rec = state.record("344010|fresh inc|new, driver")
        self.assertEqual((rec["outcome"], rec["attempts"]), ("waiting_esign", 1))
        counts = run_once(self.accepted, state, draft, out_dir=self.out, since=self.since, log=self.log)
        self.assertEqual(counts["waiting_esign"], 1)
        self.assertEqual(draft.calls.count("344010"), 2)

    def test_seed_marks_everything_without_drafting(self):
        state = WatcherState(self.state_path)
        draft = fake_draft_factory({})
        counts = run_once(self.delivered, state, draft, out_dir=self.out, seed=True, log=self.log)
        self.assertEqual(counts["seeded"], 4)
        self.assertEqual(draft.calls, [])
        state.save()
        reloaded = WatcherState(self.state_path)
        self.assertFalse(reloaded.fresh)
        counts = run_once(self.delivered, reloaded, draft, out_dir=self.out, log=self.log)
        self.assertEqual(counts["already_handled"], 4)

    def test_window_filter_and_drafting(self):
        state = WatcherState(self.state_path)
        draft = fake_draft_factory({"343514": 3, "343800": 0, "343801": 2})
        counts = run_once(self.delivered, state, draft, out_dir=self.out, since=self.since, log=self.log)
        self.assertEqual(counts["old"], 1)
        self.assertEqual(counts["drafted"], 2)
        self.assertEqual(counts["no_picks"], 1)
        rec = state.record("343514|88 transpoort llc|padureanu, eugen")
        self.assertEqual(rec["outcome"], "drafted")
        self.assertTrue(rec["draft"].endswith("343514_88-transpoort-llc.txt"))
        with open(rec["json"], encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["event"]["loads"], ["343515", "343514"])
        self.assertNotIn("picks_objects", payload)
        counts = run_once(self.delivered, state, draft, out_dir=self.out, since=self.since, log=self.log)
        self.assertEqual(counts["already_handled"], 4)
        self.assertEqual(len(draft.calls), 3)

    def test_errors_are_retried_then_given_up(self):
        state = WatcherState(self.state_path)
        draft = fake_draft_factory({"343514": 2}, fail=("343514",))
        for _ in range(3):
            counts = run_once(self.delivered, state, draft, out_dir=self.out, since=self.since, max_attempts=3, log=self.log)
            self.assertEqual(counts["error"], 1)
        rec = state.record("343514|88 transpoort llc|padureanu, eugen")
        self.assertEqual((rec["outcome"], rec["attempts"]), ("error", 3))
        counts = run_once(self.delivered, state, draft, out_dir=self.out, since=self.since, max_attempts=3, log=self.log)
        self.assertEqual(counts["given_up"], 1)
        self.assertEqual(draft.calls.count("343514"), 3)
        self.assertEqual(len(draft.calls), 5)

    def test_dry_run_touches_nothing(self):
        state = WatcherState(self.state_path)
        counts = run_once(self.accepted, state, None, out_dir=self.out, since=self.since, dry_run=True, log=self.log)
        self.assertEqual(counts["new"], 2)
        self.assertEqual(counts["old"], 1)
        self.assertFalse(os.path.exists(self.out))
        self.assertTrue(any("new: Fresh Inc / NEW, DRIVER dispatched 2 load(s), 344010" in m for m in self.logs))

    def test_max_events_defers_the_rest(self):
        state = WatcherState(self.state_path)
        draft = fake_draft_factory({"343514": 1, "343800": 1, "343801": 1})
        counts = run_once(self.delivered, state, draft, out_dir=self.out, since=self.since, max_events=2, log=self.log)
        self.assertEqual((counts["drafted"], counts["deferred"]), (2, 1))
        counts = run_once(self.delivered, state, draft, out_dir=self.out, since=self.since, max_events=2, log=self.log)
        self.assertEqual((counts["drafted"], counts["already_handled"]), (1, 3))

    def test_same_truck_new_load_is_a_new_event(self):
        state = WatcherState(self.state_path)
        draft = fake_draft_factory({"344010": 1, "344099": 1})
        run_once(self.accepted, state, draft, out_dir=self.out, since=self.since, log=self.log)
        later = [TruckEvent("Fresh Inc", "NEW, DRIVER", "344099", "accepted", "10/12/2026 13:00", loads=["344000", "344010", "344099"])]
        counts = run_once(later, state, draft, out_dir=self.out, since=self.since, log=self.log)
        self.assertEqual(counts["drafted"], 1)
        self.assertEqual(draft.calls[-1], "344099")


if __name__ == "__main__":
    unittest.main()
