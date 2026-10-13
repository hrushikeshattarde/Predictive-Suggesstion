"""Unit tests for pickup_eta_alerts.py (``python -m unittest`` from the pickup_eta_alerts
folder). No network: the tracking page is a synthetic grid shaped like the live one
(including the Pickup ETA (Edit) widget and the Sales Rep columns the job asks for), the
load pages are a dict for the fallback path, and the transport records what it was given."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.dirname(HERE)
ROOT = os.path.dirname(FOLDER)
# This folder for the job, the project folder for the shared CargoTel modules, the sibling for the email plumbing.
for path in (ROOT, os.path.join(ROOT, "due_date_alerts"), FOLDER):
    if path not in sys.path:
        sys.path.insert(0, path)

from cargotel_search import parse_grid  # noqa: E402
from due_date_alerts import DEFAULT_CC, RepCache  # noqa: E402
from pickup_eta_alerts import (  # noqa: E402
    REASON_NO_ETA,
    REASON_NO_SCHED,
    REASON_STALE_ETA,
    UNASSIGNED,
    AlertState,
    CustomerReps,
    PairGroup,
    address,
    ago_text,
    all_tracked,
    attach_reps,
    compose,
    customer_key,
    group_by_reps,
    infer_sales_reps,
    mail_groups,
    main,
    parse_pickup_eta,
    parse_stamp,
    run_once,
    select_loads,
)
from suggest_reloads import DispatchedLoad  # noqa: E402

NOW = datetime(2026, 10, 13, 10, 0)

COLUMNS = [
    ("dispatch_rep", "Dispatch Rep"), ("load_id", "Order ID"), ("cons_ref_load", "PO Number"),
    ("statuspendingpickup", "Dispatched Status Date"), ("status", "Status"), ("originCityState", "Origin City, State"),
    ("DestinationCityState", "Destination City, State"), ("carriername", "Carrier Name"), ("model", "Model"), ("vins", "Vins"),
    ("AttentionAlert_Edit", "Attention Alert (Edit)"), ("PriorityComment", "Priority Comment"), ("pickup_eta_only", "Pickup ETA (Edit)"),
    ("sched_pickup", "PickUp Scheduled Date"), ("duedate", "Due Date"), ("PriorityCommentEdit", "Priority Comment (Add)"),
    ("UploadDoc", "Upload Document"), ("driver_name", "Driver Name"), ("driver_cell_phone", "Drivers Cell Phone"),
    ("carrierphone", "Carrier Phone"),
]
#: The live job asks for these on top of the saved view.
COLUMNS_WITH_REPS = COLUMNS + [("sales_rep1", "Sales Rep 1"), ("sales_rep2", "Sales Rep 2"), ("consignor", "Customer")]


def header(columns):
    return ('<tr static="" id="det_header_row" class="unshadow dv_row"><th class="dv_cell dv_string dv_header oeheader" id="row_number_head">#</th>'
            + "".join(f'<th class="dv_cell dv_cell_{key} dv_header dv_header_{key} oeheader" id="det_header_{key}"><span class="dv_header_name">{label}</span></th>'
                      for key, label in columns) + "</tr>")


def eta_widget(load_id, eta):
    """The live "Pickup ETA (Edit)" cell: five selects whose SELECTED options are the ETA.
    Month, day, year and hour values are plain ints; minutes are zero-padded."""

    chosen = ({"month": str(eta.month), "day": str(eta.day), "year": str(eta.year), "hour": str(eta.hour), "minute": f"{eta.minute:02d}"}
              if eta else {})

    def select(part, options):
        opts = '<OPTION value="">' + "".join(
            f'<OPTION value="{v}"{" SELECTED" if chosen.get(part) == v else ""}>{v}' for v in options)
        return (f'<SELECT name="Control__setpickupeta__{load_id}__{part}_pickup_eta" onchange="ReGenerateCalFromControls( (new CgtForm(\'Control\', '
                f'\'Control__setpickupeta__{load_id}\')) , \'pickup_eta\'); " id="Control__setpickupeta__{load_id}__{part}_pickup_eta" misc="">{opts}</SELECT>')

    return ('<DIV align="left">' + select("month", [str(i) for i in range(1, 13)]) + "/" + select("day", [str(i) for i in range(1, 32)]) + "/"
            + select("year", [str(y) for y in range(2024, 2029)])
            + f' <A onclick="DisplayCal( (new CgtForm(\'Control\', \'Control__setpickupeta__{load_id}\')) ,\'pickup_eta\')"><IMG src="/images/cal.gif" alt="Cal"></A> <BR>'
            + select("hour", [str(i) for i in range(24)]) + ":" + select("minute", [f"{i:02d}" for i in range(60)])
            + f'<INPUT type="hidden" name="pickup_eta_date_edit" value="{load_id}"></DIV>')


def section(text):
    return f'<tr class="dv_section_header"><td colspan="24">{text}</td></tr>'


def row(n, load_id, dispatch_rep, status, status_date, eta, sched, *, columns=COLUMNS_WITH_REPS, link_class="LinkIndividual", widget=True,
        origin="Kansas City, MO", dest="Ashland, MO", carrier="Jra Transport Inc", carrier_phone="(816) 555-0100",
        driver="DOE, JOHN", driver_phone="(816) 555-0101", model="SAVANA", vins="1GDY7RFP0T1305791", po="",
        comment="", due="11/18/2026 12:03 *Calculated", sales1="", sales2="", consignor="N/A"):
    values = {
        "dispatch_rep": dispatch_rep, "cons_ref_load": po, "statuspendingpickup": status_date, "status": status, "originCityState": origin,
        "DestinationCityState": dest, "carriername": f"<font color=red>{carrier}</font>", "model": f"<NOBR>{model}</NOBR>", "vins": vins,
        "AttentionAlert_Edit": f'<INPUT type="checkbox" name="attention_alert_edit_{load_id}" value="Y">', "PriorityComment": comment,
        "pickup_eta_only": eta_widget(load_id, eta) if widget else "", "sched_pickup": sched, "duedate": due,
        "PriorityCommentEdit": f'<INPUT size="20" id="Control__PriorityCaomment__{load_id}__comment" value="">', "UploadDoc": "Upload Documents",
        "driver_name": driver, "driver_cell_phone": driver_phone, "carrierphone": carrier_phone,
        "sales_rep1": sales1, "sales_rep2": sales2, "consignor": consignor,
        "load_id": (f'<A name="MapId_{load_id}" class="{link_class}" style="color: blue" href="/backoffice/loadmaint.mcgi?load_id={load_id}" target="_blank" >{load_id}</A>'
                    f'<input type=\'hidden\' name=\'load_id\' value="{load_id}">'),
    }
    cells = [f'<td class="dv_cell listable_cell dv_plain dv_cell_{key}" id="DetCell_{n}_{col}">{values[key]}</td>' for col, (key, _label) in enumerate(columns)]
    return f'<tr class="unshadow dv_row listable_row" id="det_row_{n}"><td class="dv_linenumber">{n}</td>' + "".join(cells) + "</tr>"


SPECS = {
    # AVIS TRUCKS: Kyle is dispatch AND sales rep: one To address.
    "343786": dict(n=1, load_id="343786", dispatch_rep="KYLE LINK", status="Dispatched", status_date="10/10/2026 08:25", eta=None, sched="10/10/2026 09:50",
                   sales1="KYLE LINK", consignor="Avis Trucks"),
    # Kyle dispatch, Seth sales (+ Jack as secondary): both To, Jack Cc.
    "343796": dict(n=2, load_id="343796", dispatch_rep="KYLE LINK", status="Dispatched", status_date="10/10/2026 08:32", eta=None, sched="10/10/2026 08:32",
                   origin="Euless, TX", dest="Amarillo, TX", carrier="Allegiance Trucking, LLC", po="PO-77", comment="Pickup later today",
                   sales1="SETH EDMONDSON", sales2="Gibson, Jack", consignor="Avis Trucks"),
    # In-Route with an ETA an hour away: fine.
    "343963": dict(n=3, load_id="343963", dispatch_rep="JACK GIBSON", status="In-Route", status_date="10/12/2026 14:04", eta=datetime(2026, 10, 13, 11, 0),
                   sched="10/10/2026 14:26", sales1="JACK GIBSON"),
    # In-Route, ETA entered as today's date only (00:00): still an ETA, and the day is not over.
    "344680": dict(n=4, load_id="344680", dispatch_rep="KYLE LINK", status="In-Route", status_date="10/13/2026 10:38", eta=datetime(2026, 10, 13, 0, 0),
                   sched="10/14/2026 10:36", sales1="KYLE LINK"),
    # Seth's load with NO sales rep, past its pickup: Avis's other loads say Kyle, so it joins a Seth + Kyle email.
    "341900": dict(n=5, load_id="341900", dispatch_rep="SETH EDMONDSON", status="Dispatched", status_date="10/09/2026 10:00", eta=datetime(2026, 10, 9, 0, 0),
                   sched="10/09/2026 10:00", carrier="Corja Auto Hauling Inc"),
    # PRITCHARD AUTO: dispatched 15 minutes ago with no ETA and pickup tomorrow: inside the grace period.
    "344700": dict(n=6, load_id="344700", dispatch_rep="SETH EDMONDSON", status="Dispatched", status_date="10/13/2026 09:45", eta=None, sched="10/14/2026 09:45",
                   sales1="SETH EDMONDSON"),
    # Dispatched, ETA of 10/08 (date only) long gone AND no scheduled pickup date; sales rep Podge has no email.
    "342198": dict(n=7, load_id="342198", dispatch_rep="VICTORIA STORM", status="Dispatched", status_date="10/06/2026 13:03", eta=datetime(2026, 10, 8, 0, 0),
                   sched="", origin="Jonestown, PA", dest="Bronx, NY", carrier="Ss Service 1996 Corp", sales1="PODGE ORMSBY"),
    # Bundle member: skipped.
    "999999": dict(n=8, load_id="999999", dispatch_rep="PODGE ORMSBY", status="Dispatched", status_date="10/09/2026 11:00", eta=None, sched="10/09/2026 11:00",
                   link_class="LinkBundleChild"),
    # No ETA widget in the cell at all: the ETA rules are skipped with a warning.
    "341500": dict(n=9, load_id="341500", dispatch_rep="MITCHELL MEYERS", status="Dispatched", status_date="10/11/2026 09:00", eta=None, sched="10/11/2026 09:00",
                   widget=False, sales1="MITCHELL MEYERS"),
    # Not a status the run checks.
    "341600": dict(n=10, load_id="341600", dispatch_rep="VICTORIA STORM", status="Delivered", status_date="10/11/2026 09:00", eta=None, sched="10/11/2026 09:00",
                   sales1="VICTORIA STORM"),
    # Dispatched, ETA 10/09 (date only) four days ago: past the pickup, status never moved.
    "341700": dict(n=11, load_id="341700", dispatch_rep="VICTORIA STORM", status="Dispatched", status_date="10/08/2026 09:00", eta=datetime(2026, 10, 9, 0, 0),
                   sched="10/08/2026 09:00", carrier="Aztecus LLC", comment="Picked up", sales1="VICTORIA STORM"),
    # SOME NEW CUSTOMER: bundle parent with two vehicles, dispatch rep without an email, no sales rep and nothing to infer it
    # from: routed to the managers.
    "341000": dict(n=12, load_id="341000", dispatch_rep="PODGE ORMSBY", status="Dispatched", status_date="10/09/2026 11:00", eta=None, sched="10/09/2026 11:00",
                   link_class="LinkBundleParent", vins="1GDY7RFPXT1309959<br>1GDY7RFPXT1310075", model="SAVANA | SAVANA"),
    # A pickup ETA earlier today that has passed: a same-day load, left alone until tomorrow.
    "342000": dict(n=13, load_id="342000", dispatch_rep="KYLE LINK", status="Dispatched", status_date="10/12/2026 09:00", eta=datetime(2026, 10, 13, 8, 0),
                   sched="10/12/2026 09:00", sales1="KYLE LINK"),
}
LAYOUT = [("Customer: AVIS TRUCKS", ["343786", "343796", "343963", "344680", "341900"]),
          ("Customer: PRITCHARD AUTO", ["344700", "342198", "999999", "341500", "341600", "341700"]),
          ("Customer: SOME NEW CUSTOMER", ["341000"]),
          ("Customer: ELRAC, LLC", ["342000"])]


def build(columns=COLUMNS_WITH_REPS, *, without=(), replace=None):
    """The synthetic tracking page; ``without`` drops loads, ``replace`` swaps a load's row spec."""

    parts = []
    for title, ids in LAYOUT:
        parts.append(section(title))
        for lid in ids:
            if lid in without:
                continue
            spec = dict(SPECS[lid])
            if replace and lid in replace:
                spec.update(replace[lid])
            parts.append(row(columns=columns, **spec))
    return ("<html><head><title>Spot Loads - Tracking</title></head><body><table>" + header(columns) + "".join(parts)
            + "</table><div>Rows 1 - 13 of 13</div></body></html>")


