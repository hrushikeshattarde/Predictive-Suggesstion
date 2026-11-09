"""Unit tests for tracking_update_alerts.py (``python -m unittest`` from the
tracking_update_alerts folder). No network: the tracking screen is a synthetic grid shaped
like the live one (inside its Menu_alt form, with the Dispatch Comment (Add) boxes and
the extra rep/customer columns the job asks for), the GPS pages are synthetic ping
tables, and the transport and the comment poster record what they were given."""

import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.dirname(HERE)
ROOT = os.path.dirname(FOLDER)
for path in (ROOT, os.path.join(ROOT, "due_date_alerts"), FOLDER):
    if path not in sys.path:
        sys.path.insert(0, path)

from cargotel_search import parse_grid  # noqa: E402
from send_drafts import load_rep_details  # noqa: E402
from tracking_update_alerts import (  # noqa: E402
    ACTIVE,
    DEFAULT_ESCALATE,
    DEFAULT_REPORT_TO,
    DEFAULT_STELLANTIS_TO,
    NEVER,
    STALE,
    UNKNOWN,
    AlertState,
    CommentState,
    Ping,
    build_comment_post,
    check_gps,
    classify,
    comment_text,
    compose,
    compose_report,
    customer_excluded,
    eta_cutoff,
    form_fields,
    gps_params,
    grid_params,
    load_from_row,
    mail_groups,
    main,
    needs_comment,
    nice_place,
    parse_ping_stamp,
    parse_pings,
    parse_report_hours,
    parse_stamp,
    plan_comments,
    report_breakdowns,
    report_bucket,
    report_due,
    report_loads,
    run_once,
    select_loads,
    verify_comments,
)
import tracking_update_lambda  # noqa: E402

EST = timezone(timedelta(hours=-5))
NOW = datetime(2026, 11, 6, 9, 0, tzinfo=EST)

COLUMNS = [
    ("sales_rep1", "Sales Rep 1"), ("sales_rep2", "Sales Rep 2"), ("original_consignor", "Original Consignor"),
    ("cons_id", "Customer ID#"), ("consignor", "Customer"), ("dispatch_rep", "Dispatch Rep"), ("status", "Status"),
    ("bundleparent", "Load ID"), ("load_id", "Order ID"), ("carriername", "Carrier Name"), ("originCityState", "Origin City, State"),
    ("destination", "Destination"), ("pickiup_eta_datetime", "Pickup ETA"), ("driver_name", "Driver Name"),
    ("driver_cell_phone", "Drivers Cell Phone"), ("GPSLink", "GPS City/St Link"), ("currentlocation", "Current Location"),
    ("current_location_eta", "Current Location / ETA (Edit)"), ("GPSPosition", "GPS Position"), ("Mode", "Transit Mode"),
    ("transitdays", "Transit Time"), ("DispatchCmt_IDAdd", "Dispatch Comment (Add)"), ("DispatchCmt_IDLastCmt", "Last Dispatch Comment"),
]


def header(columns=COLUMNS):
    return ('<tr static="" id="det_header_row" class="unshadow dv_row"><th class="dv_cell dv_string dv_header oeheader" id="row_number_head">#</th>'
            + "".join(f'<th class="dv_cell dv_cell_{key} dv_header dv_header_{key} oeheader" id="det_header_{key}"><span class="dv_header_name">{label}</span></th>'
                      for key, label in columns) + "</tr>")


def section(text):
    return f'<tr class="dv_section_header"><td colspan="24">{text}</td></tr>'


def eta_widget(load_id):
    """The live "Current Location / ETA (Edit)" cell, trimmed: a text input and the month select."""

    return (f'Location: <INPUT size="20" id="Control__seteta__{load_id}__loc" value="" name="Control__seteta__{load_id}__loc"><br>ETA: '
            f'<SELECT name="Control__seteta__{load_id}__month_eta" id="Control__seteta__{load_id}__month_eta"><OPTION value="">'
            f'<OPTION value="10">Oct<OPTION value="11" SELECTED>Nov</SELECT>/'
            f'<SELECT name="Control__seteta__{load_id}__day_eta"><OPTION value=""><OPTION value="1">1<OPTION value="9">9</SELECT>'
            f'<INPUT type="hidden" name="eta_date_edit" value="{load_id}">')


def comment_widget(load_id):
    return (f'<TEXTAREA maxlength="255" name="Control__DispatchCmt_ID__{load_id}__comment" rows="3" cols="40" '
            f'id="Control__DispatchCmt_ID__{load_id}__comment"></TEXTAREA>'
            f'<INPUT id="Control__DispatchCmt_ID__id" type="hidden" value="{load_id}" name="Control__DispatchCmt_ID__id">')


def last_comment(at, by, text):
    if not text:
        return ""
    return f'<font size="1">{at}</font>\n<font color="red" size="1">{by}</font>\n<p>{text}</p>\n<hr>'


def row(n, load_id, dispatch_rep, eta, *, status="Dispatched", customer="Stellantis (fca)", cons_id="62943", consignor="N/A",
        sales1="", sales2="", carrier="100 Miles Transport LLC", origin="Elwood, IL", dest_name="Dodge Of Burnsville Inc",
        dest="Burnsville, MN", driver="OYUNBAATAR, ERDENETUVSHIN", phone="(224) 284-9908", link_class="LinkBundleParent",
        gps=True, location="ELWOOD, IL", comment=("", "", ""), columns=COLUMNS):
    values = {
        "sales_rep1": sales1, "sales_rep2": sales2, "original_consignor": customer, "cons_id": cons_id, "consignor": consignor,
        "dispatch_rep": dispatch_rep, "status": status, "bundleparent": "", "carriername": carrier, "originCityState": origin,
        "destination": (f"<b>{dest_name}</b><br>{dest}" if dest_name else dest), "pickiup_eta_datetime": eta, "driver_name": driver,
        "driver_cell_phone": phone,
        "GPSLink": f'\n                <a href="/backoffice/gpshistory.mcgi?load_id={load_id}" target="_GPS">GPS City&amp;St</a>\n' if gps else "",
        "currentlocation": location, "current_location_eta": eta_widget(load_id), "GPSPosition": "", "Mode": "", "transitdays": "",
        "DispatchCmt_IDAdd": comment_widget(load_id), "DispatchCmt_IDLastCmt": last_comment(*comment),
        "load_id": (f'<A name="MapId_{load_id}" class="{link_class}" style="color: blue" href="/backoffice/loadmaint.mcgi?load_id={load_id}" target="_blank" >{load_id}</A>'
                    f'<input type=\'hidden\' name=\'load_id\' value="{load_id}">'),
    }
    cells = [f'<td class="dv_cell listable_cell dv_plain dv_cell_{key}" id="DetCell_{n}_{col}">{values[key]}</td>' for col, (key, _label) in enumerate(columns)]
    return f'<tr class="unshadow dv_row listable_row" id="det_row_{n}"><td class="dv_linenumber">{n}</td>' + "".join(cells) + "</tr>"


