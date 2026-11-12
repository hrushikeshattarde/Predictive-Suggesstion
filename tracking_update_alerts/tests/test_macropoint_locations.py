"""Unit tests for the MacroPoint location step (macropoint_locations.py and its use in
run_once / main / the Lambda handler). No network: the screen is the synthetic grid of
test_tracking_update_alerts, the pings are receiver-shaped JSON records, and CargoTel's zip
service and Location/ETA form are a fake session."""

import json
import os
import shutil
import tempfile
import unittest
from datetime import timedelta, timezone

from tests.test_tracking_update_alerts import GPS, NOW, PAGE, REPS, FakePoster, FakeTransport, fetch_gps  # noqa: E402

from cargotel_search import parse_grid  # noqa: E402
from macropoint_locations import LocationState, LocationWriter, MpPing, load_pings, mp_comment_text, plan_locations  # noqa: E402
from tracking_update_alerts import AlertState, CommentState, main, run_once, screen_loads  # noqa: E402
import tracking_update_lambda  # noqa: E402

UTC = timezone.utc


def ping(load_id, minutes_ago=30, city="Forsyth", state="GA", postal="31029", locator="", source="Ping", now=NOW):
    return MpPing(load_id=load_id, when=(now - timedelta(minutes=minutes_ago)).astimezone(UTC), city=city, state=state,
                  postal=postal, lat=33.03, lng=-83.94, locator=locator, source=source)


def record(p):
    return {"load_id": p.load_id, "latest": {"load_id": p.load_id, "when_utc": p.stamp, "city": p.city, "state": p.state,
                                             "postal": p.postal, "lat": p.lat, "lng": p.lng, "locator": p.locator, "data_source": p.source},
            "history": []}


#: 352242 Dispatched, phone (312) 714-5408, matching locator; 360011 In-Route, (208) 555-0199;
#: 360001 MacroPoint tracks another phone; 360003 stale; 999999 not on the screen.
PINGS = {
    "352242": ping("352242", 30, locator="3127145408"),
    "360011": ping("360011", 10, city="Cheyenne", state="WY", postal="82001", locator="1-208-555-0199"),
    "360001": ping("360001", 20, city="Columbia", state="MO", locator="9999999999"),
    "360003": ping("360003", 300, city="Ogden", state="UT"),
    "999999": ping("999999", 5),
}


class FakeSession:
    ZIPS = {"Forsyth, GA": "31029 (Forsyth, GA)", "Cheyenne, WY": "82001 (Cheyenne, WY)", "Macon, GA": "31201 (Macon, GA)",
            "82001": "82001 (Cheyenne, WY)"}

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
        self.calls.append([p.load_id for p in plans])
        return {p.load_id: (p.load_id not in self.fail, f"zip ({p.ping.place})") for p in plans}


def candidates():
    return screen_loads(parse_grid(PAGE), statuses=("Dispatched", "In-Route"))