PAGE = build()
#: The same page without the freshly dispatched load, whose grace period would expire mid-test.
STABLE = build(without=("344700",))
#: What the saved view returns without the extra columns: the fallback path reads load pages.
PAGE_NO_REPS = build(COLUMNS)

LOAD_PAGES = {
    "343786": DispatchedLoad(load_id="343786", sales_rep="LINK, KYLE", dispatch_rep="LINK, KYLE"),
    "343796": DispatchedLoad(load_id="343796", sales_rep="EDMONDSON, SETH", sales_rep2="GIBSON, JACK", dispatch_rep="LINK, KYLE"),
    "341900": DispatchedLoad(load_id="341900", sales_rep=None, dispatch_rep="EDMONDSON, SETH"),
    "344700": DispatchedLoad(load_id="344700", sales_rep="EDMONDSON, SETH", dispatch_rep="EDMONDSON, SETH"),
    "342198": DispatchedLoad(load_id="342198", sales_rep="ORMSBY, PODGE", dispatch_rep="STORM, VICTORIA"),
    "341000": DispatchedLoad(load_id="341000", sales_rep=None, dispatch_rep="ORMSBY, PODGE"),
    "341700": DispatchedLoad(load_id="341700", sales_rep="STORM, VICTORIA", dispatch_rep="STORM, VICTORIA"),
}

REPS = {
    "kyle link": {"email": "kyle.link@circledelivers.com", "title": "Sr. Account Manager", "phone": ""},
    "seth edmondson": {"email": "seth.edmondson@circledelivers.com", "title": "", "phone": ""},
    "jack gibson": {"email": "jack.gibson@circledelivers.com", "title": "", "phone": ""},
    "victoria storm": {"email": "victoria.storm@circledelivers.com", "title": "", "phone": ""},
    "justice cromwell": {"email": "justice.cromwell@circledelivers.com", "title": "", "phone": ""},
}
CC = list(DEFAULT_CC)
DEFAULT_FLAGGED = ["343786", "343796", "341000", "341900", "342198", "341700"]
LABELS = ["Kyle Link / Kyle Link", "Kyle Link / Seth Edmondson", "Podge Ormsby / no sales rep", "Seth Edmondson / Kyle Link",
          "Victoria Storm / Podge Ormsby, Victoria Storm"]


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


def flagged_loads(html=PAGE, now=NOW, **rules):
    loads, warnings, counts = select_loads(parse_grid(html), now, **rules)
    return loads, warnings, counts


def groups(html=PAGE, now=NOW, reps=REPS, history=None, **rules):
    """The addressed mail groups for a page, keyed by label."""

    grid = parse_grid(html)
    loads, _w, _c = select_loads(grid, now, **rules)
    attach_reps(loads, lookup, RepCache(None), now)
    infer_sales_reps(loads, all_tracked(grid), history)
    return {g.label: g for g in mail_groups(loads, reps, CC)}