OLD_PING = "2026-11-05 10:19:29-05 Burnsville, MN"
SPECS = {
    # Stellantis, Kaleb dispatch, Justice sales; pinging (last ping 06:12 EST): comment the ping. Justice's last comment is an older ping.
    "352242": dict(n=1, load_id="352242", dispatch_rep="KALEB HAMMAN", eta="11/06/2026 20:00", sales1="JUSTICE CROMWELL",
                   driver="TSOGTSAIKHAN, TSOLMON", phone="(312) 714-5408", comment=("2026-11-05 15:14 EST", "JUSTICE CROMWELL", OLD_PING)),
    # Stellantis, Kaleb; last ping yesterday afternoon: stale -> Stellantis email to Kaleb + the mailbox.
    "352243": dict(n=2, load_id="352243", dispatch_rep="KALEB HAMMAN", eta="11/06/2026 20:00", sales1="JUSTICE CROMWELL"),
    # Stellantis, Joseph (no email in reps.csv); never pinged: the mailbox alone gets it, with a note.
    "354745": dict(n=3, load_id="354745", dispatch_rep="JOSEPH CAMILLERI", eta="11/06/2026 00:00", sales1="JUSTICE CROMWELL",
                   carrier="312 Hauling LLC", dest_name="Griffins Hub Chrysler Jeep Dodge", dest="Greenfield, WI", driver="WALKER, KENNETH"),
    # Avis: Kyle dispatch and sales (manager Justice); pickup ETA tomorrow (date only, before the Nov 8 cutoff); stale.
    "360001": dict(n=4, load_id="360001", dispatch_rep="KYLE LINK", eta="11/07/2026 00:00", customer="Avis Trucks",
                   cons_id="70001", consignor="Avis Trucks", sales1="KYLE LINK", carrier="Jra Transport Inc",
                   origin="Kansas City, MO", dest_name="", dest="Ashland, MO", driver="DOE, JOHN", phone="(816) 555-0101", link_class="LinkIndividual"),
    # Kingbee: Victoria dispatch, no sales rep; never pinged -> Victoria alone, noted.
    "360002": dict(n=5, load_id="360002", dispatch_rep="VICTORIA STORM", eta="11/05/2026 00:00", customer="Kingbee Rentals, LLC", cons_id="82722",
                   carrier="Kolos Inc", origin="Salt Lake City, UT", dest_name="", dest="Denver, CO", driver="", phone=""),
    # Enterprise: Podge dispatch and sales (no email); stale -> escalated.
    "360003": dict(n=6, load_id="360003", dispatch_rep="PODGE ORMSBY", eta="11/04/2026 00:00", customer="Enterprise Rent-a-car Company Of Utah, LLC",
                   cons_id="86219", sales1="PODGE ORMSBY", carrier="Mwi LLC", driver="SMITH, ANN"),
    # Pickup ETA on the cutoff day: not on the screen yet.
    "360004": dict(n=7, load_id="360004", dispatch_rep="KYLE LINK", eta="11/08/2026 00:00", customer="Avis Trucks",
                   cons_id="70001", sales1="KYLE LINK"),
    # No pickup ETA at all: not on the screen.
    "360005": dict(n=8, load_id="360005", dispatch_rep="KYLE LINK", eta="", customer="Avis Trucks", cons_id="70001", sales1="KYLE LINK"),
    # Avis, Kyle; pinging, and the last comment already carries that ping: nothing to add.
    "360006": dict(n=9, load_id="360006", dispatch_rep="KYLE LINK", eta="11/06/2026 10:00", customer="Avis Trucks",
                   cons_id="70001", sales1="KYLE LINK", comment=("2026-11-06 08:40 EST", "RUBICON BOT", "2026-11-06 08:31:00-05 Joliet, IL")),
    # Bundle member: skipped.
    "360007": dict(n=10, load_id="360007", dispatch_rep="KYLE LINK", eta="11/06/2026 10:00", link_class="LinkBundleChild"),
    # In-Route: not a status the alerts check; scanned for the report only (never pinged).
    "360008": dict(n=11, load_id="360008", dispatch_rep="KYLE LINK", eta="11/06/2026 10:00", status="In-Route", customer="Kar Korner, Inc.", cons_id="90888",
                   sales1="KYLE LINK", carrier="Lada Trans Inc", origin="Ocilla, GA", dest_name="", dest="Medina, OH", driver="MOSS, WALTER", phone="(586) 234-3689"),
    # In-Route, pinging: in the report's counts as "without issues", nowhere else.
    "360011": dict(n=14, load_id="360011", dispatch_rep="VICTORIA STORM", eta="11/06/2026 12:00", status="In-Route", customer="Kingbee Rentals, LLC",
                   cons_id="82722", carrier="Kolos Inc", origin="Boise, ID", dest_name="", dest="Denver, CO", driver="ROE, JANE", phone="(208) 555-0199"),
    # GPS page missing: unknown, never emailed.
    "360009": dict(n=12, load_id="360009", dispatch_rep="JACK GIBSON", eta="11/06/2026 10:00", customer="Kar Korner, Inc.", cons_id="90888",
                   sales1="VICTORIA STORM", sales2="KYLE LINK"),
    # Kar Korner: Jack dispatch, Victoria sales (no manager set), Kyle secondary; stale -> To Jack + Victoria, Cc Kyle, note about the manager.
    "360010": dict(n=13, load_id="360010", dispatch_rep="JACK GIBSON", eta="11/06/2026 10:00", customer="Kar Korner, Inc.", cons_id="90888",
                   consignor="Kar Korner, Inc.", sales1="VICTORIA STORM", sales2="KYLE LINK", carrier="Lada Trans Inc", driver="LEE, BOB"),
}
LAYOUT = [("Carrier: 100 MILES TRANSPORT LLC", ["352242", "352243"]), ("Carrier: 312 HAULING LLC", ["354745"]),
          ("Carrier: JRA TRANSPORT INC", ["360001", "360004", "360005", "360006", "360007", "360008"]),
          ("Carrier: KOLOS INC", ["360002", "360011"]), ("Carrier: MWI LLC", ["360003"]), ("Carrier: LADA TRANS INC", ["360009", "360010"])]

FORM_HEAD = ('<FORM name="Menu_alt" method="POST" action="?" id="Menu_alt" autocomplete="off">'
             '<INPUT type="hidden" name="FORM::DEF" value="Menu"><INPUT type="hidden" name="Menu__SUBMITTED" value="1">'
             '<INPUT type="hidden" name="Menu__DateMne_PickupETA" value="Before"><INPUT type="hidden" name="Menu__day_DateStart_PickupETA" value="7">'
             '<INPUT type="hidden" name="Control__saved_id" value="2077"><INPUT type="hidden" name="Menu__ViewFields" value="dispatch_rep">'
             '<INPUT type="hidden" name="Menu__ViewFields" value="status"><INPUT CHECKED type="checkbox" name="Menu__CarrierAdd" value="Y">'
             '<INPUT type="checkbox" name="Menu__NotChecked" value="Y"><SELECT name="Menu__Multi" multiple><OPTION value="a">a<OPTION value="b">b</SELECT>'
             '<SELECT name="Menu__perPage"><OPTION value="100">100<OPTION value="All" SELECTED>All</SELECT>'
             '<INPUT name="Menu__is_submitted" value="Submit" type="submit"><INPUT type="button" name="x" value="Tabstop By Row">')
FORM_TAIL = '<INPUT name="Menu__is_submitted" value="Submit" type="submit"></FORM>'


def build(columns=COLUMNS, *, without=(), replace=None, form=True):
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
    grid = "<table>" + header(columns) + "".join(parts) + "</table><div>Rows 1 - 14 of 14</div>"
    body = (FORM_HEAD + grid + FORM_TAIL) if form else grid
    return f"<html><head><title>Kesh: Tracking Update Screen</title></head><body>{body}</body></html>"


PAGE = build()


def gps_page(pings):
    rows = "".join(f"<tr>\n<td>{who}</td>\n<td>{stamp}</td>\n<td>{where} </td>\n<td align=center><font color=red>N</font></td>\n</tr>"
                   for who, stamp, where in pings)
    return ("<html><body><h3>Driver GPS History</h3><form method=\"post\"><table><tr><td>Drivers:</td><td><select name=\"drivers\"></select></td></tr></table>"
            "<table>\n<tr>\n<th>User Name</th><th>Timestamp</th><th>Location</th><th>Cell Coverage<th>\n</tr>" + rows
            + "</table></form>Percentage of Time When Out of Cell Coverage: 56.7</body></html>")


GPS = {
    "352242": gps_page([("Tsogtsaikhan, Tsolmon", "2026-11-05 10:19:29-05", "Burnsville,MN"), ("Tsogtsaikhan, Tsolmon", "2026-11-06 03:38:33-05", "Belvidere,IL"),
                        ("Tsogtsaikhan, Tsolmon", "2026-11-06 06:12:25-05", "Oak Lawn,IL")]),
    "352243": gps_page([("Oyunbaatar, Erdenetuvshin", "2026-11-05 09:01:31-05", "Campton Hills,IL"), ("Oyunbaatar, Erdenetuvshin", "2026-11-05 14:00:00-05", "Rockford,IL")]),
    "354745": gps_page([]),
    "360001": gps_page([("Doe, John", "2026-11-06 04:30:00-05", "Columbia,MO")]),
    "360002": gps_page([]),
    "360003": gps_page([("Smith, Ann", "2026-11-03 12:00:00-05", "Ogden,UT")]),
    "360006": gps_page([("Doe, John", "2026-11-06 08:31:00-05", "Joliet,IL")]),
    "360008": gps_page([]),
    "360011": gps_page([("Roe, Jane", "2026-11-06 08:45:00-05", "Cheyenne,WY")]),
    "360010": gps_page([("Lee, Bob", "2026-11-06 01:00:00-05", "Gary,IN")]),
}

REPS = {
    "kyle link": {"email": "kyle.link@circledelivers.com", "title": "Sr. Account Manager", "phone": "", "manager": "justice.cromwell@circledelivers.com"},
    "justice cromwell": {"email": "justice.cromwell@circledelivers.com", "title": "", "phone": "", "manager": ""},
    "victoria storm": {"email": "victoria.storm@circledelivers.com", "title": "", "phone": "", "manager": ""},
    "jack gibson": {"email": "jack.gibson@circledelivers.com", "title": "", "phone": "", "manager": "brett.booth@circledelivers.com"},
    "kaleb hamman": {"email": "kaleb.hamman@circledelivers.com", "title": "", "phone": "", "manager": "justice.cromwell@circledelivers.com"},
}
SELECTED = ["352242", "352243", "354745", "360001", "360002", "360003", "360006", "360009", "360010"]
#: What a run writes as dispatch comments: the last ping of every active or stale load that
#: does not carry it yet (360006 already does; 354745 and 360002 never pinged; 360009 is unreadable).
STALE_AND_ACTIVE_COMMENTS = {
    "352242": "2026-11-06 06:12:25-05 Oak Lawn, IL",
    "352243": "2026-11-05 14:00:00-05 Rockford, IL",
    "360001": "2026-11-06 04:30:00-05 Columbia, MO",
    "360003": "2026-11-03 12:00:00-05 Ogden, UT",
    "360010": "2026-11-06 01:00:00-05 Gary, IN",
}


def fetch_gps(load):
    try:
        return GPS[load.load_id]
    except KeyError:
        raise RuntimeError(f"no GPS page for {load.load_id}")


class FakeTransport:
    def __init__(self, fail_for=None):
        self.sent = []
        self.fail_for = fail_for

    def send(self, email, from_addr):
        if self.fail_for and email.rep == self.fail_for:
            raise RuntimeError("SES down")
        self.sent.append((email, from_addr))
        return f"msg-{len(self.sent)}"


class FakePoster:
    def __init__(self, fail=(), explode=False):
        self.calls = []
        self.fail = set(fail)
        self.explode = explode

    def post(self, page_html, comments):
        self.calls.append(dict(comments))
        if self.explode:
            raise RuntimeError("CargoTel rejected the comment form")
        return {load_id: load_id not in self.fail for load_id in comments}


def checked_loads(html=PAGE, now=NOW, **kw):
    loads, warnings, counts = select_loads(parse_grid(html), now, **kw)
    errors = check_gps(loads, fetch_gps, now)
    return loads, warnings, counts, errors


def by_id(loads):
    return {l.load_id: l for l in loads}


