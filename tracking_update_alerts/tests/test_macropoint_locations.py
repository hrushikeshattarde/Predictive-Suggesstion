"""Unit tests for the location step (macropoint_locations.py and its use in run_once / main /
the Lambda handler). The rule (the user's, 2026-11-12): a load with any ping on its GPS City/St
link gets that last ping in Location/ETA and as a comment, MacroPoint not looked at; a load
with no ping there at all gets MacroPoint's latest location in Location/ETA and a comment
labelled (MacroPoint). No network: the screen is the synthetic grid of
test_tracking_update_alerts (its GPS pages decide which loads have GPS-link pings), the
MacroPoint pings are receiver-shaped records, and CargoTel's zip service and Location/ETA form
are a fake session.

GPS-link state of the fixture at NOW (09:00 EST, 2026-11-06): pinging or quiet (has pings):
352242, 352243, 360001, 360003, 360006, 360010 (Dispatched) and 360011 (In-Route); no ping at
all: 354745, 360002 (Dispatched) and 360008 (In-Route); unreadable: 360009."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta, timezone

from tests.test_tracking_update_alerts import GPS, NOW, PAGE, REPS, FakePoster, FakeTransport, fetch_gps  # noqa: E402

from cargotel_search import parse_grid  # noqa: E402
from macropoint_locations import LocationState, LocationWriter, MpPing, load_pings, mp_comment_text, plan_locations  # noqa: E402
from tracking_update_alerts import (  # noqa: E402
    AlertState,
    CommentState,
    check_gps,
    main,
    plan_cargotel_locations,
    run_once,
    screen_loads,
    select_loads,
)
import tracking_update_lambda  # noqa: E402

UTC = timezone.utc


def ping(load_id, minutes_ago=30, city="Forsyth", state="GA", postal="", locator="", source="Ping", now=NOW):
    return MpPing(load_id=load_id, when=(now - timedelta(minutes=minutes_ago)).astimezone(UTC), city=city, state=state,
                  postal=postal, lat=33.03, lng=-83.94, locator=locator, source=source)


def record(p):
    return {"load_id": p.load_id, "latest": {"load_id": p.load_id, "when_utc": p.stamp, "city": p.city, "state": p.state,
                                             "postal": p.postal, "lat": p.lat, "lng": p.lng, "locator": p.locator, "data_source": p.source},
            "history": []}


#: MacroPoint has: 354745 and 360002 (no GPS-link ping: used), 360008 (no GPS-link ping, but tracks
#: another phone: skipped), 352242 (has GPS-link pings: ignored), 999999 (not on the screen).
PINGS = {
    "354745": ping("354745", 20, city="Macon", state="GA", locator="2242849908"),
    "360002": ping("360002", 15, city="Provo", state="UT"),
    "360008": ping("360008", 10, city="Toledo", state="OH", locator="9999999999"),
    "352242": ping("352242", 5, city="Joliet", state="IL", locator="3127145408"),
    "999999": ping("999999", 5),
}
#: Location/ETA writes of a first live run: (load, source, place).
FIRST_RUN = [("360010", "cargotel", "Gary, IN"), ("352242", "cargotel", "Oak Lawn, IL"), ("352243", "cargotel", "Rockford, IL"),
             ("360006", "cargotel", "Joliet, IL"), ("360001", "cargotel", "Columbia, MO"), ("360003", "cargotel", "Ogden, UT"),
             ("360011", "cargotel", "Cheyenne, WY"), ("354745", "macropoint", "Macon, GA"), ("360002", "macropoint", "Provo, UT")]


class FakeSession:
    ZIPS = {"Forsyth, GA": "31029 (Forsyth, GA)", "Cheyenne, WY": "82001 (Cheyenne, WY)", "Macon, GA": "31201 (Macon, GA)",
            "82001": "82001 (Cheyenne, WY)", "Oak Lawn, IL": "60453 (Oak Lawn, IL)"}

    def __init__(self, reject_form=()):
        self.calls = []
        self.reject_form = set(reject_form)

    def get(self, url, params=None):
        self.calls.append((url, dict(params or {})))
        if url.endswith("/cargotel/zipService.mcgi"):
            q = params["query"]
            return 200, self.ZIPS.get(q, "*" + q)
        if url.endswith("/cargotel/db.mcgi"):
            if params["load_id"] in self.reject_form:
                return 200, "<html><body>An Error was encountered: bad location</body></html>"
            return 200, "<html><head><title>CargoTel</title></head><body>Location Updated <h3>Location / Eta</h3></body></html>"
        return 404, ""


class FakeWriter:
    def __init__(self, fail=()):
        self.calls = []
        self.fail = set(fail)

    def apply(self, plans):
        self.calls.append([(p.load_id, p.source, p.ping.place) for p in plans])
        return {p.load_id: (p.load_id not in self.fail, f"zip ({p.ping.place})") for p in plans}


def checked(statuses=("Dispatched", "In-Route")):
    loads = []
    for status in statuses:
        part, _w, _c = select_loads(parse_grid(PAGE), NOW, statuses=(status,))
        loads += part
    check_gps(loads, fetch_gps, NOW)
    return loads


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.outbox = os.path.join(self.tmp, "outbox")
        self.alert_state = AlertState(os.path.join(self.tmp, "alert_state.json"))
        self.comment_state = CommentState(os.path.join(self.tmp, "comment_state.json"))
        self.location_state = LocationState(os.path.join(self.tmp, "location_state.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_once(self, poster=None, **kw):
        poster = poster or FakePoster()
        kw.setdefault("now", NOW)
        kw.setdefault("post_comments", True)
        summary = run_once(PAGE, parse_grid(PAGE), reps=REPS, gps_fetch=fetch_gps, state=self.alert_state, comment_state=self.comment_state,
                           transport=FakeTransport(), poster=poster, from_addr="Circle <vehicles@circledelivers.com>", record=True,
                           sent_log=None, outbox=self.outbox, send_alerts=False, report_hours=(), location_state=self.location_state, **kw)
        return summary, poster


class RuleTests(Base):
    def test_gps_link_first_macropoint_only_for_loads_with_no_ping(self):
        order = []

        class Writer(FakeWriter):
            def apply(self, plans):
                order.append("locations")
                return super().apply(plans)

        class Poster(FakePoster):
            def post(self, page_html, comments):
                order.append("comments")
                return super().post(page_html, comments)

        writer = Writer()
        summary, poster = self.run_once(poster=Poster(), mp_pings=PINGS, location_writer=writer, post_locations=True,
                                        checkpoint=lambda: order.append("checkpoint"))
        self.assertEqual(order, ["checkpoint", "locations", "checkpoint", "comments"])
        self.assertEqual(writer.calls, [FIRST_RUN])  # 352242 from its GPS-link ping (Oak Lawn), not MacroPoint's Joliet
        loc = summary["locations"]
        self.assertEqual((loc["from_cargotel"], loc["from_macropoint"], loc["written"], loc["failed"]), (7, 2, 9, 0))
        self.assertEqual(loc["skipped"], {"360008": "MacroPoint tracks 9999999999, the CargoTel driver cell is 5862343689"})
        self.assertNotIn("360009", loc["writes"])  # GPS page unreadable: no location from either source
        self.assertEqual(poster.calls, [{
            "360010": "2026-11-06 01:00:00-05 Gary, IN", "352242": "2026-11-06 06:12:25-05 Oak Lawn, IL",
            "352243": "2026-11-05 14:00:00-05 Rockford, IL", "360001": "2026-11-06 04:30:00-05 Columbia, MO",
            "360003": "2026-11-03 12:00:00-05 Ogden, UT", "360011": "2026-11-06 08:45:00-05 Cheyenne, WY",
            "354745": "2026-11-06 08:40:00-05 Macon, GA (MacroPoint)", "360002": "2026-11-06 08:45:00-05 Provo, UT (MacroPoint)"}])
        # 360006: Location/ETA written, no comment (its last dispatch comment already carries that ping)
        self.assertEqual(self.location_state.get("360006")["source"], "cargotel")
        self.assertEqual(self.location_state.get("354745")["source"], "macropoint")
        self.assertEqual(self.comment_state.written("360011"), "2026-11-06 08:45:00-05")  # In-Route loads get comments now
        self.assertIsNone(self.comment_state.written("354745"))  # MacroPoint comments are not driver-app pings

    def test_next_runs_write_only_new_pings(self):
        writer = FakeWriter()
        self.run_once(mp_pings=PINGS, location_writer=writer, post_locations=True)
        summary, poster = self.run_once(mp_pings=PINGS, location_writer=writer, post_locations=True, now=NOW + timedelta(minutes=50))
        self.assertEqual(len(writer.calls), 1)  # nothing new: the writer is not even called
        self.assertEqual(summary["locations"]["written"], 0)
        self.assertEqual(poster.calls, [])
        newer = dict(PINGS, **{"354745": ping("354745", 5, city="Perry", state="GA", locator="2242849908", now=NOW + timedelta(hours=1))})
        summary, poster = self.run_once(mp_pings=newer, location_writer=writer, post_locations=True, now=NOW + timedelta(hours=1))
        self.assertEqual(writer.calls[-1], [("354745", "macropoint", "Perry, GA")])
        self.assertEqual(poster.calls, [{"354745": "2026-11-06 09:55:00-05 Perry, GA (MacroPoint)"}])

    def test_a_failed_write_is_retried_and_not_commented(self):
        writer = FakeWriter(fail={"360002", "352243"})
        summary, poster = self.run_once(mp_pings=PINGS, location_writer=writer, post_locations=True)
        self.assertEqual(summary["locations"]["failed"], 2)
        self.assertNotIn("360002", poster.calls[0])  # MacroPoint location not written: no (MacroPoint) comment
        self.assertIn("352243", poster.calls[0])  # a GPS-link ping keeps its comment, as before
        summary, _p = self.run_once(mp_pings=PINGS, location_writer=FakeWriter(), post_locations=True, now=NOW + timedelta(minutes=50))
        self.assertEqual(sorted(summary["locations"]["writes"]), ["352243", "360002"])

    def test_an_unknown_place_is_not_retried_every_hour(self):
        class Writer(FakeWriter):
            def apply(self, plans):
                self.calls.append([p.load_id for p in plans])
                return {p.load_id: (False, f"CargoTel does not know the place {p.ping.place!r}") if p.load_id == "360003"
                        else (True, "zip") for p in plans}

        writer = Writer()
        summary, _p = self.run_once(mp_pings=PINGS, location_writer=writer, post_locations=True)
        self.assertEqual(summary["locations"]["failed"], 1)
        summary, _p = self.run_once(mp_pings=PINGS, location_writer=writer, post_locations=True, now=NOW + timedelta(minutes=50))
        self.assertEqual(len(writer.calls), 1)  # the same ping is not tried again

    def test_dry_run(self):
        writer = FakeWriter()
        summary, poster = self.run_once(mp_pings=PINGS, location_writer=writer, post_locations=False, post_comments=False)
        self.assertEqual(writer.calls, [])
        self.assertEqual(summary["locations"]["planned"], 9)
        files = os.listdir(self.outbox)
        with open(os.path.join(self.outbox, [f for f in files if f.endswith("_locations.txt")][0]), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("352242 | Dispatched | cargotel | Oak Lawn, IL | ping", text)
        self.assertIn("360011 | In-Route | cargotel | Cheyenne, WY | ping", text)
        self.assertIn("354745 | Dispatched | macropoint | Macon, GA | ping", text)
        with open(os.path.join(self.outbox, [f for f in files if f.endswith("_comments.txt")][0]), encoding="utf-8") as handle:
            self.assertIn("354745 | 2026-11-06 08:40:00-05 Macon, GA (MacroPoint)", handle.read())
        self.assertEqual(self.location_state.written, {})

    def test_locations_dry_comments_live_posts_no_macropoint_comment(self):
        summary, poster = self.run_once(mp_pings=PINGS, location_writer=FakeWriter(), post_locations=False)
        self.assertNotIn("354745", poster.calls[0])
        self.assertIn("352242", poster.calls[0])

    def test_switch_off_macropoint_comments(self):
        summary, poster = self.run_once(mp_pings=PINGS, location_writer=FakeWriter(), post_locations=True, mp_comments=False)
        self.assertEqual(summary["locations"]["from_macropoint"], 2)
        self.assertNotIn("354745", poster.calls[0])

    def test_off_without_the_location_step(self):
        summary, poster = self.run_once()
        self.assertEqual(summary["locations"]["planned"], 0)
        self.assertEqual(summary["report_loads"], [])  # In-Route loads not read outside report hours
        self.assertNotIn("360011", poster.calls[0])  # and not commented: the behaviour before the location step

    def test_only_loads(self):
        writer = FakeWriter()
        self.run_once(mp_pings=PINGS, location_writer=writer, post_locations=True, only_loads={"360002", "352243"})
        self.assertEqual(writer.calls, [[("352243", "cargotel", "Rockford, IL"), ("360002", "macropoint", "Provo, UT")]])


class CargoTelPlanTests(unittest.TestCase):
    def test_new_pings_only_across_sources(self):
        loads = {l.load_id: l for l in checked()}
        state = LocationState(None)
        plans = plan_cargotel_locations(list(loads.values()), state)
        self.assertEqual(sorted(p.load_id for p in plans), ["352242", "352243", "360001", "360003", "360006", "360010", "360011"])
        p = [x for x in plans if x.load_id == "352242"][0]
        self.assertEqual((p.ping.place, p.source, p.reason), ("Oak Lawn, IL", "cargotel", "first"))
        # written before from MacroPoint at an older time: the newer GPS-link ping is written
        state.mark("352242", ping("352242", 300, city="Joliet", state="IL"), "x", NOW, source="macropoint")
        # written before at the same or a newer time: nothing to do
        state.mark("352243", ping("352243", 60, city="Somewhere", state="IL"), "x", NOW, source="macropoint")
        plans = {x.load_id: x for x in plan_cargotel_locations(list(loads.values()), state)}
        self.assertEqual(plans["352242"].reason, "new ping")
        self.assertNotIn("352243", plans)


class MacroPointPlanTests(unittest.TestCase):
    def test_phone_age_and_place_rules(self):
        loads = screen_loads(parse_grid(PAGE), statuses=("Dispatched", "In-Route"))
        pings = {"352242": ping("352242", 300, locator="3127145408"), "360001": ping("360001", locator="9999999999"),
                 "360002": ping("360002", locator="5551112222"), "360006": ping("360006", locator="TRK 2028", source="Telematics")}
        plans, skipped = plan_locations(loads, pings, LocationState(None), NOW)
        self.assertEqual(sorted(p.load_id for p in plans), ["352242", "360002", "360006"])  # 5 h old is fine by default (48 h)
        self.assertEqual(skipped, {"360001": "MacroPoint tracks 9999999999, the CargoTel driver cell is 8165550101"})
        plans, skipped = plan_locations(loads, pings, LocationState(None), NOW, max_age_hours=3)
        self.assertEqual(skipped["352242"], "newest ping is 5.0 h old")
        state = LocationState(None)
        state.mark("360002", ping("360002", 120), "x", NOW - timedelta(minutes=30))
        plans, _s = plan_locations(loads, {"360002": ping("360002", 10)}, state, NOW)
        self.assertEqual([(p.load_id, p.reason) for p in plans], [("360002", "refresh")])  # same place, new ping: written
        plans, skipped = plan_locations(loads, {"360002": ping("360002", 10)}, state, NOW, same_place_hours=3)
        self.assertEqual(skipped["360002"], "still at Forsyth, GA, written 0.5 h ago")

    def test_odd_pings(self):
        loads = screen_loads(parse_grid(PAGE), statuses=("Dispatched", "In-Route"))
        no_time = MpPing("352242", None, "Forsyth", "GA", "", None, None, "", "Ping")
        plans, skipped = plan_locations(loads, {"352242": no_time, "360011": ping("360011", -60), "360001": ping("360001", city="", state="")},
                                        LocationState(None), NOW)
        self.assertEqual(plans, [])
        self.assertEqual(skipped, {"352242": "ping has no time", "360011": "ping time is in the future",
                                   "360001": "ping has no city, state or zip"})
        self.assertEqual(ping("1", city="FORSYTH", state="ga").place, "Forsyth, GA")
        self.assertEqual(mp_comment_text(PINGS["354745"], NOW.tzinfo), "2026-11-06 08:40:00-05 Macon, GA (MacroPoint)")


class FileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_load_pings_and_state_file(self):
        for p in PINGS.values():
            with open(os.path.join(self.tmp, f"{p.load_id}.json"), "w", encoding="utf-8") as handle:
                json.dump(record(p), handle)
        with open(os.path.join(self.tmp, "junk.json"), "w", encoding="utf-8") as handle:
            handle.write("not json")
        pings = load_pings(self.tmp)
        self.assertEqual(sorted(pings), sorted(PINGS))
        self.assertEqual(load_pings(os.path.join(self.tmp, "missing")), {})
        path = os.path.join(self.tmp, "state", "location_state.json")
        state = LocationState(path)
        state.mark("352242", PINGS["352242"], "60453 (Oak Lawn, IL)", NOW, source="cargotel")
        state.mark("360011", PINGS["360002"], "x", NOW - timedelta(days=40))
        state.prune(NOW)
        state.save()
        again = LocationState(path)
        self.assertEqual(sorted(again.written), ["352242"])
        self.assertEqual(again.get("352242")["source"], "cargotel")


class WriterTests(unittest.TestCase):
    def test_writes_like_the_load_page(self):
        session = FakeSession(reject_form={"360002"})
        writer = LocationWriter(session, "https://circle.cargotel.com")
        plans = plan_cargotel_locations([l for l in checked() if l.load_id == "352242"], LocationState(None))
        plans += plan_locations(screen_loads(parse_grid(PAGE), statuses=("Dispatched",)), {"360002": ping("360002")}, LocationState(None), NOW)[0]
        results = writer.apply(plans)
        self.assertEqual(results["352242"], (True, "60453 (Oak Lawn, IL)"))
        self.assertFalse(results["360002"][0])
        form = [params for url, params in session.calls if url.endswith("/cargotel/db.mcgi")][0]
        self.assertEqual(form, {"db__location": "60453 (Oak Lawn, IL)", "db__month_eta": "", "db__day_eta": "", "db__year_eta": "",
                                "db__hour_eta": "", "db__minute_eta": "", "load_id": "352242", "mode": "location", "closex": "",
                                "reloadx": "1"})

    def test_unknown_place_falls_back_to_the_zip_then_gives_up(self):
        session = FakeSession()
        writer = LocationWriter(session, "https://circle.cargotel.com/")
        self.assertEqual(writer.value_for(ping("1", city="Cheyenne Wells", state="WY", postal="82001")), "82001 (Cheyenne, WY)")
        self.assertIsNone(writer.value_for(ping("1", city="Nowhere", state="ZZ")))
        writer.value_for(ping("1"))
        writer.value_for(ping("2"))
        self.assertEqual(sum(1 for url, p in session.calls if p.get("query") == "Forsyth, GA"), 1)  # cached per run


class LambdaAndCliTests(unittest.TestCase):
    def test_lambda_args(self):
        args = tracking_update_lambda.alert_args("/w", {})
        self.assertEqual(args[args.index("--macropoint-dir") + 1], os.path.join("/w", "macropoint"))
        self.assertEqual(args[args.index("--location-state") + 1], os.path.join("/w", "state", "location_state.json"))
        self.assertNotIn("--post-locations", args)
        self.assertNotIn("--no-mp-comments", args)
        args = tracking_update_lambda.alert_args("/w", {"TRACK_POST_LOCATIONS": "1", "TRACK_MP_MAX_AGE_HOURS": "2",
                                                        "TRACK_MP_SAME_PLACE_HOURS": "6", "TRACK_MP_PHONE_MATCH": "0", "TRACK_MP_COMMENTS": "0"})
        self.assertIn("--post-locations", args)
        self.assertEqual(args[args.index("--mp-max-age-hours") + 1], "2")
        self.assertEqual(args[args.index("--mp-same-place-hours") + 1], "6")
        self.assertIn("--mp-no-phone-match", args)
        self.assertIn("--no-mp-comments", args)
        self.assertNotIn("--macropoint-dir", tracking_update_lambda.alert_args("/w", {"TRACK_MACROPOINT_PREFIX": "-"}))

    def test_cli_dry_run_offline(self):
        tmp = tempfile.mkdtemp()
        try:
            page = os.path.join(tmp, "grid.html")
            with open(page, "w", encoding="utf-8") as handle:
                handle.write(PAGE)
            gps = os.path.join(tmp, "gps")
            os.makedirs(gps)
            for load_id, html in GPS.items():
                with open(os.path.join(gps, f"gps_{load_id}.html"), "w", encoding="utf-8") as handle:
                    handle.write(html)
            mp = os.path.join(tmp, "mp")
            os.makedirs(mp)
            for p in PINGS.values():
                with open(os.path.join(mp, f"{p.load_id}.json"), "w", encoding="utf-8") as handle:
                    json.dump(record(p), handle)
            out = os.path.join(tmp, "run.json")
            rc = main(["--from-html", page, "--gps-dir", gps, "--now", NOW.isoformat(), "--outbox", os.path.join(tmp, "outbox"),
                       "--state", os.path.join(tmp, "a.json"), "--comment-state", os.path.join(tmp, "c.json"),
                       "--sent-log", os.path.join(tmp, "s.jsonl"), "--location-state", os.path.join(tmp, "l.json"),
                       "--reps", os.path.join(tmp, "none.csv"), "--report-hours", "never", "--no-alerts",
                       "--macropoint-dir", mp, "--json", out])
            self.assertEqual(rc, 0)
            with open(out, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual((data["locations"]["from_cargotel"], data["locations"]["from_macropoint"]), (7, 2))
            self.assertEqual(sorted(data["locations"]["writes"]), sorted(l for l, _s, _p in FIRST_RUN))
            self.assertFalse(os.path.exists(os.path.join(tmp, "l.json")))  # dry run: nothing remembered
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