class ParseTests(unittest.TestCase):
    def test_parse_stamp(self):
        self.assertEqual(parse_stamp("10/10/2026 14:26"), datetime(2026, 10, 10, 14, 26))
        self.assertEqual(parse_stamp("11/24/2026 13:26 *Calculated"), datetime(2026, 11, 24, 13, 26))
        self.assertEqual(parse_stamp("10/10/2026"), datetime(2026, 10, 10, 0, 0))
        self.assertIsNone(parse_stamp(""))
        self.assertIsNone(parse_stamp(None))
        self.assertIsNone(parse_stamp("13/45/2026 00:00"))
        self.assertIsNone(parse_stamp("tomorrow"))

    def test_parse_pickup_eta_reads_the_selected_options(self):
        self.assertEqual(parse_pickup_eta(eta_widget("1", datetime(2026, 10, 13, 11, 0))), (datetime(2026, 10, 13, 11, 0), True))
        self.assertEqual(parse_pickup_eta(eta_widget("1", datetime(2026, 10, 9, 16, 15))), (datetime(2026, 10, 9, 16, 15), True))
        self.assertEqual(parse_pickup_eta(eta_widget("1", datetime(2026, 10, 12, 0, 0))), (datetime(2026, 10, 12, 0, 0), True))
        self.assertEqual(parse_pickup_eta(eta_widget("1", None)), (None, True))
        self.assertEqual(parse_pickup_eta(""), (None, False))
        self.assertEqual(parse_pickup_eta(None), (None, False))
        self.assertEqual(parse_pickup_eta("<input type='hidden' name='pickup_eta_date_edit' value='1'>"), (None, False))
        # The empty option explicitly selected is still "no ETA".
        blank = eta_widget("1", None).replace('<OPTION value="">', '<OPTION value="" SELECTED>', 3)
        self.assertEqual(parse_pickup_eta(blank), (None, True))

    def test_ago_text(self):
        self.assertEqual(ago_text(0.2), "just now")
        self.assertEqual(ago_text(5.6), "6 h ago")
        self.assertEqual(ago_text(48), "2 d ago")
        self.assertEqual(ago_text(50), "2 d 2 h ago")

    def test_customer_key(self):
        for name in ("MERCHANTS AUTOMOTIVE GROUP HOLDINGS LLC", "Merchants Automotive Group Holdings Llc", "Merchants Automotive Group Holdings LLC (EM)"):
            self.assertEqual(customer_key(name), "merchantsautomotivegroupholdingsllc")
        self.assertEqual(customer_key("Elrac, Llc (EM)"), "elracllc")
        self.assertEqual(customer_key(""), "")
        self.assertEqual(customer_key(None), "")


class SelectTests(unittest.TestCase):
    def test_default_rules_oldest_status_first(self):
        loads, warnings, counts = flagged_loads()
        self.assertEqual([l.load_id for l in loads], DEFAULT_FLAGGED)
        by_id = {l.load_id: l for l in loads}
        self.assertEqual(by_id["343786"].reasons, [REASON_NO_ETA])
        self.assertEqual(by_id["342198"].reasons, [REASON_STALE_ETA, REASON_NO_SCHED])
        self.assertEqual(by_id["341700"].reasons, [REASON_STALE_ETA])
        self.assertEqual(by_id["341900"].reasons, [REASON_STALE_ETA])
        self.assertEqual(by_id["341000"].kind, "load")
        self.assertEqual(by_id["341000"].units, 2)
        self.assertEqual(by_id["343786"].dispatch_rep, "KYLE LINK")
        self.assertEqual(by_id["343786"].customer, "AVIS TRUCKS")
        self.assertEqual(by_id["343786"].customer_key, "avistrucks")
        self.assertEqual(by_id["343796"].carrier, "Allegiance Trucking, LLC")
        self.assertEqual(by_id["343796"].priority_comment, "Pickup later today")
        self.assertEqual(by_id["343796"].po_number, "PO-77")
        self.assertEqual(by_id["343786"].due_text, "11/18/2026 12:03 (calculated)")
        self.assertEqual(by_id["343786"].sched_pickup, datetime(2026, 10, 10, 9, 50))
        self.assertEqual(by_id["341700"].eta_deadline, datetime(2026, 10, 10, 0, 0))  # date-only ETA: the whole day
        self.assertEqual(by_id["341700"].eta_overdue_hours(NOW), 82.0)
        # Sales reps come straight from the grid.
        self.assertEqual((by_id["343796"].sales_rep, by_id["343796"].sales_rep2, by_id["343796"].rep_source), ("SETH EDMONDSON", "Gibson, Jack", "grid"))
        self.assertEqual((by_id["341900"].sales_rep, by_id["341900"].rep_source), (None, "grid"))
        self.assertEqual(counts, {"rows": 12, "in_status": 11, "too_new": 1, "same_day": 1, "no_widget": 1})
        self.assertEqual(len(warnings), 1)
        self.assertIn("341500", warnings[0])
        self.assertIn("no Pickup ETA widget", warnings[0])

    def test_same_day_loads_are_left_alone(self):
        loads, _w, counts = flagged_loads()
        self.assertNotIn("342000", [l.load_id for l in loads])
        self.assertEqual(counts["same_day"], 1)
        loads, _w, counts = flagged_loads(include_same_day=True)
        self.assertIn("342000", [l.load_id for l in loads])
        self.assertEqual(next(l for l in loads if l.load_id == "342000").reasons, [REASON_STALE_ETA])
        self.assertEqual(counts["same_day"], 0)
        # Tomorrow the same load is past its pickup and no longer same-day.
        loads, _w, counts = flagged_loads(now=NOW + timedelta(days=1))
        self.assertIn("342000", [l.load_id for l in loads])
        # A load with no ETA whose scheduled pickup is today is also same-day.
        today_sched = build(replace={"343786": dict(sched="10/13/2026 15:00")})
        loads, _w, counts = flagged_loads(today_sched)
        self.assertNotIn("343786", [l.load_id for l in loads])
        self.assertEqual(counts["same_day"], 2)

    def test_passed_eta_rule(self):
        # Off: the past-ETA loads drop out, except that 342198 is still not scheduled.
        loads, _w, _c = flagged_loads(check_stale=False)
        self.assertEqual([l.load_id for l in loads], ["343786", "343796", "341000", "342198"])
        self.assertEqual(next(l for l in loads if l.load_id == "342198").reasons, [REASON_NO_SCHED])
        # In-Route loads are left alone by default; opting them in flags 344680 once its day is over plus the grace
        # (its scheduled pickup is 10/14, so the same-day rule has to be off to see it on the 14th).
        late = datetime(2026, 10, 14, 3, 0)
        loads, _w, _c = flagged_loads(now=late, include_same_day=True)
        self.assertNotIn("344680", [l.load_id for l in loads])
        loads, _w, _c = flagged_loads(now=late, include_same_day=True, stale_statuses=("Dispatched", "In-Route"))
        self.assertIn("344680", [l.load_id for l in loads])
        self.assertIn("343963", [l.load_id for l in loads])  # ETA 10/13 11:00, 16 h ago

    def test_passed_eta_grace_and_date_only_semantics(self):
        # 342000 was dispatched 10/12 09:00; the ETA is moved to that morning so the checks run with the same-day rule off.
        timed = build(replace={"342000": dict(eta=datetime(2026, 10, 12, 8, 30))})
        dated = build(replace={"342000": dict(eta=datetime(2026, 10, 12, 0, 0))})
        self.assertNotIn("342000", [l.load_id for l in flagged_loads(timed, now=datetime(2026, 10, 12, 10, 0), include_same_day=True)[0]])  # 1.5 h late: inside the grace
        self.assertIn("342000", [l.load_id for l in flagged_loads(timed, now=datetime(2026, 10, 12, 10, 30), include_same_day=True)[0]])     # 2 h late
        self.assertIn("342000", [l.load_id for l in flagged_loads(timed, now=datetime(2026, 10, 12, 10, 0), include_same_day=True, stale_eta_hours=0)[0]])
        self.assertNotIn("342000", [l.load_id for l in flagged_loads(dated, now=datetime(2026, 10, 12, 23, 0), include_same_day=True)[0]])  # the ETA day is not over
        self.assertNotIn("342000", [l.load_id for l in flagged_loads(dated, now=datetime(2026, 10, 13, 1, 59))[0]])                          # over, but inside the grace
        self.assertIn("342000", [l.load_id for l in flagged_loads(dated, now=datetime(2026, 10, 13, 2, 0))[0]])

    def test_grace_period_is_adjustable(self):
        loads, _w, counts = flagged_loads(min_age_hours=0)
        self.assertIn("344700", [l.load_id for l in loads])
        self.assertEqual(counts["too_new"], 0)
        loads, _w, counts = flagged_loads(min_age_hours=24 * 7)
        self.assertEqual([l.load_id for l in loads], [])

    def test_status_filter(self):
        loads, _w, _c = flagged_loads(statuses=("Dispatched",))
        self.assertEqual([l.load_id for l in loads], DEFAULT_FLAGGED)
        loads, _w, _c = flagged_loads(statuses=("delivered",))
        self.assertEqual([l.load_id for l in loads], ["341600"])
        loads, _w, _c = flagged_loads(statuses=("In-Route",))
        self.assertEqual(loads, [])

    def test_schedule_check_can_be_skipped(self):
        loads, _w, _c = flagged_loads(check_sched=False)
        self.assertEqual([l.load_id for l in loads], DEFAULT_FLAGGED)  # 342198 stays, for its passed ETA
        self.assertEqual(next(l for l in loads if l.load_id == "342198").reasons, [REASON_STALE_ETA])
        loads, _w, _c = flagged_loads(check_sched=False, check_stale=False)
        self.assertEqual([l.load_id for l in loads], ["343786", "343796", "341000"])

    def test_missing_columns_stop_the_run(self):
        no_eta_column = PAGE.replace("dv_cell_pickup_eta_only dv_header dv_header_pickup_eta_only", "dv_cell_gone dv_header dv_header_gone").replace(
            '<span class="dv_header_name">Pickup ETA (Edit)</span>', '<span class="dv_header_name">Gone</span>')
        with self.assertRaises(RuntimeError) as ctx:
            flagged_loads(no_eta_column)
        self.assertIn("pickup_eta_only", str(ctx.exception))
        no_sched_column = PAGE.replace("dv_cell_sched_pickup dv_header dv_header_sched_pickup", "dv_cell_gone dv_header dv_header_gone").replace(
            '<span class="dv_header_name">PickUp Scheduled Date</span>', '<span class="dv_header_name">Gone</span>')
        with self.assertRaises(RuntimeError):
            flagged_loads(no_sched_column)
        loads, _w, _c = flagged_loads(no_sched_column, check_sched=False)
        self.assertEqual([l.load_id for l in loads], DEFAULT_FLAGGED)

    def test_empty_grid(self):
        empty = "<html><body><table>" + header(COLUMNS_WITH_REPS) + "</table><div>No Results Found</div></body></html>"
        loads, warnings, counts = flagged_loads(empty)
        self.assertEqual((loads, warnings, counts["rows"]), ([], [], 0))