class PlanTests(unittest.TestCase):
    def test_screen_loads_take_both_statuses_without_the_eta_rule(self):
        ids = [l.load_id for l in candidates()]
        self.assertIn("360008", ids)  # In-Route
        self.assertIn("360011", ids)  # In-Route
        self.assertIn("360004", ids)  # pickup ETA on the cutoff day: still a load to locate
        self.assertNotIn("360007", ids)  # bundle member
        merchants = screen_loads(parse_grid(PAGE), statuses=("Dispatched",), exclude_customers=("Kingbee",))
        self.assertNotIn("360002", [l.load_id for l in merchants])

    def test_plan(self):
        plans, skipped = plan_locations(candidates(), PINGS, LocationState(None), NOW)
        self.assertEqual([(p.load_id, p.reason, p.status) for p in plans], [("352242", "first", "Dispatched"), ("360011", "first", "In-Route")])
        self.assertEqual(skipped, {"360001": "MacroPoint tracks 9999999999, the CargoTel driver cell is 8165550101",
                                   "360003": "newest ping is 5.0 h old"})
        plans, skipped = plan_locations(candidates(), PINGS, LocationState(None), NOW, match_phone=False, max_age_hours=6)
        self.assertEqual(sorted(p.load_id for p in plans), ["352242", "360001", "360003", "360011"])

    def test_truck_locators_and_missing_driver_phones_pass(self):
        pings = {"352242": ping("352242", locator="TRK 2028", source="Telematics"), "360002": ping("360002", locator="5551112222")}
        plans, skipped = plan_locations(candidates(), pings, LocationState(None), NOW)
        self.assertEqual(sorted(p.load_id for p in plans), ["352242", "360002"])  # 360002 has no driver phone in CargoTel

    def test_already_written_same_place_and_moved(self):
        state = LocationState(None)
        state.mark("352242", PINGS["352242"], "31029 (Forsyth, GA)", NOW)
        plans, skipped = plan_locations(candidates(), {"352242": PINGS["352242"]}, state, NOW)
        self.assertEqual((plans, skipped), ([], {"352242": "this ping is already written"}))
        newer_same = {"352242": ping("352242", 5, locator="3127145408")}
        plans, skipped = plan_locations(candidates(), newer_same, state, NOW + timedelta(hours=1))
        self.assertEqual(skipped["352242"], "still at Forsyth, GA, written 1.0 h ago")
        plans, _s = plan_locations(candidates(), {"352242": ping("352242", 5, locator="3127145408", now=NOW + timedelta(hours=4))}, state,
                                   NOW + timedelta(hours=4))
        self.assertEqual([(p.load_id, p.reason) for p in plans], [("352242", "refresh")])
        moved = {"352242": ping("352242", 5, city="Macon", locator="3127145408", now=NOW + timedelta(hours=1))}
        plans, _s = plan_locations(candidates(), moved, state, NOW + timedelta(hours=1))
        self.assertEqual([(p.load_id, p.reason) for p in plans], [("352242", "moved")])

    def test_odd_pings(self):
        no_time = MpPing("352242", None, "Forsyth", "GA", "", None, None, "", "Ping")
        future = ping("360011", -60)
        no_place = ping("360001", city="", state="", postal="")
        plans, skipped = plan_locations(candidates(), {"352242": no_time, "360011": future, "360001": no_place}, LocationState(None), NOW)
        self.assertEqual(plans, [])
        self.assertEqual(skipped, {"352242": "ping has no time", "360011": "ping time is in the future",
                                   "360001": "ping has no city, state or zip"})
        self.assertEqual(ping("1", city="FORSYTH", state="ga").place, "Forsyth, GA")
        self.assertEqual(ping("1", city="", state="", postal="31029").place, "31029")


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
        self.assertEqual(pings["360011"].when, PINGS["360011"].when)
        self.assertEqual(load_pings(os.path.join(self.tmp, "missing")), {})
        path = os.path.join(self.tmp, "state", "location_state.json")
        state = LocationState(path)
        state.mark("352242", PINGS["352242"], "31029 (Forsyth, GA)", NOW)
        state.mark("360011", PINGS["360011"], "x", NOW - timedelta(days=40))
        state.prune(NOW)
        state.save()
        self.assertEqual(sorted(LocationState(path).written), ["352242"])