def scanned_loads(now=NOW):
    """The alert loads plus the In-Route ones, GPS-checked: what the report sees."""
    loads, _w, counts = select_loads(parse_grid(PAGE), now)
    extra, _w2, extra_counts = select_loads(parse_grid(PAGE), now, statuses=("In-Route",))
    check_gps(loads + extra, fetch_gps, now)
    return loads + extra, {"excluded": counts["excluded"] + extra_counts["excluded"]}


class ParsingTests(unittest.TestCase):
    def test_parse_stamp(self):
        self.assertEqual(parse_stamp("11/06/2026 20:00"), datetime(2026, 11, 6, 20, 0))
        self.assertEqual(parse_stamp("11/06/2026 00:00"), datetime(2026, 11, 6))
        self.assertIsNone(parse_stamp(""))
        self.assertIsNone(parse_stamp("tomorrow"))

    def test_parse_ping_stamp(self):
        when = parse_ping_stamp("2026-11-06 06:12:25-05")
        self.assertEqual(when, datetime(2026, 11, 6, 6, 12, 25, tzinfo=EST))
        self.assertEqual(when.utcoffset(), timedelta(hours=-5))
        self.assertEqual(parse_ping_stamp("2026-11-06 06:12:25+05:30").utcoffset(), timedelta(hours=5, minutes=30))
        self.assertEqual(parse_ping_stamp("2026-11-06 06:12:25", EST), datetime(2026, 11, 6, 6, 12, 25, tzinfo=EST))
        self.assertIsNone(parse_ping_stamp("yesterday"))

    def test_nice_place(self):
        self.assertEqual(nice_place("Oak Lawn,IL "), "Oak Lawn, IL")
        self.assertEqual(nice_place("S Chicago Hei , IL"), "S Chicago Hei, IL")
        self.assertEqual(nice_place(""), "")

    def test_parse_pings_oldest_first(self):
        pings = parse_pings(GPS["352242"])
        self.assertEqual([p.where for p in pings], ["Burnsville, MN", "Belvidere, IL", "Oak Lawn, IL"])
        self.assertEqual(pings[-1].stamp, "2026-11-06 06:12:25-05")
        self.assertEqual(pings[-1].who, "Tsogtsaikhan, Tsolmon")
        self.assertEqual(pings[-1].when, datetime(2026, 11, 6, 6, 12, 25, tzinfo=EST))
        self.assertEqual(parse_pings(GPS["354745"]), [])
        self.assertEqual(parse_pings("<html><body>no table</body></html>"), [])

    def test_parse_pings_sorts_out_of_order_rows(self):
        page = gps_page([("A", "2026-11-06 06:00:00-05", "Later,IL"), ("A", "2026-11-06 05:00:00-05", "Earlier,IL"), ("A", "garbage", "X,Y")])
        self.assertEqual([p.where for p in parse_pings(page)], ["Earlier, IL", "Later, IL"])

    def test_eta_cutoff_is_today_plus_days_at_midnight(self):
        self.assertEqual(eta_cutoff(NOW), datetime(2026, 11, 8))
        self.assertEqual(eta_cutoff(datetime(2026, 11, 7, 23, 59, tzinfo=EST), 2), datetime(2026, 11, 9))
        self.assertEqual(eta_cutoff(NOW, 0), datetime(2026, 11, 6))

    def test_default_window_matches_the_gps_link(self):
        from tracking_update_alerts import DEFAULT_GPS_DAYS
        self.assertEqual(DEFAULT_GPS_DAYS, 2)  # the page's own default: a rep clicking the link sees the same 48 hours

    def test_gps_and_grid_params(self):
        params = gps_params("352242", datetime(2026, 10, 23, 9, 0), datetime(2026, 11, 7, 9, 0))
        self.assertEqual(params["load_id"], "352242")
        self.assertEqual((params["gpshistory_form__month_start_date"], params["gpshistory_form__day_start_date"]), ("10", "23"))
        self.assertEqual((params["gpshistory_form__year_end_date"], params["gpshistory_form__hour_end_date"]), ("2026", "9"))
        grid = grid_params(NOW)
        self.assertEqual((grid["Menu__month_DateStart_PickupETA"], grid["Menu__day_DateStart_PickupETA"], grid["Menu__year_DateStart_PickupETA"]), ("11", "8", "2026"))
        self.assertNotIn("Menu__DateMne_PickupETA", grid)
        self.assertIn("original_consignor", grid["Menu__ViewFields"])


class LoadTests(unittest.TestCase):
    def test_load_from_row_reads_the_cells(self):
        grid = parse_grid(PAGE)
        rows = {r.load_id: r for r in grid.rows}
        load = load_from_row(rows["352242"])
        self.assertEqual((load.customer, load.customer_id, load.kind), ("Stellantis (fca)", "62943", "load"))
        self.assertTrue(load.is_stellantis)
        self.assertEqual(load.dispatch_rep, "KALEB HAMMAN")
        self.assertEqual(load.sales_rep, "JUSTICE CROMWELL")
        self.assertEqual((load.dest_name, load.dest), ("Dodge Of Burnsville Inc", "Burnsville, MN"))
        self.assertEqual(load.gps_url, "https://circle.cargotel.com/backoffice/gpshistory.mcgi?load_id=352242")
        self.assertEqual(load.pickup_eta, datetime(2026, 11, 6, 20, 0))
        self.assertEqual((load.last_comment_at, load.last_comment_by, load.last_comment), ("2026-11-05 15:14 EST", "JUSTICE CROMWELL", OLD_PING))
        self.assertEqual(load.carrier, "100 Miles Transport LLC")
        self.assertEqual(load.group_key, ("kaleb hamman", "stellantis"))
        merchants = load_from_row(rows["360001"])
        self.assertFalse(merchants.is_stellantis)
        self.assertEqual((merchants.customer, merchants.kind, merchants.dest_name, merchants.dest), ("Avis Trucks", "order", "", "Ashland, MO"))
        self.assertEqual(merchants.group_key, ("kyle link", "kyle link"))
        self.assertEqual(merchants.last_comment, "")

    def test_stellantis_by_name_when_id_is_missing(self):
        page = build(replace={"352242": dict(cons_id="", customer="STELLANTIS (FCA)")})
        load = by_id([load_from_row(r) for r in parse_grid(page).rows])["352242"]
        self.assertTrue(load.is_stellantis)
        page = build(replace={"352242": dict(cons_id="", customer="N/A", consignor="N/A")})
        load = by_id([load_from_row(r) for r in parse_grid(page).rows])["352242"]
        self.assertEqual(load.customer, "")
        self.assertFalse(load.is_stellantis)

    def test_select_loads_applies_the_screen_rule(self):
        loads, warnings, counts = select_loads(parse_grid(PAGE), NOW)
        self.assertEqual(sorted(l.load_id for l in loads), SELECTED)
        self.assertEqual(counts, {"rows": 13, "in_status": 11, "excluded": 0, "no_eta": 1, "eta_later": 1, "selected": 9})
        self.assertEqual(warnings, [])
        loads, _w, _c = select_loads(parse_grid(PAGE), NOW, days_ahead=0)
        self.assertNotIn("360001", [l.load_id for l in loads])
        loads, _w, _c = select_loads(parse_grid(PAGE), NOW, statuses=("In-Route",))
        self.assertEqual([l.load_id for l in loads], ["360008", "360011"])

    def test_excluded_customers_are_left_out_of_the_run(self):
        merchants = dict(customer="Merchants Automotive Group Holdings LLC", cons_id="81464", consignor="N/A")
        page = build(replace={"360001": merchants, "360006": dict(merchants, cons_id="")})
        loads, _w, counts = select_loads(parse_grid(page), NOW)
        self.assertNotIn("360001", [l.load_id for l in loads])  # by id
        self.assertNotIn("360006", [l.load_id for l in loads])  # by name
        self.assertEqual(counts["excluded"], 2)
        self.assertEqual(counts["selected"], 7)
        loads, _w, counts = select_loads(parse_grid(page), NOW, exclude_customers=())
        self.assertIn("360001", [l.load_id for l in loads])
        self.assertEqual(counts["excluded"], 0)
        loads, _w, counts = select_loads(parse_grid(page), NOW, exclude_customers=("kingbee",))
        self.assertNotIn("360002", [l.load_id for l in loads])
        self.assertIn("360001", [l.load_id for l in loads])
        load = by_id([load_from_row(r) for r in parse_grid(page).rows])["360001"]
        self.assertTrue(customer_excluded(load, ("81464",)))
        self.assertTrue(customer_excluded(load, ("MERCHANTS automotive",)))
        self.assertFalse(customer_excluded(load, ("", "   ", "62943", "Stellantis")))

    def test_select_loads_warns_and_refuses(self):
        page = build(replace={"352242": dict(eta="soon", gps=False), "352243": dict(gps=False)})
        loads, warnings, _c = select_loads(parse_grid(page), NOW)
        self.assertNotIn("352242", [l.load_id for l in loads])
        self.assertTrue(any("unreadable pickup ETA" in w for w in warnings))
        self.assertTrue(any("352243: no GPS link" in w for w in warnings))
        columns = [c for c in COLUMNS if c[0] != "GPSLink"]
        with self.assertRaises(RuntimeError):
            select_loads(parse_grid(build(columns)), NOW)