class RepTests(unittest.TestCase):
    def test_grid_reps_need_no_lookup(self):
        loads, _w, _c = flagged_loads()
        calls = []
        errors = attach_reps(loads, lambda lid: calls.append(lid), RepCache(None), NOW)
        self.assertEqual((errors, calls), ([], []))
        self.assertEqual({l.rep_source for l in loads}, {"grid"})

    def test_fallback_reads_pages_through_the_cache_and_keeps_the_grid_dispatch_rep(self):
        loads, _w, _c = flagged_loads(PAGE_NO_REPS)
        self.assertEqual({l.rep_source for l in loads}, {""})
        with tempfile.TemporaryDirectory() as tmp:
            cache = RepCache(os.path.join(tmp, "cache.json"))
            calls = []

            def counting(load_id):
                calls.append(load_id)
                return lookup(load_id)

            errors = attach_reps(loads, counting, cache, NOW)
            self.assertEqual(errors, [])
            self.assertEqual(calls, DEFAULT_FLAGGED)
            by_id = {l.load_id: l for l in loads}
            self.assertEqual(by_id["343796"].sales_rep, "EDMONDSON, SETH")
            self.assertEqual(by_id["343796"].sales_rep2, "GIBSON, JACK")
            self.assertEqual(by_id["343796"].dispatch_rep, "KYLE LINK")  # grid wins over the page's "LINK, KYLE"
            self.assertIsNone(by_id["341000"].sales_rep)
            self.assertEqual({l.rep_source for l in loads}, {"page"})
            cache.save()

            loads2, _w, _c = flagged_loads(PAGE_NO_REPS)
            cache2 = RepCache(os.path.join(tmp, "cache.json"))
            attach_reps(loads2, counting, cache2, NOW + timedelta(hours=1))
            self.assertEqual(len(calls), 6)  # all from the cache
            self.assertEqual({l.rep_source for l in loads2}, {"cache"})

    def test_a_bad_page_or_the_cap_leaves_the_load_in_with_no_sales_rep(self):
        loads, _w, _c = flagged_loads(PAGE_NO_REPS)
        cache = RepCache(None)

        def flaky(load_id):
            if load_id == "343796":
                raise RuntimeError("HTTP 500")
            return lookup(load_id)

        errors = attach_reps(loads, flaky, cache, NOW, max_lookups=3)
        self.assertEqual(len(errors), 4)
        self.assertIn("HTTP 500", errors[0])
        self.assertTrue(all("lookup cap" in e for e in errors[1:]))
        by_id = {l.load_id: l for l in loads}
        self.assertEqual(by_id["343796"].rep_source, "error")
        self.assertEqual(by_id["342198"].rep_source, "error")
        self.assertEqual(by_id["343786"].sales_rep, "LINK, KYLE")
        self.assertEqual(attach_reps(loads, None, RepCache(None), NOW), [f"load {l.load_id}: no rep lookup available" for l in loads])

    def test_missing_sales_rep_is_taken_from_the_customers_other_loads(self):
        grid = parse_grid(PAGE)
        loads, _w, _c = select_loads(grid, NOW)
        everything = all_tracked(grid)
        self.assertEqual(len(everything), 12)
        logged = []
        filled = infer_sales_reps(loads, everything, None, log=logged.append)
        self.assertEqual(filled, 1)
        by_id = {l.load_id: l for l in loads}
        self.assertEqual((by_id["341900"].sales_rep, by_id["341900"].sales_rep_inferred), ("KYLE LINK", True))
        self.assertEqual(by_id["341900"].inferred_from, "2 other loads of AVIS TRUCKS on the tracking page")
        self.assertEqual(by_id["341900"].sales_key, "kyle link")
        self.assertIsNone(by_id["341000"].sales_rep)  # nobody else at SOME NEW CUSTOMER, no history
        self.assertFalse(by_id["343786"].sales_rep_inferred)
        self.assertEqual(logged, ["load 341900: no sales rep; Kyle Link taken from 2 other loads of AVIS TRUCKS on the tracking page"])

    def test_history_fills_in_when_the_grid_cannot(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "customer_reps.json")
            history = CustomerReps(path)
            grid = parse_grid(PAGE)
            history.learn(all_tracked(grid), NOW)
            self.assertEqual(history.best("Avis Trucks"), ("Kyle Link", 2))
            self.assertEqual(history.best("PRITCHARD AUTO"), ("Victoria Storm", 2))  # 341600 + 341700; Seth, Podge, Mitchell once each
            self.assertIsNone(history.best("Some New Customer"))
            history.save()

            # A past run saw Victoria on three loads of that customer.
            reloaded = CustomerReps(path)
            reloaded.customers[customer_key("SOME NEW CUSTOMER")] = {"victoria storm": {"name": "Victoria Storm", "count": 3, "last": "2026-10-01T10:00:00"}}
            loads, _w, _c = select_loads(grid, NOW)
            infer_sales_reps(loads, all_tracked(grid), reloaded)
            by_id = {l.load_id: l for l in loads}
            self.assertEqual((by_id["341000"].sales_rep, by_id["341000"].inferred_from), ("Victoria Storm", "3 earlier loads of SOME NEW CUSTOMER"))
            self.assertEqual(by_id["341900"].sales_rep, "KYLE LINK")  # the grid still comes first
            # Inferred reps are never learnt back into the history.
            reloaded.learn(loads, NOW)
            self.assertEqual(list(reloaded.customers[customer_key("SOME NEW CUSTOMER")]), ["victoria storm"])
            self.assertEqual(reloaded.customers[customer_key("SOME NEW CUSTOMER")]["victoria storm"]["count"], 3)