class WriterTests(unittest.TestCase):
    def test_writes_like_the_load_page(self):
        session = FakeSession(reject_form={"360011"})
        writer = LocationWriter(session, "https://circle.cargotel.com")
        plans, _s = plan_locations(candidates(), PINGS, LocationState(None), NOW)
        results = writer.apply(plans)
        self.assertEqual(results["352242"], (True, "31029 (Forsyth, GA)"))
        self.assertFalse(results["360011"][0])
        self.assertIn("An Error was encountered", results["360011"][1])
        form = [params for url, params in session.calls if url.endswith("/cargotel/db.mcgi")][0]
        self.assertEqual(form, {"db__location": "31029 (Forsyth, GA)", "db__month_eta": "", "db__day_eta": "", "db__year_eta": "",
                                "db__hour_eta": "", "db__minute_eta": "", "load_id": "352242", "mode": "location", "closex": "",
                                "reloadx": "1"})

    def test_unknown_place_falls_back_to_the_zip_then_gives_up(self):
        session = FakeSession()
        writer = LocationWriter(session, "https://circle.cargotel.com/")
        self.assertEqual(writer.value_for(ping("1", city="Cheyenne Wells", state="WY", postal="82001")), "82001 (Cheyenne, WY)")
        self.assertIsNone(writer.value_for(ping("1", city="Nowhere", state="ZZ", postal="")))
        plans = plan_locations(candidates(), {"352242": ping("352242", city="Nowhere", state="ZZ", postal="", locator="3127145408")},
                               LocationState(None), NOW)[0]
        self.assertEqual(writer.apply(plans), {"352242": (False, "CargoTel does not know the place 'Nowhere, ZZ'")})
        self.assertFalse(any(url.endswith("/cargotel/db.mcgi") for url, _p in session.calls))
        writer.value_for(ping("1"))
        writer.value_for(ping("2"))
        self.assertEqual(sum(1 for url, p in session.calls if p.get("query") == "Forsyth, GA"), 1)  # cached per run


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.outbox = os.path.join(self.tmp, "outbox")
        self.state = AlertState(os.path.join(self.tmp, "alert_state.json"))
        self.comment_state = CommentState(os.path.join(self.tmp, "comment_state.json"))
        self.location_state = LocationState(os.path.join(self.tmp, "location_state.json"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_once(self, **kw):
        transport, poster = FakeTransport(), FakePoster()
        kw.setdefault("now", NOW)
        summary = run_once(PAGE, parse_grid(PAGE), reps=REPS, gps_fetch=fetch_gps, state=self.state, comment_state=self.comment_state,
                           transport=transport, poster=poster, from_addr="Circle <vehicles@circledelivers.com>", record=True,
                           post_comments=True, sent_log=None, outbox=self.outbox, send_alerts=False, report_hours=(),
                           location_state=self.location_state, **kw)
        return summary, poster

    def test_writes_remembers_and_comes_before_the_comments(self):
        order = []

        class Writer(FakeWriter):
            def apply(self, plans):
                order.append("locations")
                return super().apply(plans)

        class Poster(FakePoster):
            def post(self, page_html, comments):
                order.append("comments")
                return super().post(page_html, comments)

        writer = Writer(fail={"360011"})
        transport = FakeTransport()
        summary = run_once(PAGE, parse_grid(PAGE), now=NOW, reps=REPS, gps_fetch=fetch_gps, state=self.state, comment_state=self.comment_state,
                           transport=transport, poster=Poster(), from_addr="x", record=True, post_comments=True, sent_log=None,
                           outbox=self.outbox, send_alerts=False, report_hours=(), mp_pings=PINGS, location_state=self.location_state,
                           location_writer=writer, post_locations=True, checkpoint=lambda: order.append("checkpoint"))
        self.assertEqual(order, ["checkpoint", "locations", "checkpoint", "comments"])
        loc = summary["locations"]
        self.assertEqual((loc["on_macropoint"], loc["planned"], loc["written"], loc["failed"]), (4, 2, 1, 1))
        self.assertEqual(sorted(loc["skipped"]), ["360001", "360003"])
        self.assertEqual(loc["writes"]["352242"]["written"], True)
        self.assertEqual(sorted(self.location_state.written), ["352242"])  # the failed one is retried next run
        self.assertTrue(os.path.exists(self.location_state.path))
        # an hour later, same pings: 352242 is done, 360011 is tried again
        summary2, _p = self.run_once(now=NOW + timedelta(minutes=50), mp_pings=PINGS, location_writer=writer, post_locations=True)
        self.assertEqual(writer.calls[-1], ["360011"])
        self.assertEqual(summary2["locations"]["skipped"]["352242"], "this ping is already written")

    def test_dry_run_lists_the_writes_in_the_outbox(self):
        writer = FakeWriter()
        summary, _p = self.run_once(mp_pings=PINGS, location_writer=writer, post_locations=False)
        self.assertEqual(writer.calls, [])
        self.assertEqual(summary["locations"]["planned"], 2)
        files = [f for f in os.listdir(self.outbox) if f.endswith("_locations.txt")]
        self.assertEqual(len(files), 1)
        with open(os.path.join(self.outbox, files[0]), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("352242 | Dispatched | Forsyth, GA | ping", text)
        self.assertIn("360011 | In-Route | Cheyenne, WY | ping", text)
        self.assertEqual(self.location_state.written, {})

    def test_off_without_pings_and_limited_by_only_loads(self):
        summary, _p = self.run_once()
        self.assertEqual(summary["locations"]["planned"], 0)
        writer = FakeWriter()
        summary, _p = self.run_once(mp_pings=PINGS, location_writer=writer, post_locations=True, only_loads={"360011", "352243"})
        self.assertEqual(writer.calls, [["360011"]])


class CommentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_text(self):
        self.assertEqual(mp_comment_text(PINGS["352242"], NOW.tzinfo), "2026-11-06 08:30:00-05 Forsyth, GA (MacroPoint)")
        self.assertEqual(mp_comment_text(MpPing("1", None, "Macon", "GA", "", None, None, "", ""), NOW.tzinfo), "Macon, GA (MacroPoint)")

    def run_with(self, writer, **kw):
        poster = FakePoster()
        summary = run_once(PAGE, parse_grid(PAGE), now=NOW, reps=REPS, gps_fetch=fetch_gps,
                           state=AlertState(os.path.join(self.tmp, "a.json")), comment_state=CommentState(os.path.join(self.tmp, "c.json")),
                           transport=FakeTransport(), poster=poster, from_addr="x", record=True, post_comments=True, sent_log=None,
                           outbox=os.path.join(self.tmp, "outbox"), send_alerts=False, report_hours=(), mp_pings=PINGS,
                           location_state=LocationState(os.path.join(self.tmp, "l.json")), location_writer=writer, post_locations=True, **kw)
        return summary, poster

    def test_written_places_are_also_commented(self):
        summary, poster = self.run_with(FakeWriter(fail={"360011"}))
        posted = poster.calls[0]
        # 352242 has a new driver-app ping too: both go in one comment
        self.assertEqual(posted["352242"], "2026-11-06 06:12:25-05 Oak Lawn, IL | 2026-11-06 08:30:00-05 Forsyth, GA (MacroPoint)")
        self.assertNotIn("360011", posted)  # its Location/ETA write failed: no comment either
        self.assertEqual(summary["locations"]["comments"], 1)
        state = CommentState(os.path.join(self.tmp, "c.json"))
        self.assertEqual(state.written("352242"), "2026-11-06 06:12:25-05")  # the driver-app ping is what the comment state tracks

    def test_macropoint_only_load_and_switch_off(self):
        summary, poster = self.run_with(FakeWriter())
        self.assertEqual(poster.calls[0]["360011"], "2026-11-06 08:50:00-05 Cheyenne, WY (MacroPoint)")  # In-Route, no driver-app comment
        self.assertIsNone(CommentState(os.path.join(self.tmp, "c.json")).written("360011"))
        shutil.rmtree(self.tmp)
        os.makedirs(self.tmp)
        summary, poster = self.run_with(FakeWriter(), mp_comments=False)
        self.assertNotIn("360011", poster.calls[0])
        self.assertEqual(poster.calls[0]["352242"], "2026-11-06 06:12:25-05 Oak Lawn, IL")
        self.assertEqual(summary["locations"]["written"], 2)


class LambdaAndCliTests(unittest.TestCase):
    def test_lambda_args(self):
        args = tracking_update_lambda.alert_args("/w", {})
        self.assertEqual(args[args.index("--macropoint-dir") + 1], os.path.join("/w", "macropoint"))
        self.assertEqual(args[args.index("--location-state") + 1], os.path.join("/w", "state", "location_state.json"))
        self.assertNotIn("--post-locations", args)
        self.assertNotIn("--mp-no-phone-match", args)
        args = tracking_update_lambda.alert_args("/w", {"TRACK_POST_LOCATIONS": "1", "TRACK_MP_MAX_AGE_HOURS": "2",
                                                        "TRACK_MP_SAME_PLACE_HOURS": "6", "TRACK_MP_PHONE_MATCH": "0"})
        self.assertIn("--post-locations", args)
        self.assertEqual(args[args.index("--mp-max-age-hours") + 1], "2")
        self.assertEqual(args[args.index("--mp-same-place-hours") + 1], "6")
        self.assertIn("--mp-no-phone-match", args)
        self.assertNotIn("--macropoint-dir", tracking_update_lambda.alert_args("/w", {"TRACK_MACROPOINT_PREFIX": "-"}))
        self.assertNotIn("--no-mp-comments", args)
        self.assertIn("--no-mp-comments", tracking_update_lambda.alert_args("/w", {"TRACK_MP_COMMENTS": "0"}))

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
            self.assertEqual(data["locations"]["planned"], 2)
            self.assertEqual(sorted(data["locations"]["writes"]), ["352242", "360011"])
            self.assertTrue(any(f.endswith("_locations.txt") for f in os.listdir(os.path.join(tmp, "outbox"))))
            self.assertFalse(os.path.exists(os.path.join(tmp, "l.json")))  # dry run: nothing remembered
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