class GpsTests(unittest.TestCase):
    def test_classify(self):
        loads, _w, _c, errors = checked_loads()
        loads = by_id(loads)
        self.assertEqual(loads["352242"].tracking, ACTIVE)
        self.assertAlmostEqual(loads["352242"].hours_since_ping(NOW), 2.8, places=1)
        self.assertEqual(loads["352243"].tracking, STALE)
        self.assertEqual(loads["354745"].tracking, NEVER)
        self.assertEqual(loads["360009"].tracking, UNKNOWN)
        self.assertEqual(errors, ["load 360009: no GPS page for 360009"])
        self.assertTrue(loads["360009"].gps_error)
        self.assertTrue(loads["352242"].gps_checked)

    def test_stale_threshold_and_cap(self):
        loads, _w, _c, _e = checked_loads(now=datetime(2026, 11, 6, 10, 20, tzinfo=EST))
        self.assertEqual(by_id(loads)["352242"].tracking, STALE)  # 4 h 8 min
        loads, _w, _c = select_loads(parse_grid(PAGE), NOW)
        errors = check_gps(loads, fetch_gps, NOW, max_checks=2)
        self.assertTrue(all(l.tracking == UNKNOWN for l in loads[2:]))
        self.assertTrue(all(l.gps_checked for l in loads[:2] if not l.gps_error))
        self.assertEqual(sum(1 for e in errors if "cap of 2" in e), len(loads) - 2)
        errors = check_gps(loads, None, NOW)
        self.assertTrue(all(l.tracking == UNKNOWN for l in loads))

    def test_classify_direct(self):
        load = by_id(select_loads(parse_grid(PAGE), NOW)[0])["352242"]
        self.assertEqual(classify(load, NOW), NEVER)
        load.pings = [Ping(when=NOW - timedelta(hours=3, minutes=59), where="X, IL", who="a", stamp="s")]
        self.assertEqual(classify(load, NOW), ACTIVE)
        load.pings = [Ping(when=NOW - timedelta(hours=4, minutes=1), where="X, IL", who="a", stamp="s")]
        self.assertEqual(classify(load, NOW), STALE)
        self.assertEqual(classify(load, NOW, stale_hours=5), ACTIVE)
        load.gps_error = "boom"
        self.assertEqual(classify(load, NOW), UNKNOWN)


class CommentTests(unittest.TestCase):
    def test_comment_text_matches_the_hand_written_format(self):
        ping = Ping(when=NOW, where="Oak Lawn, IL", who="x", stamp="2026-11-06 06:12:25-05")
        self.assertEqual(comment_text(ping), "2026-11-06 06:12:25-05 Oak Lawn, IL")
        self.assertLessEqual(len(comment_text(Ping(when=NOW, where="x" * 300, who="x", stamp="s"))), 255)

    def test_plan_comments_for_every_load_with_a_ping_not_on_the_load_yet(self):
        loads, _w, _c, _e = checked_loads()
        comments = plan_comments(loads)
        self.assertEqual(comments, STALE_AND_ACTIVE_COMMENTS)
        loads = by_id(loads)
        self.assertFalse(needs_comment(loads["360006"]))  # active, but the last comment already carries the ping
        self.assertTrue(needs_comment(loads["352243"]))  # stale: the last known position is still worth a comment
        self.assertFalse(needs_comment(loads["354745"]))  # never: nothing to write
        self.assertFalse(needs_comment(loads["360009"]))  # unknown
        state = CommentState(None)
        state.mark("352242", "2026-11-06 06:12:25-05", "whatever", NOW)
        self.assertFalse(needs_comment(loads["352242"], state))
        state.mark("352242", "2026-11-06 03:38:33-05", "older", NOW)
        self.assertTrue(needs_comment(loads["352242"], state))

    def test_form_fields_like_a_browser(self):
        form = ('<form><INPUT type="hidden" name="h" value="1"><INPUT name="t" value="text"><INPUT type="checkbox" name="c1" value="Y" CHECKED>'
                '<INPUT type="checkbox" name="c2" value="Y"><INPUT type="submit" name="s" value="Submit"><INPUT type="button" name="b" value="x">'
                '<INPUT type="hidden" value="noname"><SELECT name="sel"><OPTION value="a">a<OPTION value="b" SELECTED>b</SELECT>'
                '<SELECT name="first"><OPTION value="">-<OPTION value="z">z</SELECT><SELECT name="multi" multiple><OPTION value="m">m</SELECT>'
                '<SELECT name="multi2" multiple><OPTION value="m" selected>m<OPTION value="n" selected>n</SELECT>'
                '<TEXTAREA name="ta">hello &amp; bye</TEXTAREA></form>')
        self.assertEqual(form_fields(form), [("h", "1"), ("t", "text"), ("c1", "Y"), ("sel", "b"), ("first", ""), ("multi2", "m"), ("multi2", "n"),
                                             ("ta", "hello & bye")])

    def test_build_comment_post_fills_the_boxes(self):
        data = build_comment_post(PAGE, {"352242": "2026-11-06 06:12:25-05 Oak Lawn, IL", "360001": "ping"})
        fields = dict(data)
        self.assertEqual(fields["Control__DispatchCmt_ID__352242__comment"], "2026-11-06 06:12:25-05 Oak Lawn, IL")
        self.assertEqual(fields["Control__DispatchCmt_ID__360001__comment"], "ping")
        self.assertEqual(fields["Control__DispatchCmt_ID__352243__comment"], "")
        self.assertEqual(fields["Menu__DateMne_PickupETA"], "Before")
        self.assertEqual(fields["Control__saved_id"], "2077")
        self.assertEqual(fields["Menu__CarrierAdd"], "Y")
        self.assertEqual(fields["Control__seteta__352242__month_eta"], "11")
        self.assertEqual(fields["Control__seteta__352242__day_eta"], "")
        self.assertNotIn("Menu__NotChecked", fields)
        self.assertNotIn("Menu__Multi", fields)
        self.assertNotIn("x", fields)
        self.assertEqual([v for n, v in data if n == "Menu__is_submitted"], ["Submit"])
        self.assertEqual(data[-1], ("Menu__is_submitted", "Submit"))
        self.assertEqual([v for n, v in data if n == "Menu__ViewFields"], ["dispatch_rep", "status"])
        ids = [v for n, v in data if n == "Control__DispatchCmt_ID__id"]
        self.assertIn("352242", ids)
        self.assertEqual(len(ids), len(SPECS))

    def test_build_comment_post_refuses_without_form_or_box(self):
        with self.assertRaises(RuntimeError):
            build_comment_post(build(form=False), {"352242": "x"})
        with self.assertRaises(RuntimeError) as ctx:
            build_comment_post(PAGE, {"999999": "x"})
        self.assertIn("999999", str(ctx.exception))

    def test_verify_comments(self):
        page = build(replace={"352242": dict(comment=("2026-11-06 09:01 EST", "RUBICON BOT", "2026-11-06 06:12:25-05\tOak Lawn, IL"))})
        result = verify_comments(parse_grid(page), {"352242": "2026-11-06 06:12:25-05 Oak Lawn, IL", "352243": "nope", "999": "x"})
        self.assertEqual(result, {"352242": True, "352243": False, "999": False})

    def test_comment_state_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "comment_state.json")
            state = CommentState(path)
            state.mark("1", "stamp-1", "text", NOW)
            state.mark("2", "stamp-2", "text", NOW - timedelta(days=90))
            state.prune(NOW)
            state.save()
            again = CommentState(path)
            self.assertEqual(again.written("1"), "stamp-1")
            self.assertIsNone(again.written("2"))


class GroupingTests(unittest.TestCase):
    def alerts(self):
        loads, _w, _c, _e = checked_loads()
        return [l for l in loads if l.tracking in (STALE, NEVER)]

    def test_mail_groups(self):
        groups = {g.label: g for g in mail_groups(self.alerts(), REPS)}
        self.assertEqual(sorted(groups), ["Jack Gibson / Victoria Storm", "Kyle Link / Kyle Link", "Podge Ormsby / Podge Ormsby",
                                          "Stellantis: Joseph Camilleri / no sales rep", "Stellantis: Kaleb Hamman / no sales rep",
                                          "Victoria Storm / no sales rep"])
        kaleb = groups["Stellantis: Kaleb Hamman / no sales rep"]
        self.assertEqual(kaleb.to, ["kaleb.hamman@circledelivers.com", "stellantisob@circledelivers.com"])
        self.assertEqual((kaleb.cc, kaleb.routed, kaleb.stellantis, kaleb.greet_names), ([], False, True, ["Kaleb"]))
        self.assertEqual([l.load_id for l in kaleb.loads], ["352243"])
        joseph = groups["Stellantis: Joseph Camilleri / no sales rep"]
        self.assertEqual(joseph.to, list(DEFAULT_STELLANTIS_TO))
        self.assertTrue(joseph.routed)
        self.assertEqual(joseph.notes, ["dispatch rep Joseph Camilleri has no email in reps.csv"])
        kyle = groups["Kyle Link / Kyle Link"]
        self.assertEqual((kyle.to, kyle.cc, kyle.routed), (["kyle.link@circledelivers.com"], ["justice.cromwell@circledelivers.com"], False))
        self.assertEqual(kyle.greet_names, ["Kyle"])
        victoria = groups["Victoria Storm / no sales rep"]
        self.assertEqual((victoria.to, victoria.cc), (["victoria.storm@circledelivers.com"], []))
        self.assertEqual(victoria.notes, ["no sales rep on load 360002"])
        podge = groups["Podge Ormsby / Podge Ormsby"]
        self.assertEqual((podge.to, podge.routed), (list(DEFAULT_ESCALATE), True))
        self.assertEqual(podge.notes, ["dispatch rep Podge Ormsby has no email in reps.csv", "sales rep Podge Ormsby has no email in reps.csv"])
        jack = groups["Jack Gibson / Victoria Storm"]
        self.assertEqual(jack.to, ["jack.gibson@circledelivers.com", "victoria.storm@circledelivers.com"])
        self.assertEqual(jack.cc, ["kyle.link@circledelivers.com"])
        self.assertEqual(jack.notes, ["no manager set for Victoria Storm in reps.csv"])

    def test_default_manager_and_custom_lists(self):
        groups = {g.label: g for g in mail_groups(self.alerts(), REPS, stellantis_to=["ob@x.com"], escalate=["boss@x.com"],
                                                  default_manager=["mgr@x.com"])}
        self.assertEqual(groups["Jack Gibson / Victoria Storm"].cc, ["mgr@x.com", "kyle.link@circledelivers.com"])
        self.assertEqual(groups["Jack Gibson / Victoria Storm"].notes, [])
        self.assertEqual(groups["Stellantis: Kaleb Hamman / no sales rep"].to, ["kaleb.hamman@circledelivers.com", "ob@x.com"])
        self.assertEqual(groups["Podge Ormsby / Podge Ormsby"].to, ["boss@x.com"])

    def test_groups_with_the_same_recipients_merge(self):
        page = build(replace={"360002": dict(dispatch_rep="KYLE LINK", sales1="")})
        loads, _w, _c = select_loads(parse_grid(page), NOW)
        check_gps(loads, fetch_gps, NOW)
        alerts = [l for l in loads if l.tracking in (STALE, NEVER)]
        kyle = [g for g in mail_groups(alerts, REPS) if "Kyle Link" in g.dispatch_reps and not g.stellantis]
        self.assertEqual(len(kyle), 2)  # Kyle+Kyle (Cc Justice) and Kyle alone (no Cc) have different recipients
        page = build(replace={"360002": dict(dispatch_rep="KYLE LINK", sales1="KYLE LINK")})
        loads, _w, _c = select_loads(parse_grid(page), NOW)
        check_gps(loads, fetch_gps, NOW)
        alerts = [l for l in loads if l.tracking in (STALE, NEVER)]
        kyle = [g for g in mail_groups(alerts, REPS) if "Kyle Link" in g.dispatch_reps and not g.stellantis]
        self.assertEqual(len(kyle), 1)
        self.assertEqual(sorted(l.load_id for l in kyle[0].loads), ["360001", "360002"])

    def test_matches(self):
        groups = {g.label: g for g in mail_groups(self.alerts(), REPS)}
        self.assertTrue(groups["Stellantis: Kaleb Hamman / no sales rep"].matches("stellantis"))
        self.assertTrue(groups["Stellantis: Kaleb Hamman / no sales rep"].matches("kaleb hamman"))
        self.assertFalse(groups["Kyle Link / Kyle Link"].matches("stellantis"))
        self.assertTrue(groups["Kyle Link / Kyle Link"].matches("kyle link"))