class GroupAndAddressTests(unittest.TestCase):
    def test_one_pair_group_per_dispatch_and_sales_rep(self):
        grid = parse_grid(PAGE)
        loads, _w, _c = select_loads(grid, NOW)
        infer_sales_reps(loads, all_tracked(grid), None)
        pairs = group_by_reps(loads)
        self.assertEqual([(g.dispatch_rep, g.sales_rep) for g in pairs],
                         [("Kyle Link", "Kyle Link"), ("Kyle Link", "Seth Edmondson"), ("Podge Ormsby", None), ("Seth Edmondson", "Kyle Link"),
                          ("Victoria Storm", "Podge Ormsby"), ("Victoria Storm", "Victoria Storm")])
        self.assertEqual([[l.load_id for l in g.loads] for g in pairs], [["343786"], ["343796"], ["341000"], ["341900"], ["342198"], ["341700"]])
        self.assertFalse(pairs[2].unassigned)
        self.assertTrue(PairGroup((UNASSIGNED, UNASSIGNED), None, None).unassigned)

    def test_addressing(self):
        loads, _w, _c = flagged_loads()
        pairs = {(g.dispatch_rep, g.sales_rep): g for g in group_by_reps(loads)}
        to, cc, routed, notes = address(pairs[("Kyle Link", "Kyle Link")], REPS, CC)
        self.assertEqual((to, cc, routed, notes), (["kyle.link@circledelivers.com"], CC, False, []))

        to, cc, routed, notes = address(pairs[("Kyle Link", "Seth Edmondson")], REPS, CC)
        self.assertEqual(to, ["kyle.link@circledelivers.com", "seth.edmondson@circledelivers.com"])
        self.assertEqual(cc, CC + ["jack.gibson@circledelivers.com"])
        self.assertFalse(routed)

        to, cc, routed, notes = address(pairs[("Victoria Storm", "Podge Ormsby")], REPS, CC)
        self.assertEqual((to, cc, routed), (["victoria.storm@circledelivers.com"], CC, False))
        self.assertEqual(notes, ["sales rep Podge Ormsby has no email in reps.csv"])

        to, cc, routed, notes = address(pairs[("Podge Ormsby", None)], REPS, CC)
        self.assertEqual((to, cc, routed), (CC, [], True))
        self.assertEqual(notes, ["dispatch rep Podge Ormsby has no email in reps.csv", "no sales rep on load 341000"])

    def test_pairs_with_the_same_recipients_become_one_email(self):
        by_label = groups()
        # Victoria's two pairs (sales rep Podge without an email, and herself) both resolve to To Victoria, Cc the managers.
        self.assertEqual(list(by_label), LABELS)
        victoria = by_label["Victoria Storm / Podge Ormsby, Victoria Storm"]
        self.assertEqual([l.load_id for l in victoria.loads], ["342198", "341700"])
        self.assertEqual((victoria.to, victoria.cc, victoria.routed), (["victoria.storm@circledelivers.com"], CC, False))
        self.assertEqual(victoria.notes, ["sales rep Podge Ormsby has no email in reps.csv"])
        self.assertEqual(victoria.greet_names, ["Victoria"])
        self.assertEqual(victoria.key, "Victoria Storm|Podge Ormsby+Victoria Storm")
        self.assertTrue(victoria.matches("podge ormsby"))
        self.assertTrue(victoria.matches("victoria storm"))
        self.assertFalse(victoria.matches("kyle link"))
        routed = by_label["Podge Ormsby / no sales rep"]
        self.assertEqual((routed.to, routed.cc, routed.routed, routed.who), (CC, [], True, "Podge Ormsby"))
        self.assertFalse(routed.matches("unassigned"))
        # The inferred sales rep puts Seth's load in a Seth + Kyle email, with a note, instead of one to Seth alone.
        seth = by_label["Seth Edmondson / Kyle Link"]
        self.assertEqual(seth.to, ["seth.edmondson@circledelivers.com", "kyle.link@circledelivers.com"])
        self.assertEqual(seth.notes, ["Kyle Link is not set as sales rep on load 341900 but is on the customer's other loads, "
                                      "so this comes to them too (please set it on the load)"])
        self.assertNotIn("Seth Edmondson / no sales rep", by_label)

    def test_a_manager_who_is_also_the_rep_is_not_copied_to_herself(self):
        group = PairGroup(("justice cromwell", "kyle link"), "Justice Cromwell", "Kyle Link", [])
        to, cc, routed, _notes = address(group, REPS, CC)
        self.assertEqual(to, ["justice.cromwell@circledelivers.com", "kyle.link@circledelivers.com"])
        self.assertEqual(cc, ["smitchell@circledelivers.com"])

    def test_empty_cc_leaves_nobody_for_the_routed_group(self):
        group = PairGroup((UNASSIGNED, UNASSIGNED), None, None, [])
        self.assertEqual(address(group, REPS, []), ([], [], True, ["no dispatch rep on the load", "no sales rep on the load"]))
        loads, _w, _c = flagged_loads()
        group = PairGroup((UNASSIGNED, UNASSIGNED), None, None, loads[:2])
        self.assertEqual(address(group, REPS, CC)[3], ["no dispatch rep on loads 343786, 343796", "no sales rep on loads 343786, 343796"])


