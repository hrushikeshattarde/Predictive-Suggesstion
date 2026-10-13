"""Unit tests for due_date_alerts.py (``python -m unittest`` from the due_date_alerts
folder). No network: the Spot Load Map is a synthetic page shaped like the live grid,
the load pages are a dict, and the transport records what it was given."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.dirname(HERE)
# This folder for due_date_alerts / alerts_lambda, the project folder for the shared CargoTel modules.
for path in (os.path.dirname(FOLDER), FOLDER):
    if path not in sys.path:
        sys.path.insert(0, path)

from cargotel_search import parse_grid  # noqa: E402
from due_date_alerts import (  # noqa: E402
    DEFAULT_CC,
    UNASSIGNED,
    AlertState,
    RepCache,
    RepGroup,
    address,
    attach_reps,
    compose,
    due_loads_from_grid,
    group_by_rep,
    hours_text,
    main,
    parse_due,
    redirect,
    run_once,
)
from suggest_reloads import DispatchedLoad  # noqa: E402

NOW = datetime(2026, 10, 13, 10, 0)

COLUMNS = [
    ("age_SLL_d_Inventory", "Age Inventory Date"), ("bundle", "Load"), ("load_id", "Order ID"),
    ("pickup", "PickUp Scheduled Date"), ("duedate", "Due Date"), ("origin", "Origin"), ("originzip", "Origin Zip"),
    ("destination", "Destination"), ("destinationzip", "Destination Zip"), ("vins", "Vins"), ("model", "Model"),
    ("problemsYN", "Problems (Y/N)"), ("consignorprice", "Price"), ("carrierpayedit", "Carrier Pay (Edit)"),
    ("bol_comments", "BOL Comments"), ("cons_ref_load", "PO Number"), ("carriername", "Carrier Name"),
]

HEADER = '<tr static="" id="det_header_row" class="unshadow dv_row"><th class="dv_cell dv_string dv_header oeheader" id="row_number_head">#</th>' + "".join(
    f'<th class="dv_cell dv_cell_{key} dv_header dv_header_{key} oeheader"><span class="dv_header_name">{label}</span></th>'
    for key, label in COLUMNS) + "</tr>"


def section(text):
    return f'<tr class="dv_section_header"><td colspan="18">{text}</td></tr>'


def row(n, load_id, due, *, link_class="LinkIndividual", pickup="10/10/2026 09:00", origin="Roberts International<br>Richmond Hill, GA",
        ozip="31324", dest="Budget Truck<br>North Charleston, SC", dzip="29406", vins="1HTMMMMM9KH043933", model="MA025",
        problems="No", price="1200.00", comments="", po="", carrier=""):
    values = ["3", load_id if link_class == "LinkBundleParent" else "Load 1 2 3 Or Existing", None, pickup, due, origin, ozip, dest, dzip,
              vins, model, problems, price, "", comments, po, carrier]
    cells = []
    for col, (key, _label) in enumerate(COLUMNS):
        if key == "load_id":
            body = (f'<A name="MapId_{load_id}" class="{link_class}" href="/backoffice/loadmaint.mcgi?load_id={load_id}">{load_id}</A>'
                    f'<input type=\'hidden\' name=\'load_id\' value="{load_id}">')
        else:
            body = values[col]
        cells.append(f'<td class="dv_cell listable_cell dv_plain dv_cell_{key}" id="DetCell_{n}_{col}">{body}</td>')
    return f'<tr id="det_row_{n}"><td class="dv_linenumber">{n}</td>' + "".join(cells) + "</tr>"


PAGE = "<html><head><title>Spot Load Map</title></head><body><table>" + HEADER + "".join([
    section("Customer: KINGBEE RENTALS, LLC"),
    row(1, "343502", "10/09/2026 00:00"),                                   # 4 days past due, Victoria
    row(2, "344435", "10/15/2026 00:00", vins="1A<br>1B", model="A | B"),    # 38 h left, Victoria (+ Victoria as sales2)
    section("Customer: AVIS TRUCKS"),
    row(3, "343965", "10/15/2026 14:29 *Calculated"),                       # 52.5 h: outside the window
    row(4, "342271", "10/14/2026 15:20 *Calculated", link_class="LinkBundleParent", vins="V1<br>V2<br>V3", model="SAVANA"),  # 29 h, Kyle
    row(5, "999999", "10/13/2026 12:00", link_class="LinkBundleChild"),     # bundle member: skipped
    section("Customer: PRITCHARD AUTO"),
    row(6, "341217", "10/13/2026 13:02 *Calculated", problems="Yes"),       # 3 h, no sales rep
    row(7, "341205", "10/12/2026 12:55 *Calculated"),                       # past due, Podge (no email)
    row(8, "341300", ""),                                                   # no due date: warning
]) + "</table><div>Rows 1 - 8 of 8</div></body></html>"

LOAD_PAGES = {
    "343502": DispatchedLoad(load_id="343502", sales_rep="STORM, VICTORIA", dispatch_rep="STORM, VICTORIA"),
    "344435": DispatchedLoad(load_id="344435", sales_rep="STORM, VICTORIA", sales_rep2="STORM, VICTORIA"),
    "342271": DispatchedLoad(load_id="342271", sales_rep="LINK, KYLE", sales_rep2="GIBSON, JACK", dispatch_rep="LINK, KYLE"),
    "341217": DispatchedLoad(load_id="341217", sales_rep=None, dispatch_rep="ORMSBY, PODGE"),
    "341205": DispatchedLoad(load_id="341205", sales_rep="ORMSBY, PODGE", dispatch_rep="EDMONDSON, SETH"),
}

REPS = {
    "victoria storm": {"email": "victoria.storm@circledelivers.com", "title": "Senior Account Manager", "phone": ""},
    "kyle link": {"email": "kyle.link@circledelivers.com", "title": "", "phone": ""},
    "jack gibson": {"email": "jack.gibson@circledelivers.com", "title": "", "phone": ""},
    "justice cromwell": {"email": "justice.cromwell@circledelivers.com", "title": "", "phone": ""},
}
CC = list(DEFAULT_CC)


def lookup(load_id):
    try:
        return LOAD_PAGES[load_id]
    except KeyError:
        raise RuntimeError(f"no page for {load_id}")


class FakeTransport:
    def __init__(self, fail_for=None):
        self.sent = []
        self.fail_for = fail_for

    def send(self, email, from_addr):
        if self.fail_for and email.rep == self.fail_for:
            raise RuntimeError("SES down")
        self.sent.append((email, from_addr))
        return f"msg-{len(self.sent)}"


class ParseTests(unittest.TestCase):
    def test_parse_due_variants(self):
        self.assertEqual(parse_due("10/16/2026 11:33 *Calculated"), (datetime(2026, 10, 16, 11, 33), True))
        self.assertEqual(parse_due("10/16/2026 00:00"), (datetime(2026, 10, 16, 0, 0), False))
        self.assertEqual(parse_due("10/16/2026"), (datetime(2026, 10, 16, 0, 0), False))
        self.assertEqual(parse_due(""), (None, False))
        self.assertEqual(parse_due(None), (None, False))
        self.assertEqual(parse_due("13/45/2026 00:00"), (None, False))
        self.assertEqual(parse_due("soon"), (None, False))

    def test_hours_text(self):
        self.assertEqual(hours_text(31.2), "1 d 7 h left")
        self.assertEqual(hours_text(23.6), "1 d left")
        self.assertEqual(hours_text(5.4), "5 h left")
        self.assertEqual(hours_text(-50.5), "2 d 3 h past due")
        self.assertEqual(hours_text(0.2), "due now")
        self.assertEqual(hours_text(48), "2 d left")


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.grid = parse_grid(PAGE)

    def test_window_order_and_warnings(self):
        due, warnings = due_loads_from_grid(self.grid, NOW, 48)
        self.assertEqual([l.load_id for l in due], ["343502", "341205", "341217", "342271", "344435"])
        self.assertEqual(len(warnings), 1)
        self.assertIn("341300", warnings[0])
        by_id = {l.load_id: l for l in due}
        self.assertTrue(by_id["343502"].is_overdue(NOW))
        self.assertAlmostEqual(by_id["344435"].hours_left(NOW), 38.0)
        self.assertEqual(by_id["342271"].kind, "load")
        self.assertEqual(by_id["342271"].units, 3)  # from the VIN lines when there is no load hash
        self.assertEqual(by_id["344435"].kind, "order")
        self.assertEqual(by_id["344435"].customer, "KINGBEE RENTALS, LLC")
        self.assertEqual(by_id["342271"].customer, "AVIS TRUCKS")
        self.assertTrue(by_id["342271"].due_calculated)
        self.assertFalse(by_id["344435"].due_calculated)
        self.assertEqual(by_id["343502"].lane, "Richmond Hill, GA 31324 -> North Charleston, SC 29406")
        self.assertEqual(by_id["343502"].url, "https://circle.cargotel.com/backoffice/loadmaint.mcgi?load_id=343502")

    def test_overdue_can_be_left_out_and_window_is_adjustable(self):
        due, _ = due_loads_from_grid(self.grid, NOW, 48, include_overdue=False)
        self.assertEqual([l.load_id for l in due], ["341217", "342271", "344435"])
        due, _ = due_loads_from_grid(self.grid, NOW, 72)
        self.assertIn("343965", [l.load_id for l in due])


class RepTests(unittest.TestCase):
    def test_attach_reps_uses_cache_then_pages_and_survives_a_bad_page(self):
        due, _ = due_loads_from_grid(parse_grid(PAGE), NOW, 48)
        calls = []

        def counting(load_id):
            calls.append(load_id)
            return lookup(load_id)

        with tempfile.TemporaryDirectory() as tmp:
            cache = RepCache(os.path.join(tmp, "rep_cache.json"), 12)
            cache.put("343502", DispatchedLoad(load_id="343502", sales_rep="LINK, KYLE"), NOW - timedelta(hours=1))   # fresh
            cache.put("344435", DispatchedLoad(load_id="344435", sales_rep="HANEY, ANGELICA"), NOW - timedelta(hours=13))  # stale
            errors = attach_reps(due, counting, cache, NOW)
            self.assertEqual(errors, [])
            self.assertEqual(sorted(calls), ["341205", "341217", "342271", "344435"])
            by_id = {l.load_id: l for l in due}
            self.assertEqual(by_id["343502"].sales_rep, "LINK, KYLE")       # from the cache, not the page
            self.assertEqual(by_id["343502"].rep_source, "cache")
            self.assertEqual(by_id["344435"].sales_rep, "STORM, VICTORIA")  # stale entry re-read
            self.assertEqual(by_id["344435"].rep_source, "page")
            self.assertIsNone(by_id["341217"].sales_rep)
            self.assertEqual(by_id["341217"].dispatch_rep, "ORMSBY, PODGE")
            cache.save()
            self.assertEqual(set(RepCache(cache.path).entries), {"343502", "344435", "342271", "341217", "341205"})

            # A page that cannot be read: the load stays, as unassigned, and the error is reported.
            due2, _ = due_loads_from_grid(parse_grid(PAGE), NOW, 48)
            empty = RepCache(None)
            errors = attach_reps(due2, lambda lid: (_ for _ in ()).throw(RuntimeError("HTTP 500")), empty, NOW, max_lookups=2)
            self.assertEqual(len(errors), 5)
            self.assertTrue(all(l.rep_source == "error" for l in due2))
            self.assertIn("lookup cap", errors[-1])

    def test_group_by_rep_orders_names_and_puts_unassigned_last(self):
        due, _ = due_loads_from_grid(parse_grid(PAGE), NOW, 48)
        attach_reps(due, lookup, RepCache(None), NOW)
        groups = group_by_rep(due)
        self.assertEqual([g.name for g in groups], ["Kyle Link", "Podge Ormsby", "Victoria Storm", None])
        self.assertEqual([g.key for g in groups][-1], UNASSIGNED)
        self.assertEqual([l.load_id for l in groups[2].loads], ["343502", "344435"])


class ComposeTests(unittest.TestCase):
    def groups(self):
        due, _ = due_loads_from_grid(parse_grid(PAGE), NOW, 48)
        attach_reps(due, lookup, RepCache(None), NOW)
        return {g.name: g for g in group_by_rep(due)}

    def test_rep_with_email_gets_to_and_managers_cc_plus_secondary_rep(self):
        kyle = self.groups()["Kyle Link"]
        to, cc, routed = address(kyle, REPS, CC)
        self.assertEqual(to, ["kyle.link@circledelivers.com"])
        self.assertEqual(cc, ["justice.cromwell@circledelivers.com", "smitchell@circledelivers.com", "jack.gibson@circledelivers.com"])
        self.assertFalse(routed)
        email = compose(kyle, REPS, cc=CC, now=NOW)
        self.assertEqual(email.subject, "Due-date alert: 1 load due within 48 hours - Kyle Link")
        self.assertTrue(email.text.startswith("Hi Kyle,"))
        self.assertIn("Load 342271  |  AVIS TRUCKS  |  due Wed 10/14 15:20 (calculated)  |  1 d 5 h left", email.text)
        self.assertIn("3 units: SAVANA", email.text)
        self.assertIn("price $1,200.00", email.text)
        self.assertIn("loadmaint.mcgi?load_id=342271", email.text)
        self.assertNotIn("PAST DUE", email.text)
        self.assertIn("<table", email.html)
        self.assertIn("Due within 48 hours (1)", email.html)
        self.assertNotIn("dispatch rep", email.text)

    def test_manager_who_is_also_the_rep_is_not_copied_to_herself(self):
        victoria = self.groups()["Victoria Storm"]
        reps = dict(REPS)
        reps["victoria storm"] = {"email": "justice.cromwell@circledelivers.com", "title": "", "phone": ""}
        to, cc, _ = address(victoria, reps, CC)
        self.assertEqual(to, ["justice.cromwell@circledelivers.com"])
        self.assertEqual(cc, ["smitchell@circledelivers.com"])  # the secondary rep is the same person: not repeated

    def test_overdue_section_and_calculated_marker(self):
        victoria = self.groups()["Victoria Storm"]
        email = compose(victoria, REPS, cc=CC, now=NOW)
        self.assertEqual(email.subject, "Due-date alert: 2 loads due within 48 hours (1 past due) - Victoria Storm")
        self.assertIn("PAST DUE (1)", email.text)
        self.assertIn("DUE WITHIN 48 HOURS (1)", email.text)
        self.assertIn("4 d 10 h past due", email.text)
        self.assertIn("or already past due", email.text)
        self.assertIn("Past due (1)", email.html)
        self.assertIn("background:#fdecea", email.html)
        self.assertEqual(email.load_ids, ["343502", "344435"])

    def test_rep_without_email_is_routed_to_the_managers(self):
        podge = self.groups()["Podge Ormsby"]
        email = compose(podge, REPS, cc=CC, now=NOW)
        self.assertTrue(email.routed)
        self.assertEqual(email.to, CC)
        self.assertEqual(email.cc, [])
        self.assertEqual(email.subject, "Due-date alert: 1 load due within 48 hours (1 past due) - Podge Ormsby (no email on file)")
        self.assertTrue(email.text.startswith("Hi Justice and Stuart,"))
        self.assertIn("no email address in reps.csv", email.text)
        self.assertIn("sales rep: Podge Ormsby; dispatch rep: Seth Edmondson", email.text)

    def test_unassigned_loads_go_to_the_managers_with_the_dispatch_rep_shown(self):
        unassigned = self.groups()[None]
        email = compose(unassigned, REPS, cc=CC, now=NOW)
        self.assertEqual(email.to, CC)
        self.assertEqual(email.subject, "Due-date alert: 1 load due within 48 hours - no sales rep assigned")
        self.assertIn("NO sales rep on the load page", email.text)
        self.assertIn("sales rep: none; dispatch rep: Podge Ormsby", email.text)
        self.assertIn("problems flagged: Yes", email.text)
        self.assertIn("Sales / dispatch rep", email.html)

    def test_empty_cc_leaves_nobody_for_unassigned(self):
        unassigned = self.groups()[None]
        to, cc, routed = address(unassigned, REPS, [])
        self.assertEqual((to, cc, routed), ([], [], True))


class RunTests(unittest.TestCase):
    def run_pass(self, tmp, *, now=NOW, send=True, transport=None, repeat_hours=24.0, page=PAGE):
        transport = transport or FakeTransport()
        state = AlertState(os.path.join(tmp, "alert_state.json"))
        summary = run_once(
            parse_grid(page), now=now, hours=48, include_overdue=True, reps=REPS, cc=CC, lookup=lookup,
            rep_cache=RepCache(os.path.join(tmp, "rep_cache.json")), state=state, repeat_hours=repeat_hours,
            transport=transport, from_addr="Circle <vehicles@circledelivers.com>", record=send,
            sent_log=os.path.join(tmp, "sent_log.jsonl") if send else None,
        )
        return summary, transport, state

    def test_dry_run_writes_nothing_to_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary, transport, _ = self.run_pass(tmp, send=False)
            self.assertEqual(len(transport.sent), 4)  # Kyle, Podge (routed), Victoria, unassigned
            self.assertFalse(os.path.exists(os.path.join(tmp, "alert_state.json")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "sent_log.jsonl")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "rep_cache.json")))  # the cache is always kept
            self.assertEqual(summary["due"], 5)
            self.assertEqual(summary["overdue"], 2)
            self.assertFalse(summary["emails"][0]["sent"])

    def test_send_records_state_and_repeats_only_after_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary, transport, _ = self.run_pass(tmp)
            self.assertEqual(len(transport.sent), 4)
            self.assertEqual([e["rep"] for e in summary["emails"]], ["Kyle Link", "Podge Ormsby", "Victoria Storm", None])
            with open(os.path.join(tmp, "sent_log.jsonl"), encoding="utf-8") as handle:
                lines = [json.loads(line) for line in handle]
            self.assertEqual(len(lines), 4)
            self.assertEqual(lines[0]["to"], ["kyle.link@circledelivers.com"])
            self.assertEqual(lines[0]["message_id"], "msg-1")
            state = AlertState(os.path.join(tmp, "alert_state.json"))
            self.assertEqual(set(state.sent), {"343502", "344435", "342271", "341217", "341205"})

            # Two hours later: everything was alerted already, nothing goes out.
            summary, transport, _ = self.run_pass(tmp, now=NOW + timedelta(hours=2))
            self.assertEqual(transport.sent, [])
            self.assertEqual(summary["skipped_repeat"], 5)

            # Next morning: the same loads are repeated.
            summary, transport, _ = self.run_pass(tmp, now=NOW + timedelta(hours=24))
            self.assertEqual(len(transport.sent), 4)

            # A changed due date is alerted at once, on its own, even inside the window.
            moved = PAGE.replace("10/14/2026 15:20 *Calculated", "10/14/2026 09:00")
            summary, transport, _ = self.run_pass(tmp, now=NOW + timedelta(hours=26), page=moved)
            self.assertEqual(len(transport.sent), 1)
            self.assertEqual(transport.sent[0][0].load_ids, ["342271"])

            # repeat_hours 0 means every run.
            summary, transport, _ = self.run_pass(tmp, now=NOW + timedelta(hours=27), repeat_hours=0)
            self.assertEqual(len(transport.sent), 4)

    def test_a_failed_send_does_not_mark_its_loads_or_stop_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary, transport, state = self.run_pass(tmp, transport=FakeTransport(fail_for="Kyle Link"))
            self.assertEqual(len(transport.sent), 3)
            self.assertEqual(summary["send_errors"], ["Kyle Link: SES down"])
            self.assertNotIn("342271", state.sent)
            self.assertIn("343502", state.sent)


class RedirectTests(unittest.TestCase):
    def test_redirect_marks_the_copy_and_keeps_the_content(self):
        due, _ = due_loads_from_grid(parse_grid(PAGE), NOW, 48)
        attach_reps(due, lookup, RepCache(None), NOW)
        kyle = [g for g in group_by_rep(due) if g.name == "Kyle Link"][0]
        email = redirect(compose(kyle, REPS, cc=CC, now=NOW), ["me@circledelivers.com"])
        self.assertEqual(email.to, ["me@circledelivers.com"])
        self.assertEqual(email.cc, [])
        self.assertEqual(email.subject, "[Test copy] Due-date alert: 1 load due within 48 hours - Kyle Link")
        self.assertTrue(email.text.startswith(
            "TEST COPY. This alert would have been sent To: kyle.link@circledelivers.com; "
            "Cc: justice.cromwell@circledelivers.com, smitchell@circledelivers.com, jack.gibson@circledelivers.com."))
        self.assertIn("Hi Kyle,", email.text)
        self.assertIn("loadmaint.mcgi?load_id=342271", email.text)
        self.assertIn("TEST COPY.", email.html)
        self.assertLess(email.html.index("TEST COPY."), email.html.index("<table"))

    def test_redirected_send_records_nothing_and_only_rep_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            transport = FakeTransport()
            state = AlertState(os.path.join(tmp, "alert_state.json"))
            summary = run_once(
                parse_grid(PAGE), now=NOW, hours=48, include_overdue=True, reps=REPS, cc=CC, lookup=lookup,
                rep_cache=RepCache(None), state=state, repeat_hours=24, transport=transport, from_addr="x@y",
                record=True, sent_log=os.path.join(tmp, "sent_log.jsonl"), redirect_to=["me@circledelivers.com"],
                only_rep="LINK, KYLE",
            )
            self.assertEqual(len(transport.sent), 1)
            self.assertEqual(transport.sent[0][0].to, ["me@circledelivers.com"])
            self.assertEqual(transport.sent[0][0].rep, "Kyle Link")
            self.assertEqual(state.sent, {})
            self.assertFalse(os.path.exists(os.path.join(tmp, "alert_state.json")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "sent_log.jsonl")))
            self.assertEqual(summary["emails"][0]["redirected_to"], ["me@circledelivers.com"])
            self.assertFalse(summary["emails"][0]["sent"])

            transport = FakeTransport()
            run_once(parse_grid(PAGE), now=NOW, hours=48, include_overdue=True, reps=REPS, cc=CC, lookup=lookup,
                     rep_cache=RepCache(None), state=state, repeat_hours=24, transport=transport, from_addr="x@y",
                     record=False, sent_log=None, only_rep="unassigned")
            self.assertEqual([e.rep for e, _ in transport.sent], [None])


class CliTests(unittest.TestCase):
    def test_offline_dry_run_from_saved_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "spot.html")
            with open(page, "w", encoding="utf-8") as handle:
                handle.write(PAGE)
            reps_json = os.path.join(tmp, "reps_by_load.json")
            with open(reps_json, "w", encoding="utf-8") as handle:
                json.dump({lid: {"sales_rep": l.sales_rep, "sales_rep2": l.sales_rep2, "dispatch_rep": l.dispatch_rep}
                           for lid, l in LOAD_PAGES.items()}, handle)
            reps_csv = os.path.join(tmp, "reps.csv")
            with open(reps_csv, "w", encoding="utf-8") as handle:
                handle.write("name,email,title,phone\nLINK, KYLE,kyle.link@circledelivers.com,Sr. Account Manager,(260) 319-8153\n"
                             "STORM, VICTORIA,victoria.storm@circledelivers.com,Senior Account Manager,(260) 322-6108\n")
            outbox = os.path.join(tmp, "outbox")
            summary_path = os.path.join(tmp, "summary.json")
            rc = main(["--from-html", page, "--reps-json", reps_json, "--reps", reps_csv, "--now", NOW.isoformat(),
                       "--outbox", outbox, "--state", os.path.join(tmp, "state.json"), "--sent-log", os.path.join(tmp, "log.jsonl"),
                       "--rep-cache", os.path.join(tmp, "cache.json"), "--json", summary_path])
            self.assertEqual(rc, 0)
            files = sorted(os.listdir(outbox))
            self.assertEqual(len(files), 4)
            with open(os.path.join(outbox, [f for f in files if f.endswith("_kyle-link.txt")][0]), encoding="utf-8") as handle:
                text = handle.read()
            self.assertIn("From: Circle Logistics <vehicles@circledelivers.com>", text)
            self.assertIn("To: kyle.link@circledelivers.com", text)
            self.assertIn("Cc: justice.cromwell@circledelivers.com, smitchell@circledelivers.com", text)
            self.assertIn("Subject: Due-date alert: 1 load due within 48 hours - Kyle Link", text)
            self.assertFalse(os.path.exists(os.path.join(tmp, "state.json")))
            with open(summary_path, encoding="utf-8") as handle:
                summary = json.load(handle)
            self.assertEqual(summary["due"], 5)
            self.assertEqual(len(summary["emails"]), 4)

    def test_list_mode_prints_and_composes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "spot.html")
            with open(page, "w", encoding="utf-8") as handle:
                handle.write(PAGE)
            listing = os.path.join(tmp, "due.json")
            rc = main(["--from-html", page, "--now", NOW.isoformat(), "--list", "--json", listing,
                       "--rep-cache", os.path.join(tmp, "cache.json"), "--outbox", os.path.join(tmp, "outbox")])
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, "outbox")))
            with open(listing, encoding="utf-8") as handle:
                loads = json.load(handle)["loads"]
            self.assertEqual([l["load_id"] for l in loads], ["343502", "341205", "341217", "342271", "344435"])
            self.assertEqual(loads[0]["hours_left"], -106.0)


class LambdaArgsTests(unittest.TestCase):
    def test_env_to_args(self):
        import alerts_lambda

        env = {"ALERT_SEND": "1", "ALERT_HOURS": "36", "ALERT_CC": "a@x.com,b@x.com", "ALERT_SAVE_PAGES": "1", "MAIL_FROM": "Me <me@x.com>"}
        args = alerts_lambda.alert_args("/tmp/w", env)
        self.assertIn("--send", args)
        self.assertEqual(args[args.index("--hours") + 1], "36")
        self.assertEqual(args[args.index("--repeat-hours") + 1], "24")
        self.assertEqual(args[args.index("--cc") + 1], "a@x.com,b@x.com")
        self.assertEqual(args[args.index("--from") + 1], "Me <me@x.com>")
        self.assertEqual(args[args.index("--reps") + 1], os.path.join("/tmp/w", "config", "reps.csv"))
        self.assertEqual(args[args.index("--save-html") + 1], os.path.join("/tmp/w", "pages"))
        self.assertNotIn("--no-overdue", args)
        args = alerts_lambda.alert_args("/tmp/w", {})
        self.assertNotIn("--send", args)
        self.assertNotIn("--cc", args)
        self.assertEqual(args[args.index("--from") + 1], "Circle Logistics <vehicles@circledelivers.com>")


if __name__ == "__main__":
    unittest.main()