class ComposeTests(unittest.TestCase):
    def groups(self):
        loads, _w, _c, _e = checked_loads()
        return {g.label: g for g in mail_groups([l for l in loads if l.tracking in (STALE, NEVER)], REPS)}

    def test_regular_email(self):
        email = compose(self.groups()["Jack Gibson / Victoria Storm"], now=NOW)
        self.assertEqual(email.subject, "GPS tracking alert: 1 load not pinging for more than 4 h - Jack Gibson")
        self.assertTrue(email.text.startswith("Hi Jack and Victoria,"))
        self.assertIn("GPS STOPPED MORE THAN 4 H AGO (1)", email.text)
        self.assertIn("Load 360010  |  Kar Korner, Inc.  |  last GPS ping: Fri 11/06 01:00 Gary, IN (8 h ago)", email.text)
        self.assertIn("Lada Trans Inc  |  driver Bob Lee", email.text)
        self.assertIn("Note: no manager set for Victoria Storm in reps.csv.", email.text)
        self.assertIn("gpshistory.mcgi?load_id=360010", email.html)
        self.assertIn("<b style=\"color:#b00020\">Fri 11/06 01:00</b> Gary, IN", email.html)
        self.assertEqual(email.load_ids, ["360010"])
        self.assertFalse(email.routed)

    def test_stellantis_email(self):
        email = compose(self.groups()["Stellantis: Kaleb Hamman / no sales rep"], now=NOW)
        self.assertEqual(email.subject, "Stellantis GPS tracking alert: 1 load not pinging for more than 4 h - Kaleb Hamman")
        self.assertTrue(email.text.startswith("Hi Kaleb,"))
        self.assertIn("the drivers on these Stellantis loads", email.text)
        self.assertEqual(email.to, ["kaleb.hamman@circledelivers.com", "stellantisob@circledelivers.com"])
        self.assertIn("Load 352243  |  Stellantis (fca)  |  last GPS ping: Thu 11/05 14:00 Rockford, IL (19 h ago)", email.text)
        self.assertIn("Elwood, IL -> Dodge Of Burnsville Inc, Burnsville, MN", email.text)

    def test_routed_email_names_the_reps(self):
        email = compose(self.groups()["Stellantis: Joseph Camilleri / no sales rep"], now=NOW)
        self.assertEqual(email.subject, "Stellantis GPS tracking alert: 1 load with no GPS pings - Joseph Camilleri (no email on file)")
        self.assertTrue(email.text.startswith("Hi all,"))
        self.assertIn("Nobody could be emailed directly (dispatch rep Joseph Camilleri has no email in reps.csv)", email.text)
        self.assertIn("NO GPS PINGS (1)", email.text)
        self.assertIn("none in the last 2 days", email.text)
        self.assertIn("in the last 2 days. Call the driver", email.text)
        self.assertIn("dispatch rep: Joseph Camilleri; sales rep: Justice Cromwell", email.text)
        self.assertIn("Dispatch / sales rep", email.html)
        self.assertTrue(email.routed)

    def test_mixed_subject_and_sections(self):
        loads, _w, _c, _e = checked_loads()
        alerts = [l for l in loads if l.tracking in (STALE, NEVER)]
        for load in alerts:
            load.dispatch_rep, load.sales_rep, load.customer_id, load.customer = "KYLE LINK", "KYLE LINK", "1", "Someone"
        alerts[0].last_comment_at, alerts[0].last_comment_by, alerts[0].last_comment = "2026-11-05 15:14 EST", "JUSTICE CROMWELL", OLD_PING
        group = mail_groups(alerts, REPS)[0]
        email = compose(group, now=NOW, stale_hours=6, gps_days=7, repeat_hours=2)
        self.assertEqual(email.subject, "GPS tracking alert: 6 loads (2 with no GPS pings, 4 not pinging for more than 6 h) - Kyle Link")
        self.assertLess(email.text.index("NO GPS PINGS (2)"), email.text.index("GPS STOPPED MORE THAN 6 H AGO (4)"))
        self.assertIn("in the last 7 days", email.text)
        self.assertIn("repeats every 2 hours", email.text)
        self.assertIn("last dispatch comment (2026-11-05 15:14 EST, Justice Cromwell): " + OLD_PING, email.text)


class StateTests(unittest.TestCase):
    def test_alert_state_repeats_after_the_window_or_on_change(self):
        loads, _w, _c, _e = checked_loads()
        load = by_id(loads)["352243"]
        state = AlertState(None)
        self.assertTrue(state.should_alert(load, NOW, 4))
        state.mark(load, NOW)
        self.assertFalse(state.should_alert(load, NOW + timedelta(hours=3), 4))
        self.assertTrue(state.should_alert(load, NOW + timedelta(hours=4), 4))
        self.assertTrue(state.should_alert(load, NOW, 0))
        load.tracking = NEVER
        self.assertTrue(state.should_alert(load, NOW, 4))
        load.tracking = STALE
        load.dispatch_rep = "KYLE LINK"
        self.assertTrue(state.should_alert(load, NOW, 4))

    def test_alert_state_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "alert_state.json")
            loads, _w, _c, _e = checked_loads()
            state = AlertState(path)
            state.mark(by_id(loads)["352243"], NOW)
            state.mark(by_id(loads)["360001"], NOW - timedelta(days=80))
            state.prune(NOW)
            state.save()
            again = AlertState(path)
            self.assertEqual(sorted(again.sent), ["352243"])
            self.assertFalse(again.should_alert(by_id(loads)["352243"], NOW + timedelta(hours=1), 4))
            self.assertEqual(again.report, {})