class ComposeTests(unittest.TestCase):
    def test_pair_email(self):
        email = compose(groups()["Kyle Link / Seth Edmondson"], now=NOW)
        self.assertEqual(email.subject, "Pickup ETA alert: 1 load with no pickup ETA - Kyle Link")
        self.assertEqual(email.to, ["kyle.link@circledelivers.com", "seth.edmondson@circledelivers.com"])
        self.assertEqual(email.cc, CC + ["jack.gibson@circledelivers.com"])
        self.assertFalse(email.routed)
        self.assertEqual(email.load_ids, ["343796"])
        self.assertEqual(email.key, "Kyle Link|Seth Edmondson")
        self.assertTrue(email.text.startswith("Hi Kyle and Seth,"))
        self.assertIn("As of Tue 10/13/2026 10:00 AM ET, these loads on Spot Loads - Tracking (Load Tracking > Spot Load Tracking > Spot Loads - Tracking) "
                      "need a pickup update from you.", email.text)
        self.assertIn("NO PICKUP ETA (1)\nGet the pickup ETA from the carrier and enter it on the load.", email.text)
        self.assertIn("Order 343796  |  AVIS TRUCKS  |  Dispatched since Sat 10/10 08:32 (3 d 1 h ago)  |  ACTION: enter the pickup ETA", email.text)
        self.assertIn("Euless, TX -> Amarillo, TX", email.text)
        self.assertIn("Allegiance Trucking, LLC (816) 555-0100  |  driver John Doe (816) 555-0101", email.text)
        self.assertIn("scheduled pickup Sat 10/10 08:32  |  pickup ETA none  |  due 11/18/2026 12:03 (calculated)", email.text)
        self.assertIn("1 unit: SAVANA  |  PO PO-77  |  note: Pickup later today", email.text)
        self.assertIn("loadmaint.mcgi?load_id=343796", email.text)
        self.assertNotIn("dispatch rep:", email.text)
        self.assertNotIn("PICKUP ETA PASSED", email.text)
        self.assertNotIn("sales rep not set", email.text)
        self.assertIn("No pickup ETA (1)", email.html)
        self.assertIn("<b>enter the pickup ETA</b>", email.html)
        self.assertIn('<b style="color:#b00020">none</b>', email.html)
        self.assertNotIn("Dispatch / sales rep", email.html)

    def test_same_person_in_both_roles_is_greeted_once(self):
        email = compose(groups()["Kyle Link / Kyle Link"], now=NOW)
        self.assertTrue(email.text.startswith("Hi Kyle,"))
        self.assertEqual(email.to, ["kyle.link@circledelivers.com"])

    def test_inferred_sales_rep_is_explained(self):
        email = compose(groups()["Seth Edmondson / Kyle Link"], now=NOW)
        self.assertEqual(email.subject, "Pickup ETA alert: 1 load past the pickup ETA - Seth Edmondson")
        self.assertEqual(email.to, ["seth.edmondson@circledelivers.com", "kyle.link@circledelivers.com"])
        self.assertTrue(email.text.startswith("Hi Seth and Kyle,"))
        self.assertIn("Note: Kyle Link is not set as sales rep on load 341900 but is on the customer's other loads, so this comes to them too "
                      "(please set it on the load).", email.text)
        self.assertIn("sales rep not set on the load; Kyle Link taken from 2 other loads of AVIS TRUCKS on the tracking page", email.text)
        self.assertIn("AVIS TRUCKS<br><small>sales rep not set on the load</small>", email.html)

    def test_passed_eta_section_and_the_note_about_an_unknown_rep(self):
        email = compose(groups()["Victoria Storm / Podge Ormsby, Victoria Storm"], now=NOW)
        self.assertEqual(email.subject, "Pickup ETA alert: 2 loads (2 past the pickup ETA, 1 not scheduled) - Victoria Storm")
        self.assertTrue(email.text.startswith("Hi Victoria,"))
        self.assertIn("Note: sales rep Podge Ormsby has no email in reps.csv.", email.text)
        self.assertIn("PICKUP ETA PASSED, STATUS NOT UPDATED (2)\nThe pickup ETA has come and gone and the load is still in the same status: "
                      "mark it picked up (update the status) or enter a new pickup ETA.", email.text)
        self.assertIn("ACTION: pickup ETA passed (the end of Thu 10/08, 4 d 10 h ago): mark picked up or set a new ETA; enter the scheduled pickup date", email.text)
        self.assertIn("ACTION: pickup ETA passed (the end of Fri 10/09, 3 d 10 h ago): mark picked up or set a new ETA\n", email.text)
        self.assertIn("scheduled pickup none  |  pickup ETA Thu 10/08 (passed)", email.text)
        self.assertNotIn("NO SCHEDULED PICKUP DATE", email.text)  # 342198 sits under the passed-ETA section
        self.assertIn("note: Picked up", email.text)
        self.assertIn("Pickup ETA passed, status not updated (2)", email.html)
        self.assertIn('<b style="color:#b00020">Fri 10/09</b><br><small>passed 3 d 10 h ago</small>', email.html)
        self.assertEqual(email.load_ids, ["342198", "341700"])

    def test_routed_email_names_the_reps_per_load(self):
        email = compose(groups()["Podge Ormsby / no sales rep"], now=NOW)
        self.assertEqual(email.subject, "Pickup ETA alert: 1 load with no pickup ETA - Podge Ormsby (no email on file)")
        self.assertEqual((email.to, email.cc, email.routed), (CC, [], True))
        self.assertTrue(email.text.startswith("Hi all,"))
        self.assertIn("Nobody could be emailed directly (dispatch rep Podge Ormsby has no email in reps.csv; no sales rep on load 341000)", email.text)
        self.assertIn("Load 341000", email.text)
        self.assertIn("2 units: SAVANA | SAVANA", email.text)
        self.assertIn("dispatch rep: Podge Ormsby; sales rep: none", email.text)
        self.assertIn("Dispatch / sales rep", email.html)
        self.assertEqual(email.key, "Podge Ormsby|nobody")

    def test_sections_and_subject_with_every_reason(self):
        grid = parse_grid(PAGE)
        loads, _w, _c = select_loads(grid, NOW)
        infer_sales_reps(loads, all_tracked(grid), None)
        everyone = mail_groups(loads, {}, CC)  # nobody has an address: one email to the managers with all six loads
        self.assertEqual(len(everyone), 1)
        email = compose(everyone[0], now=NOW)
        self.assertEqual(email.subject, "Pickup ETA alert: 6 loads (3 with no pickup ETA, 3 past the pickup ETA, 1 not scheduled) "
                                        "- Kyle Link, Podge Ormsby, Seth Edmondson, Victoria Storm (no email on file)")
        self.assertLess(email.text.index("NO PICKUP ETA (3)"), email.text.index("PICKUP ETA PASSED, STATUS NOT UPDATED (3)"))
        self.assertNotIn("NO SCHEDULED PICKUP DATE", email.text)
        self.assertIn("dispatch rep: Seth Edmondson; sales rep: Kyle Link (inferred)", email.text)
        self.assertEqual(email.load_ids, ["342198", "341700", "341900", "341000", "343786", "343796"])  # oldest status first