class ReportTests(unittest.TestCase):
    def test_report_hours(self):
        self.assertEqual(parse_report_hours("8"), (8,))
        self.assertEqual(parse_report_hours("14, 8"), (8, 14))
        self.assertEqual(parse_report_hours("all"), "all")
        for text in ("", "never", "-", None):
            self.assertEqual(parse_report_hours(text), ())
        for bad in ("25", "noon", "8,x"):
            with self.assertRaises(ValueError):
                parse_report_hours(bad)
        self.assertTrue(report_due(NOW.replace(hour=8), (8,)))
        self.assertFalse(report_due(NOW, (8,)))
        self.assertTrue(report_due(NOW, (8, 9)))
        self.assertTrue(report_due(NOW, "all"))
        self.assertFalse(report_due(NOW, ()))

    def test_report_loads_split_never_and_quiet_by_customer_and_rep(self):
        loads, _w, _c, _e = checked_loads()
        never, quiet = report_loads(loads, NOW, 11)
        self.assertEqual([l.load_id for l in never], ["360002", "354745"])  # Kingbee before Stellantis
        self.assertEqual([l.load_id for l in quiet], ["360003", "352243"])  # 360001 (4.5 h) and 360010 (8 h) pinged within 11 h
        never, quiet = report_loads(loads, NOW, 4)
        self.assertEqual([l.load_id for l in quiet], ["360001", "360003", "360010", "352243"])
        # with the In-Route loads: 360008 never pinged, 360011 pinging
        scanned, _c = scanned_loads()
        never, quiet = report_loads(scanned, NOW, 11)
        self.assertEqual([l.load_id for l in never], ["360008", "360002", "354745"])
        self.assertEqual([l.load_id for l in quiet], ["360003", "352243"])
        self.assertEqual([report_bucket(by_id(scanned)[i], NOW) for i in ("360002", "352243", "352242", "360009", "360011")],
                         [NEVER, "quiet", "ok", UNKNOWN, "ok"])
        breakdown = report_breakdowns(scanned, NOW, 11)
        self.assertEqual(breakdown["statuses"], ["Dispatched", "In-Route"])
        self.assertEqual(breakdown["status"], [
            ("Dispatched", {"scanned": 9, "never": 2, "quiet": 2, "ok": 4, "unknown": 1}),
            ("In-Route", {"scanned": 2, "never": 1, "quiet": 0, "ok": 1, "unknown": 0}),
            ("All", {"scanned": 11, "never": 3, "quiet": 2, "ok": 5, "unknown": 1})])
        self.assertEqual([(label, total["scanned"], total["never"] + total["quiet"], total["ok"]) for label, _per, total in breakdown["customer"]],
                         [("Stellantis (fca)", 3, 2, 1), ("Kar Korner, Inc.", 3, 1, 1), ("Kingbee Rentals, LLC", 2, 1, 1),
                          ("Enterprise Rent-a-car Company Of Utah, LLC", 1, 1, 0), ("Avis Trucks", 2, 0, 2)])
        kar = [row for row in breakdown["customer"] if row[0] == "Kar Korner, Inc."][0]
        self.assertEqual(kar[1]["Dispatched"], {"scanned": 2, "never": 0, "quiet": 0, "ok": 1, "unknown": 1})
        self.assertEqual(kar[1]["In-Route"], {"scanned": 1, "never": 1, "quiet": 0, "ok": 0, "unknown": 0})
        self.assertEqual([(label, total["scanned"], total["never"] + total["quiet"]) for label, _per, total in breakdown["dispatch_rep"]],
                         [("Kyle Link", 3, 1), ("Kaleb Hamman", 2, 1), ("Victoria Storm", 2, 1), ("Joseph Camilleri", 1, 1),
                          ("Podge Ormsby", 1, 1), ("Jack Gibson", 2, 0)])
        # a status with no loads keeps its (empty) column
        only = report_breakdowns([l for l in scanned if l.status == "Dispatched"], NOW, 11)
        self.assertEqual(only["status"][1], ("In-Route", {"scanned": 0, "never": 0, "quiet": 0, "ok": 0, "unknown": 0}))

    def test_compose_report(self):
        scanned, counts = scanned_loads()
        email = compose_report(scanned, now=NOW, to=list(DEFAULT_REPORT_TO), reps=REPS, counts=counts)
        self.assertEqual(email.key, "report")
        self.assertEqual((email.to, email.cc), (["justice.cromwell@circledelivers.com"], []))
        self.assertEqual(email.subject, "GPS tracking report Fri 11/06 09:00: 5 of 11 loads with issues (3 with no GPS pings, 2 quiet for more than 11 h)")
        self.assertEqual(email.load_ids, ["360008", "360002", "354745", "360003", "352243"])
        text = email.text
        self.assertTrue(text.startswith("Hi Justice,\n"))
        self.assertIn("shows 9 Dispatched and 2 In-Route loads with a pickup ETA before 11/08/2026.", text)
        self.assertIn("SCANNED\n", text)
        self.assertIn("  status      no pings  quiet > 11 h  OK  total issues  total scanned  unreadable  tracking rate\n", text)
        self.assertIn("  Dispatched         2             2   4             4              9           1         44.44%\n", text)
        self.assertIn("  In-Route           1             0   1             1              2           0         50.00%\n", text)
        self.assertIn("  All                3             2   5             5             11           1         45.45%\n", text)
        self.assertIn("BY CUSTOMER\n", text)
        self.assertIn("  customer                                    no pings  quiet > 11 h  OK  total issues  total scanned  unreadable  tracking rate\n", text)
        self.assertIn("  Stellantis (fca)                                   1             1   1             2              3           0         33.33%\n", text)
        self.assertIn("  Kar Korner, Inc.                                   1             0   1             1              3           1         33.33%\n", text)
        self.assertIn("  Avis Trucks                                        0             0   2             0              2           0        100.00%\n", text)
        self.assertIn("  Total                                              3             2   5             5             11           1         45.45%\n", text)
        self.assertIn("BY DISPATCH REP\n", text)
        self.assertIn("  Kyle Link                1             0   2             1              3           0         66.67%\n", text)
        self.assertIn("The tracking rate is the share of the scanned loads without an issue.", text)
        self.assertIn("NO GPS PINGS (3)", text)
        self.assertIn("QUIET FOR MORE THAN 11 H (2)", text)
        self.assertIn("Kar Korner, Inc. (1)\n  Kyle Link (1)\n    Load 360008  |  In-Route  |  Ocilla, GA -> Medina, OH  |  pickup ETA Fri 11/06 10:00", text)
        self.assertIn("Stellantis (fca) (1)\n  Kaleb Hamman (1)\n    Load 352243  |  Dispatched  |  Elwood, IL -> Burnsville, MN  |  pickup ETA Fri 11/06 20:00  |  "
                      "100 Miles Transport LLC, driver Erdenetuvshin Oyunbaatar (224) 284-9908  |  last ping Thu 11/05 14:00 Rockford, IL (19 h ago)"
                      "  |  sales rep Justice Cromwell", text)
        self.assertIn("GPS PAGE UNREADABLE (1): 360009", text)
        self.assertIn("the In-Route loads are scanned for this report only and get no comments or alerts", text)
        html = email.html
        self.assertIn("load_id=354745", html)
        self.assertIn("load_id=360008", html)
        for header in ("Customer", "Dispatch rep", "No Pings", "Quiet &gt; 11 h", "OK", "Total Issues", "Total Scanned", "Unreadable", "Tracking Rate"):
            self.assertIn(f'border-bottom:2px solid #999">{header}</th>', html)
        self.assertIn('<b style="color:#1a7f37">33.33%</b>', html)
        self.assertIn('<b style="color:#b00020">2</b>', html)  # an issue count in red
        self.assertIn("<b>Total</b>", html)
        self.assertIn("<b style=\"color:#b00020\">Thu 11/05 14:00</b> Rockford, IL", html)
        self.assertIn("<td style=\"padding:4px 8px;border-bottom:1px solid #ddd;vertical-align:top\">In-Route</td>", html)
        # Merchants left out, mentioned in the intro; a recipient without a name
        merchants = dict(customer="Merchants Automotive Group Holdings LLC", cons_id="81464")
        loads2, _w, counts2, _e = checked_loads(build(replace={"352242": merchants}))
        email = compose_report(loads2, now=NOW, to=["ops@circledelivers.com"], reps=REPS, counts=counts2)
        self.assertIn("shows 8 Dispatched and 0 In-Route loads with a pickup ETA before 11/08/2026; 1 Merchants Automotive load left out.", email.text)
        self.assertTrue(email.text.startswith("Hi all,"))
        # nothing to report: no unreadable column either
        email = compose_report([l for l in scanned if l.tracking == ACTIVE], now=NOW, to=["kyle.link@circledelivers.com"], reps=REPS)
        self.assertEqual(email.subject, "GPS tracking report Fri 11/06 09:00: all 3 loads scanned are pinging")
        self.assertTrue(email.text.startswith("Hi Kyle,"))
        self.assertIn("NO GPS PINGS (0)", email.text)
        self.assertNotIn("unreadable", email.text)
        self.assertIn("100.00%", email.text)
        self.assertEqual(email.load_ids, [])

class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.outbox = os.path.join(self.tmp, "outbox")
        self.state = AlertState(os.path.join(self.tmp, "alert_state.json"))
        self.comment_state = CommentState(os.path.join(self.tmp, "comment_state.json"))
        self.sent_log = os.path.join(self.tmp, "sent_log.jsonl")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_once(self, *, record=False, post_comments=False, transport=None, poster=None, page=PAGE, now=NOW, **kw):
        transport = transport or FakeTransport()
        poster = FakePoster() if poster is None else poster
        summary = run_once(page, parse_grid(page), now=now, reps=REPS, gps_fetch=fetch_gps, state=self.state, comment_state=self.comment_state,
                           transport=transport, poster=poster, from_addr="Circle <vehicles@circledelivers.com>", record=record,
                           post_comments=post_comments, sent_log=self.sent_log if record else None, outbox=self.outbox, **kw)
        return summary, transport, poster

    def test_dry_run_writes_everything_to_the_outbox_and_touches_no_state(self):
        summary, transport, poster = self.run_once()
        self.assertEqual(summary["selected"], 9)
        self.assertEqual(summary["by_state"], {ACTIVE: 2, STALE: 4, NEVER: 2, UNKNOWN: 1})
        self.assertEqual(summary["comments_planned"], 5)
        self.assertEqual(summary["comments_written"], 0)
        self.assertEqual(poster.calls, [])
        dumps = [f for f in os.listdir(self.outbox) if f.endswith("_comments.txt")]
        self.assertEqual(len(dumps), 1)
        with open(os.path.join(self.outbox, dumps[0]), encoding="utf-8") as handle:
            dump = handle.read()
        self.assertIn("352242 | 2026-11-06 06:12:25-05 Oak Lawn, IL | Tsolmon Tsogtsaikhan | 100 Miles Transport LLC", dump)
        self.assertIn("352243 | 2026-11-05 14:00:00-05 Rockford, IL | Erdenetuvshin Oyunbaatar | 100 Miles Transport LLC", dump)
        self.assertEqual(len(transport.sent), 6)
        self.assertEqual(sorted(e["group"] for e in summary["emails"]),
                         ["Jack Gibson|Victoria Storm", "Kyle Link|Kyle Link", "Podge Ormsby|Podge Ormsby", "Victoria Storm|nobody",
                          "stellantis|Joseph Camilleri|nobody", "stellantis|Kaleb Hamman|nobody"])
        self.assertTrue(all(not e["sent"] for e in summary["emails"]))
        self.assertEqual(self.state.sent, {})
        self.assertEqual(self.comment_state.entries, {})
        self.assertFalse(os.path.exists(self.sent_log))
        self.assertEqual(summary["gps_errors"], ["load 360009: no GPS page for 360009"])
        self.assertNotIn("360009", [lid for e in summary["emails"] for lid in e["loads"]])

    def test_live_run_posts_comments_sends_and_remembers(self):
        summary, transport, poster = self.run_once(record=True, post_comments=True)
        self.assertEqual(poster.calls, [STALE_AND_ACTIVE_COMMENTS])
        self.assertEqual((summary["comments_written"], summary["comments_failed"]), (5, 0))
        self.assertEqual(self.comment_state.written("352242"), "2026-11-06 06:12:25-05")
        self.assertEqual(self.comment_state.written("352243"), "2026-11-05 14:00:00-05")  # the quiet driver's last position
        self.assertTrue(os.path.exists(self.comment_state.path))
        self.assertEqual(len(transport.sent), 6)
        self.assertEqual(sorted(self.state.sent), ["352243", "354745", "360001", "360002", "360003", "360010"])
        self.assertIn("352243", [l for e, _f in transport.sent for l in e.load_ids])  # commented AND in the alert
        with open(self.sent_log, encoding="utf-8") as handle:
            entries = [json.loads(line) for line in handle]
        self.assertEqual(len(entries), 6)
        self.assertTrue(all(e["sent"] for e in entries))
        # the next run an hour later: every ping is already on its load, every alert is inside the repeat window
        summary2, transport2, poster2 = self.run_once(record=True, post_comments=True, now=NOW + timedelta(hours=1))
        self.assertEqual(poster2.calls, [])
        self.assertEqual([e.key for e, _f in transport2.sent], ["report"])  # 10:00 is a report hour; no alerts
        self.assertEqual(summary2["skipped_repeat"], 6)
        # five hours later the alerts repeat, the comments do not (same last pings)
        summary3, transport3, poster3 = self.run_once(record=True, post_comments=True, now=NOW + timedelta(hours=5))
        self.assertEqual(len(transport3.sent), 6)
        self.assertEqual(poster3.calls, [])

    def test_emails_and_report_go_before_the_comments_with_a_checkpoint_between(self):
        order = []

        class Poster(FakePoster):
            def post(self, page_html, comments):
                order.append("comments")
                return super().post(page_html, comments)

        class Transport(FakeTransport):
            def send(self, email, from_addr):
                order.append("report" if email.key == "report" else "email")
                return super().send(email, from_addr)

        summary, transport, poster = self.run_once(record=True, post_comments=True, now=NOW.replace(hour=16), transport=Transport(),
                                                   poster=Poster(), checkpoint=lambda: order.append("checkpoint"))
        self.assertEqual(order, ["email"] * 6 + ["report", "checkpoint", "comments"])
        self.assertEqual((summary["comments_written"], len(summary["emails"])), (5, 6))
        self.assertEqual(sorted(poster.calls[0]), sorted(STALE_AND_ACTIVE_COMMENTS))  # no comment on the pinging In-Route load 360011
        self.assertEqual(self.state.report["slot"], "2026-11-06 16")
        # a checkpoint that fails does not stop the comments
        self.comment_state.entries.clear()
        summary, _t, poster = self.run_once(record=True, post_comments=True, now=NOW + timedelta(days=1),
                                            checkpoint=lambda: (_ for _ in ()).throw(RuntimeError("s3 down")))
        self.assertEqual(len(poster.calls), 1)

    def test_comment_failures_are_counted_and_do_not_stop_the_emails(self):
        summary, transport, _p = self.run_once(record=True, post_comments=True, poster=FakePoster(fail=["352242"]))
        self.assertEqual((summary["comments_written"], summary["comments_failed"]), (4, 1))
        self.assertIsNone(self.comment_state.written("352242"))
        self.assertEqual(self.comment_state.written("360001"), "2026-11-06 04:30:00-05")
        self.assertEqual(len(transport.sent), 6)
        self.comment_state.entries.clear()
        summary, transport, _p = self.run_once(record=True, post_comments=True, poster=FakePoster(explode=True))
        self.assertIn("rejected", summary["comment_error"])
        self.assertEqual(summary["comments_failed"], 5)

    def test_redirect_and_only_rep(self):
        summary, transport, _p = self.run_once(record=True, redirect_to=["me@circledelivers.com"])
        self.assertEqual(len(transport.sent), 6)
        self.assertTrue(all(e.to == ["me@circledelivers.com"] and e.subject.startswith("[Test copy] ") for e, _f in transport.sent))
        self.assertEqual(self.state.sent, {})
        summary, transport, _p = self.run_once(only_rep="stellantis")
        self.assertEqual(len(transport.sent), 2)
        summary, transport, _p = self.run_once(only_rep="Kyle Link")
        self.assertEqual([e.subject for e, _f in transport.sent], ["GPS tracking alert: 1 load not pinging for more than 4 h - Kyle Link"])

    def test_only_loads_limits_the_whole_run(self):
        summary, transport, poster = self.run_once(record=True, post_comments=True, only_loads={"352242", "360001", "999999"})
        self.assertEqual(summary["selected"], 2)
        self.assertEqual(poster.calls, [{"352242": "2026-11-06 06:12:25-05 Oak Lawn, IL", "360001": "2026-11-06 04:30:00-05 Columbia, MO"}])
        self.assertEqual([e.subject for e, _f in transport.sent], ["GPS tracking alert: 1 load not pinging for more than 4 h - Kyle Link"])
        self.assertEqual(sorted(self.state.sent), ["360001"])

    def test_excluded_customer_gets_no_comment_and_no_email(self):
        merchants = dict(customer="Merchants Automotive Group Holdings LLC", cons_id="81464")
        page = build(replace={"352242": merchants, "360010": merchants})
        summary, transport, poster = self.run_once(record=True, post_comments=True, page=page)
        self.assertEqual(summary["counts"]["excluded"], 2)
        self.assertEqual(sorted(poster.calls[0]), ["352243", "360001", "360003"])  # neither Merchants load is commented
        self.assertNotIn("Jack Gibson|Victoria Storm", [e["group"] for e in summary["emails"]])
        self.assertEqual(len(transport.sent), 5)
        # the same run with the exclusion off: the Merchants loads come back (the other five groups are inside the repeat window)
        summary, transport, poster = self.run_once(record=True, post_comments=True, page=page, exclude_customers=())
        self.assertEqual(summary["counts"]["excluded"], 0)
        self.assertEqual(poster.calls, [{"352242": "2026-11-06 06:12:25-05 Oak Lawn, IL", "360010": "2026-11-06 01:00:00-05 Gary, IN"}])
        self.assertEqual([e["group"] for e in summary["emails"]], ["Jack Gibson|Victoria Storm"])
        self.assertEqual(summary["skipped_repeat"], 5)

    def test_send_failure_is_logged_not_fatal(self):
        summary, transport, _p = self.run_once(record=True, transport=FakeTransport(fail_for="Kyle Link"))
        self.assertEqual(len(transport.sent), 5)
        self.assertEqual(summary["send_errors"], ["Kyle Link / Kyle Link: SES down"])
        self.assertNotIn("360001", self.state.sent)

    def test_report_goes_out_at_its_hour_once(self):
        ten = NOW.replace(hour=10)
        summary, transport, _p = self.run_once(record=True)
        self.assertIsNone(summary["report"])  # 09:00 is not a report hour
        self.assertEqual([e for e, _f in transport.sent if e.key == "report"], [])
        self.assertEqual(summary["report_loads"], [])  # the In-Route loads were not even looked at
        summary, transport, _p = self.run_once(record=True, now=ten)
        reports = [e for e, _f in transport.sent if e.key == "report"]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0].to, ["justice.cromwell@circledelivers.com"])
        self.assertEqual((summary["report"]["never"], summary["report"]["quiet"]), (["360008", "360002", "354745"], ["360003", "352243"]))
        self.assertEqual(summary["report"]["scanned"]["In-Route"], {"scanned": 2, "never": 1, "quiet": 0, "ok": 1, "unknown": 0})
        self.assertEqual([l["load_id"] for l in summary["report_loads"]], ["360008", "360011"])
        self.assertEqual(summary["selected"], 9)  # the In-Route loads are not alert loads
        self.assertNotIn("360008", [lid for e in summary["emails"] for lid in e["loads"]])
        self.assertNotIn("360008", self.state.sent)
        self.assertTrue(summary["report"]["sent"])
        self.assertEqual(self.state.report["slot"], "2026-11-06 10")
        self.assertEqual(AlertState(self.state.path).report["slot"], "2026-11-06 10")
        with open(self.sent_log, encoding="utf-8") as handle:
            entries = [json.loads(line) for line in handle]
        self.assertEqual([e["subject"] for e in entries if e.get("report")],
                         ["GPS tracking report Fri 11/06 10:00: 5 of 11 loads with issues (3 with no GPS pings, 2 quiet for more than 11 h)"])
        # the same hour again (a manual run): not twice
        summary, transport, _p = self.run_once(record=True, now=ten + timedelta(minutes=20))
        self.assertIsNone(summary["report"])
        self.assertEqual([e for e, _f in transport.sent if e.key == "report"], [])
        # forced: any hour; and a test copy is never recorded
        summary, transport, _p = self.run_once(record=True, force_report=True, redirect_to=["me@circledelivers.com"])
        reports = [e for e, _f in transport.sent if e.key == "report"]
        self.assertEqual(reports[0].to, ["me@circledelivers.com"])
        self.assertTrue(reports[0].subject.startswith("[Test copy] GPS tracking report"))
        self.assertFalse(summary["report"]["sent"])
        self.assertEqual(self.state.report["slot"], "2026-11-06 10")
        # every run; never; nobody; an --only-rep test run
        self.assertIsNotNone(self.run_once(report_hours="all")[0]["report"])
        self.assertIsNone(self.run_once(now=ten, report_hours=())[0]["report"])
        self.assertIsNone(self.run_once(now=ten, report_to=())[0]["report"])
        self.assertIsNone(self.run_once(now=ten, only_rep="stellantis")[0]["report"])
        # the next day's slot sends again; a send failure is logged, not fatal
        summary, transport, _p = self.run_once(record=True, now=ten + timedelta(days=1))
        self.assertTrue(summary["report"]["sent"])
        self.assertEqual(self.state.report["slot"], "2026-11-07 10")

        class Down(FakeTransport):
            def send(self, email, from_addr):
                if email.key == "report":
                    raise RuntimeError("SES down")
                return super().send(email, from_addr)

        summary, transport, _p = self.run_once(record=True, now=ten + timedelta(days=2), transport=Down())
        self.assertIsNone(summary["report"])
        self.assertEqual(summary["send_errors"], ["report: SES down"])
        self.assertEqual(self.state.report["slot"], "2026-11-07 10")


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.page = os.path.join(self.tmp, "grid.html")
        with open(self.page, "w", encoding="utf-8") as handle:
            handle.write(PAGE)
        self.gps = os.path.join(self.tmp, "gps")
        os.makedirs(self.gps)
        for load_id, html in GPS.items():
            with open(os.path.join(self.gps, f"gps_{load_id}.html"), "w", encoding="utf-8") as handle:
                handle.write(html)
        self.reps = os.path.join(self.tmp, "reps.csv")
        with open(self.reps, "w", encoding="utf-8") as handle:
            handle.write("name,email,title,phone,manager\n# comment\nLINK, KYLE,kyle.link@circledelivers.com,Sr. Account Manager,(260) 319-8153,justice.cromwell@circledelivers.com\n"
                         "HAMMAN, KALEB,kaleb.hamman@circledelivers.com,,,justice.cromwell@circledelivers.com\n"
                         "STORM, VICTORIA,victoria.storm@circledelivers.com,Senior Account Manager\n")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def common(self):
        return ["--from-html", self.page, "--gps-dir", self.gps, "--reps", self.reps, "--now", NOW.isoformat(),
                "--outbox", os.path.join(self.tmp, "outbox"), "--state", os.path.join(self.tmp, "alert_state.json"),
                "--comment-state", os.path.join(self.tmp, "comment_state.json"), "--sent-log", os.path.join(self.tmp, "sent_log.jsonl")]

    def test_reps_csv_manager_column(self):
        reps = load_rep_details(self.reps)
        self.assertEqual(reps["kyle link"], {"email": "kyle.link@circledelivers.com", "title": "Sr. Account Manager", "phone": "(260) 319-8153",
                                             "manager": "justice.cromwell@circledelivers.com"})
        self.assertEqual(reps["kaleb hamman"]["manager"], "justice.cromwell@circledelivers.com")
        self.assertEqual(reps["kaleb hamman"]["title"], "")
        self.assertEqual(reps["victoria storm"]["manager"], "")

    def test_list(self):
        out = os.path.join(self.tmp, "list.json")
        self.assertEqual(main(self.common() + ["--list", "--json", out]), 0)
        with open(out, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["by_state"], {ACTIVE: 2, STALE: 4, NEVER: 2, UNKNOWN: 1})
        self.assertEqual(data["comments"], STALE_AND_ACTIVE_COMMENTS)
        self.assertEqual(len(data["loads"]), 9)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "outbox")))

    def test_dry_run(self):
        out = os.path.join(self.tmp, "run.json")
        self.assertEqual(main(self.common() + ["--json", out]), 0)
        with open(out, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(len(data["emails"]), 6)
        files = os.listdir(os.path.join(self.tmp, "outbox"))
        self.assertEqual(len([f for f in files if f.endswith("_comments.txt")]), 1)
        self.assertEqual(len([f for f in files if not f.endswith("_comments.txt")]), 6)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "alert_state.json")))
        kaleb = [e for e in data["emails"] if e["group"] == "stellantis|Kaleb Hamman|nobody"][0]
        self.assertEqual(kaleb["to"], ["kaleb.hamman@circledelivers.com", "stellantisob@circledelivers.com"])
        self.assertEqual(main(self.common() + ["--only-load", "352243,360010", "--json", out]), 0)
        with open(out, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["selected"], 2)
        self.assertEqual(sorted(e["group"] for e in data["emails"]), ["Jack Gibson|Victoria Storm", "stellantis|Kaleb Hamman|nobody"])
        # the exclusion from the command line: Kar Korner out, so Jack's group is gone; "" means nobody excluded
        self.assertEqual(main(self.common() + ["--exclude-customer", "kar korner", "--json", out]), 0)
        with open(out, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["counts"]["excluded"], 2)
        self.assertNotIn("Jack Gibson|Victoria Storm", [e["group"] for e in data["emails"]])
        self.assertEqual(main(self.common() + ["--exclude-customer", "", "--json", out]), 0)
        with open(out, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["counts"]["excluded"], 0)

    def test_lambda_args(self):
        args = tracking_update_lambda.alert_args("/w", {"TRACK_SEND": "1", "TRACK_POST_COMMENTS": "1", "TRACK_STALE_HOURS": "6",
                                                        "TRACK_STELLANTIS_TO": "ob@x.com", "TRACK_SAVE_PAGES": "1"})
        self.assertIn("--send", args)
        self.assertIn("--post-comments", args)
        self.assertEqual(args[args.index("--stale-hours") + 1], "6")
        self.assertEqual(args[args.index("--stellantis-to") + 1], "ob@x.com")
        self.assertIn("--save-html", args)
        args = tracking_update_lambda.alert_args("/w", {})
        self.assertNotIn("--send", args)
        self.assertNotIn("--post-comments", args)
        self.assertNotIn("--exclude-customer", args)  # the code default (Merchants) applies
        self.assertEqual(args[args.index("--repeat-hours") + 1], "4")
        args = tracking_update_lambda.alert_args("/w", {"TRACK_EXCLUDE_CUSTOMERS": "-"})
        self.assertEqual(args[args.index("--exclude-customer") + 1], "")
        args = tracking_update_lambda.alert_args("/w", {"TRACK_EXCLUDE_CUSTOMERS": "81464,Kingbee"})
        self.assertEqual(args[args.index("--exclude-customer") + 1], "81464,Kingbee")
        # the report: env for the schedule and the recipients, the event to force one now
        self.assertNotIn("--report-hours", args)  # the code default (10:00 and 16:00 ET, Justice) applies
        self.assertNotIn("--report", args)
        args = tracking_update_lambda.alert_args("/w", {"TRACK_REPORT_HOURS": "8,14", "TRACK_REPORT_TO": "a@x.com",
                                                        "TRACK_REPORT_QUIET_HOURS": "12"}, {"report": True})
        self.assertEqual(args[args.index("--report-hours") + 1], "8,14")
        self.assertEqual(args[args.index("--report-to") + 1], "a@x.com")
        self.assertEqual(args[args.index("--report-quiet-hours") + 1], "12")
        self.assertIn("--report", args)
        args = tracking_update_lambda.alert_args("/w", {"TRACK_REPORT_HOURS": "-", "TRACK_REPORT_TO": "-", "TRACK_REPORT_STATUS": "-"}, {"report": "0"})
        self.assertEqual(args[args.index("--report-hours") + 1], "")
        self.assertEqual(args[args.index("--report-to") + 1], "")
        self.assertEqual(args[args.index("--report-status") + 1], "")
        args = tracking_update_lambda.alert_args("/w", {"TRACK_REPORT_STATUS": "In-Route,Delivered"})
        self.assertEqual(args[args.index("--report-status") + 1], "In-Route,Delivered")
        self.assertNotIn("--report", args)

    def test_report_from_the_command_line(self):
        out = os.path.join(self.tmp, "run.json")
        self.assertEqual(main(self.common() + ["--report", "--json", out]), 0)
        with open(out, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["report"]["to"], ["justice.cromwell@circledelivers.com"])
        self.assertEqual(data["report"]["never"], ["360008", "360002", "354745"])
        self.assertEqual(data["report"]["scanned"]["All"]["scanned"], 11)
        self.assertFalse(data["report"]["sent"])
        self.assertEqual(main(self.common() + ["--report", "--report-status", "", "--json", out]), 0)
        with open(out, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["report"]["never"], ["360002", "354745"])
        self.assertEqual(list(data["report"]["scanned"]), ["Dispatched", "All"])
        files = os.listdir(os.path.join(self.tmp, "outbox"))
        self.assertEqual(len([f for f in files if f.endswith("_report.txt")]), 1)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "alert_state.json")))
        # at 10:00 it goes by itself; "never" and an empty --report-to stop it; --report-to changes the recipient
        ten = ["--now", NOW.replace(hour=10).isoformat()]
        self.assertEqual(main(self.common() + ten + ["--json", out]), 0)
        with open(out, encoding="utf-8") as handle:
            self.assertIsNotNone(json.load(handle)["report"])
        self.assertEqual(main(self.common() + ten + ["--report-hours", "never", "--json", out]), 0)
        with open(out, encoding="utf-8") as handle:
            self.assertIsNone(json.load(handle)["report"])
        self.assertEqual(main(self.common() + ["--report", "--report-to", "", "--json", out]), 0)
        with open(out, encoding="utf-8") as handle:
            self.assertIsNone(json.load(handle)["report"])
        self.assertEqual(main(self.common() + ["--report", "--report-to", "a@x.com,b@x.com", "--report-quiet-hours", "4", "--json", out]), 0)
        with open(out, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["report"]["to"], ["a@x.com", "b@x.com"])
        self.assertEqual(data["report"]["quiet"], ["360001", "360003", "360010", "352243"])
        with self.assertRaises(SystemExit):
            main(self.common() + ["--report-hours", "noon"])


if __name__ == "__main__":
    unittest.main()