class RunTests(unittest.TestCase):
    def run_pass(self, tmp, *, send=True, now=NOW, html=PAGE, transport=None, **kwargs):
        transport = transport or FakeTransport()
        state = AlertState(os.path.join(tmp, "alert_state.json"))
        summary = run_once(
            parse_grid(html), now=now, reps=REPS, cc=CC, lookup=lookup, rep_cache=RepCache(os.path.join(tmp, "rep_cache.json")),
            customer_reps=CustomerReps(os.path.join(tmp, "customer_reps.json")),
            state=state, transport=transport, from_addr="Circle <vehicles@circledelivers.com>", record=send,
            sent_log=os.path.join(tmp, "sent_log.jsonl") if send else None, **kwargs,
        )
        return summary, transport, state

    def test_dry_run_writes_nothing_to_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary, transport, _ = self.run_pass(tmp, send=False)
            self.assertEqual(len(transport.sent), 5)
            self.assertFalse(os.path.exists(os.path.join(tmp, "alert_state.json")))
            self.assertFalse(os.path.exists(os.path.join(tmp, "sent_log.jsonl")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "rep_cache.json")))  # the cache and the history are always kept
            self.assertTrue(os.path.exists(os.path.join(tmp, "customer_reps.json")))
            self.assertEqual((summary["rows"], summary["in_status"], summary["flagged"], summary["too_new"], summary["same_day"]), (12, 11, 6, 1, 1))
            self.assertEqual(summary["by_reason"], {REASON_NO_ETA: 3, REASON_STALE_ETA: 3, REASON_NO_SCHED: 1})
            self.assertEqual(summary["reps_inferred"], 1)
            self.assertFalse(summary["emails"][0]["sent"])

    def test_the_grace_period_expires_between_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary, transport, _ = self.run_pass(tmp)
            self.assertEqual(len(transport.sent), 5)
            self.assertEqual(summary["too_new"], 1)
            # Two hours later only the load that was too new at 10:00 goes out.
            summary, transport, _ = self.run_pass(tmp, now=NOW + timedelta(hours=2))
            self.assertEqual([e.load_ids for e, _ in transport.sent], [["344700"]])
            self.assertEqual(transport.sent[0][0].to, ["seth.edmondson@circledelivers.com"])
            self.assertEqual(summary["skipped_repeat"], 6)

    def test_send_records_state_and_repeats_only_after_the_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary, transport, _ = self.run_pass(tmp, html=STABLE)
            self.assertEqual(len(transport.sent), 5)
            self.assertEqual([(e["dispatch_reps"], e["sales_reps"]) for e in summary["emails"]],
                             [(["Kyle Link"], ["Kyle Link"]), (["Kyle Link"], ["Seth Edmondson"]), (["Podge Ormsby"], []),
                              (["Seth Edmondson"], ["Kyle Link"]), (["Victoria Storm"], ["Podge Ormsby", "Victoria Storm"])])
            with open(os.path.join(tmp, "sent_log.jsonl"), encoding="utf-8") as handle:
                lines = [json.loads(line) for line in handle]
            self.assertEqual(len(lines), 5)
            self.assertEqual(lines[1]["to"], ["kyle.link@circledelivers.com", "seth.edmondson@circledelivers.com"])
            self.assertEqual(lines[1]["message_id"], "msg-2")
            self.assertEqual(lines[4]["loads"], ["342198", "341700"])
            state = AlertState(os.path.join(tmp, "alert_state.json"))
            self.assertEqual(set(state.sent), set(DEFAULT_FLAGGED))
            self.assertEqual(state.sent["343796"], {"reasons": REASON_NO_ETA, "group": "kyle link|seth edmondson", "at": NOW.isoformat()})
            self.assertEqual(state.sent["341900"]["group"], "seth edmondson|kyle link")  # the inferred rep counts
            self.assertEqual(state.sent["342198"]["reasons"], f"{REASON_STALE_ETA}, {REASON_NO_SCHED}")

            # Two hours later: everything was alerted already, nothing goes out.
            summary, transport, _ = self.run_pass(tmp, now=NOW + timedelta(hours=2), html=STABLE)
            self.assertEqual(transport.sent, [])
            self.assertEqual(summary["skipped_repeat"], 6)

            # Next day: the same loads are repeated, and 342000 (same-day yesterday, pickup ETA passed) joins Kyle's email.
            summary, transport, _ = self.run_pass(tmp, now=NOW + timedelta(hours=24), html=STABLE)
            self.assertEqual(len(transport.sent), 5)
            self.assertEqual(next(e for e, _ in transport.sent if e.key == "Kyle Link|Kyle Link").load_ids, ["343786", "342000"])  # oldest status first

            # 343786 loses its scheduled date too: what is needed changed, alerted at once on its own.
            changed = build(without=("344700",), replace={"343786": dict(sched="")})
            summary, transport, _ = self.run_pass(tmp, now=NOW + timedelta(hours=26), html=changed)
            self.assertEqual(len(transport.sent), 1)
            self.assertEqual(transport.sent[0][0].load_ids, ["343786"])
            self.assertEqual(transport.sent[0][0].subject, "Pickup ETA alert: 1 load (1 with no pickup ETA, 1 not scheduled) - Kyle Link")

            # The ETA gets entered (for a day still ahead): the load simply drops out (Kyle's email still carries 342000).
            fixed = build(without=("344700",), replace={"343786": dict(eta=datetime(2026, 10, 16, 9, 0))})
            summary, transport, _ = self.run_pass(tmp, now=NOW + timedelta(hours=48), html=fixed)
            self.assertNotIn("343786", [lid for e, _ in transport.sent for lid in e.load_ids])
            self.assertEqual(len(transport.sent), 5)
            self.assertEqual(next(e for e, _ in transport.sent if e.key == "Kyle Link|Kyle Link").load_ids, ["342000"])

            # ... and comes back under the other rule the day after that ETA has passed, as a changed reason.
            summary, transport, _ = self.run_pass(tmp, now=datetime(2026, 10, 17, 11, 30), html=fixed)
            back = next(e for e, _ in transport.sent if "343786" in e.load_ids)
            self.assertIn("pickup ETA passed (Fri 10/16 09:00, 1 d 3 h ago)", back.text)

            # repeat_hours 0 means every run.
            summary, transport, _ = self.run_pass(tmp, now=datetime(2026, 10, 17, 12, 0), html=STABLE, repeat_hours=0)
            self.assertEqual(len(transport.sent), 5)

    def test_a_failed_send_does_not_mark_its_loads_or_stop_the_others(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary, transport, state = self.run_pass(tmp, transport=FakeTransport(fail_for="Victoria Storm"))
            self.assertEqual(len(transport.sent), 4)
            self.assertEqual(summary["send_errors"], ["Victoria Storm / Podge Ormsby, Victoria Storm: SES down"])
            self.assertNotIn("342198", state.sent)
            self.assertNotIn("341700", state.sent)
            self.assertIn("343786", state.sent)

    def test_redirected_send_records_nothing_and_only_rep_filters(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary, transport, _ = self.run_pass(tmp, redirect_to=["me@circledelivers.com"], only_rep="Kyle Link")
            self.assertEqual(len(transport.sent), 3)  # the groups with Kyle in a role, the inferred one included
            email = transport.sent[0][0]
            self.assertEqual(email.to, ["me@circledelivers.com"])
            self.assertEqual(email.cc, [])
            self.assertTrue(email.subject.startswith("[Test copy] Pickup ETA alert:"))
            self.assertIn("TEST COPY. This alert would have been sent To: kyle.link@circledelivers.com; Cc: justice.cromwell@circledelivers.com, smitchell@circledelivers.com.", email.text)
            self.assertFalse(os.path.exists(os.path.join(tmp, "alert_state.json")))
            self.assertEqual(summary["emails"][0]["redirected_to"], ["me@circledelivers.com"])
            self.assertFalse(summary["emails"][0]["sent"])

            summary, transport, _ = self.run_pass(tmp, only_rep="Podge Ormsby", send=False)
            self.assertEqual([e.load_ids for e, _ in transport.sent], [["341000"], ["342198", "341700"]])

    def test_rules_pass_through(self):
        with tempfile.TemporaryDirectory() as tmp:
            summary, transport, _ = self.run_pass(tmp, send=False, min_age_hours=0, check_sched=False, stale_eta_hours=48, statuses=("Dispatched",))
            ids = sorted(lid for e, _ in transport.sent for lid in e.load_ids)
            self.assertEqual(ids, ["341000", "341700", "341900", "342198", "343786", "343796", "344700"])
            self.assertEqual(summary["by_reason"], {REASON_NO_ETA: 4, REASON_STALE_ETA: 3, REASON_NO_SCHED: 0})
            summary, transport, _ = self.run_pass(tmp, send=False, check_stale=False, check_sched=False)
            self.assertEqual(sorted(lid for e, _ in transport.sent for lid in e.load_ids), ["341000", "343786", "343796"])
            # Without inference Seth's load goes out on its own, To Seth alone.
            summary, transport, _ = self.run_pass(tmp, send=False, infer=False)
            alone = next(e for e, _ in transport.sent if e.load_ids == ["341900"])
            self.assertEqual((alone.to, alone.key), (["seth.edmondson@circledelivers.com"], "Seth Edmondson|nobody"))
            self.assertEqual(summary["reps_inferred"], 0)
            self.assertEqual(len(transport.sent), 5)


class CliTests(unittest.TestCase):
    def test_offline_dry_run_from_saved_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            page_path = os.path.join(tmp, "tracking.html")
            with open(page_path, "w", encoding="utf-8") as handle:
                handle.write(PAGE)
            reps_csv = os.path.join(tmp, "reps.csv")
            with open(reps_csv, "w", encoding="utf-8") as handle:
                handle.write("name,email,title,phone\nLINK, KYLE,kyle.link@circledelivers.com,Sr. Account Manager,(260) 319-8153\n"
                             "EDMONDSON, SETH,seth.edmondson@circledelivers.com,Sr Account Manager,(260) 319-5659\n")
            outbox = os.path.join(tmp, "outbox")
            summary_path = os.path.join(tmp, "summary.json")
            rc = main(["--from-html", page_path, "--reps", reps_csv, "--now", NOW.isoformat(),
                       "--outbox", outbox, "--state", os.path.join(tmp, "state.json"), "--sent-log", os.path.join(tmp, "log.jsonl"),
                       "--rep-cache", os.path.join(tmp, "cache.json"), "--customer-reps", os.path.join(tmp, "customers.json"), "--json", summary_path])
            self.assertEqual(rc, 0)
            files = sorted(os.listdir(outbox))
            # Victoria is not in this reps.csv, so her loads join Podge's in the one email to the managers; Jack is not in it
            # either, so Kyle + Seth and Seth + Kyle have the same recipients and become one email.
            self.assertEqual(sorted(f.split("_", 1)[1] for f in files),
                             ["kyle-link-seth-edmondson_seth-edmondson-kyle-link.txt", "kyle-link_kyle-link.txt",
                              "podge-ormsby-victoria-storm_podge-ormsby-victoria-storm.txt"])
            with open(os.path.join(outbox, [f for f in files if f.endswith("seth-edmondson-kyle-link.txt")][0]), encoding="utf-8") as handle:
                pair = handle.read()
            self.assertIn("From: Circle Logistics <vehicles@circledelivers.com>", pair)
            self.assertIn("To: kyle.link@circledelivers.com, seth.edmondson@circledelivers.com", pair)
            self.assertIn("Cc: justice.cromwell@circledelivers.com, smitchell@circledelivers.com", pair)
            self.assertIn("Subject: Pickup ETA alert: 2 loads (1 with no pickup ETA, 1 past the pickup ETA) - Kyle Link, Seth Edmondson", pair)
            self.assertIn("Hi Kyle and Seth,", pair)
            self.assertFalse(os.path.exists(os.path.join(tmp, "state.json")))
            self.assertTrue(os.path.exists(os.path.join(tmp, "customers.json")))
            with open(summary_path, encoding="utf-8") as handle:
                summary = json.load(handle)
            self.assertEqual(summary["flagged"], 6)
            self.assertEqual(len(summary["emails"]), 3)
            routed = [e for e in summary["emails"] if "Victoria Storm" in e["dispatch_reps"]][0]
            self.assertEqual(routed["to"], CC)
            self.assertEqual(routed["loads"], ["342198", "341700", "341000"])  # oldest status first

    def test_offline_run_without_the_rep_columns_uses_the_page_lookup(self):
        with tempfile.TemporaryDirectory() as tmp:
            page_path = os.path.join(tmp, "tracking.html")
            with open(page_path, "w", encoding="utf-8") as handle:
                handle.write(PAGE_NO_REPS)
            reps_json = os.path.join(tmp, "reps_by_load.json")
            with open(reps_json, "w", encoding="utf-8") as handle:
                json.dump({lid: {"sales_rep": l.sales_rep, "sales_rep2": l.sales_rep2, "dispatch_rep": l.dispatch_rep}
                           for lid, l in LOAD_PAGES.items()}, handle)
            listing = os.path.join(tmp, "flagged.json")
            rc = main(["--from-html", page_path, "--reps-json", reps_json, "--now", NOW.isoformat(), "--list", "--json", listing,
                       "--rep-cache", os.path.join(tmp, "cache.json"), "--customer-reps", os.path.join(tmp, "customers.json"),
                       "--outbox", os.path.join(tmp, "outbox")])
            self.assertEqual(rc, 0)
            with open(listing, encoding="utf-8") as handle:
                data = json.load(handle)
            by_id = {l["load_id"]: l for l in data["loads"]}
            self.assertEqual(by_id["343796"]["sales_rep"], "EDMONDSON, SETH")
            self.assertEqual(by_id["343796"]["rep_source"], "page")
            self.assertIsNone(by_id["341900"]["sales_rep"])  # the other loads' reps are unknown without the columns, nothing to infer from

    def test_list_mode_prints_and_composes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            page_path = os.path.join(tmp, "tracking.html")
            with open(page_path, "w", encoding="utf-8") as handle:
                handle.write(PAGE)
            listing = os.path.join(tmp, "flagged.json")
            rc = main(["--from-html", page_path, "--now", NOW.isoformat(), "--list", "--json", listing,
                       "--rep-cache", os.path.join(tmp, "cache.json"), "--customer-reps", os.path.join(tmp, "customers.json"),
                       "--outbox", os.path.join(tmp, "outbox")])
            self.assertEqual(rc, 0)
            self.assertFalse(os.path.exists(os.path.join(tmp, "outbox")))
            with open(listing, encoding="utf-8") as handle:
                data = json.load(handle)
            self.assertEqual([l["load_id"] for l in data["loads"]], DEFAULT_FLAGGED)
            self.assertEqual(data["loads"][0]["age_hours"], 73.6)
            self.assertEqual(data["loads"][0]["reasons"], [REASON_NO_ETA])
            self.assertEqual(data["loads"][0]["rep_source"], "grid")
            inferred = [l for l in data["loads"] if l["sales_rep_inferred"]]
            self.assertEqual([(l["load_id"], l["sales_rep"]) for l in inferred], [("341900", "KYLE LINK")])
            self.assertEqual((data["counts"]["too_new"], data["counts"]["same_day"]), (1, 1))


class LambdaArgsTests(unittest.TestCase):
    def test_env_to_args(self):
        import pickup_eta_lambda

        env = {"PICKUP_SEND": "1", "PICKUP_REPEAT_HOURS": "12", "PICKUP_MIN_AGE_HOURS": "2", "PICKUP_STATUS": "Dispatched",
               "PICKUP_STALE_ETA_HOURS": "48", "PICKUP_STALE_STATUSES": "Dispatched,In-Route", "PICKUP_CC": "a@x.com,b@x.com",
               "PICKUP_SKIP_SCHED_CHECK": "1", "PICKUP_SKIP_STALE_CHECK": "1", "PICKUP_INCLUDE_SAME_DAY": "1", "PICKUP_NO_INFER_SALES_REP": "1",
               "PICKUP_SAVE_PAGES": "1", "MAIL_FROM": "Me <me@x.com>"}
        args = pickup_eta_lambda.alert_args("/tmp/w", env)
        for switch in ("--send", "--skip-sched-check", "--skip-stale-check", "--include-same-day", "--no-infer-sales-rep"):
            self.assertIn(switch, args)
        self.assertEqual(args[args.index("--repeat-hours") + 1], "12")
        self.assertEqual(args[args.index("--min-age-hours") + 1], "2")
        self.assertEqual(args[args.index("--status") + 1], "Dispatched")
        self.assertEqual(args[args.index("--stale-eta-hours") + 1], "48")
        self.assertEqual(args[args.index("--stale-statuses") + 1], "Dispatched,In-Route")
        self.assertEqual(args[args.index("--cc") + 1], "a@x.com,b@x.com")
        self.assertEqual(args[args.index("--from") + 1], "Me <me@x.com>")
        self.assertEqual(args[args.index("--reps") + 1], os.path.join("/tmp/w", "config", "reps.csv"))
        self.assertEqual(args[args.index("--customer-reps") + 1], os.path.join("/tmp/w", "state", "customer_reps.json"))
        self.assertEqual(args[args.index("--save-html") + 1], os.path.join("/tmp/w", "pages"))
        args = pickup_eta_lambda.alert_args("/tmp/w", {})
        for switch in ("--send", "--skip-sched-check", "--skip-stale-check", "--include-same-day", "--no-infer-sales-rep", "--status", "--stale-statuses", "--cc"):
            self.assertNotIn(switch, args)
        self.assertEqual(args[args.index("--repeat-hours") + 1], "24")
        self.assertEqual(args[args.index("--min-age-hours") + 1], "1")
        self.assertEqual(args[args.index("--stale-eta-hours") + 1], "2")
        self.assertEqual(args[args.index("--from") + 1], "Circle Logistics <vehicles@circledelivers.com>")


if __name__ == "__main__":
    unittest.main()
