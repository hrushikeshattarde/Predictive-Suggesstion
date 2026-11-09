"""Unit tests for sales_rep_sync.py (``python -m unittest`` from the sales_rep_sync folder).
No network: the Customer List and the Sales Rep Edit screen are synthetic grids shaped
like the live pages of 2026-11-09 (the screen inside its Menu_alt form, with the per-row
bulk checkboxes, the state tokens, the ETA widgets and the rep dropdown at the foot), and
CargoTel is a fake that answers a bulk POST the way the real screen does."""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.dirname(HERE)
ROOT = os.path.dirname(FOLDER)
for path in (ROOT, FOLDER):
    if path not in sys.path:
        sys.path.insert(0, path)

from cargotel_search import parse_grid  # noqa: E402
from sales_rep_sync import (  # noqa: E402
    BULK_SELECT,
    DRY_RUN,
    FAILED,
    SET,
    SKIP,
    UNASSIGN,
    VERIFIED,
    apply_changes,
    build_bulk_post,
    form_fields,
    groups,
    is_empty_screen,
    load_from_row,
    main,
    menu_alt_form,
    norm_name,
    parse_boxes,
    parse_customers,
    parse_rep_options,
    parse_screen,
    plan,
    run_once,
    screen_problem,
    select,
    summarize,
    verify,
)

NOW = datetime(2026, 11, 9, 14, 30)

# --------------------------------------------------------------------------- #
# Synthetic pages                                                              #
# --------------------------------------------------------------------------- #


def header(columns):
    return ('<tr static="" id="det_header_row" class="unshadow dv_row"><th class="dv_cell dv_string dv_header oeheader" id="row_number_head">#</th>'
            + "".join(f'<th class="dv_cell dv_cell_{key} dv_header dv_header_{key} oeheader" id="det_header_{key}"><span class="dv_header_name">{label}</span></th>'
                      for key, label in columns) + "</tr>")


def cells(n, columns, values):
    tds = [f'<td class="dv_cell listable_cell dv_plain dv_cell_{key}" id="DetCell_{n}_{col}" valign="top">{values.get(key, "")}</td>'
           for col, (key, _label) in enumerate(columns)]
    return f'<tr class="unshadow dv_row listable_row" id="det_row_{n}"><td class="dv_linenumber">{n}</td>' + "".join(tds) + "</tr>"


def menu_form(title):
    return (f'<FORM name="Menu" method="POST" action="?" id="ListablePanel" autocomplete="off">'
            f'<INPUT type="hidden" name="Control__keepTitle" value="{title}"></FORM>')


SIGNIN_PAGE = ('<html><head><title>Circle | Sign in</title></head><body><form method="POST">'
               '<input name="credential_0"><input type="password" name="credential_1"></form></body></html>')

# ---- the Customer List -------------------------------------------------------

CUSTOMER_COLUMNS = [
    ("Id", "Client Id"), ("Client", "Client"), ("Key", "Key"), ("CreditTerms", "Credit Terms"), ("Current", "Current Orders"),
    ("Phone", "Phone"), ("Addr1", "Address 1"), ("City", "City"), ("State", "State"), ("Zip", "Zip"), ("Contact", "Contact Mgr"),
    ("defaultPrimaryrep", "Default Primary Rep"),
]

CUSTOMERS = [
    ("62943", "STELLANTIS (FCA)", "FCA", "CROMWELL, JUSTICE"),
    ("71573", "CIRCLE LOGISTICS", "", "CROMWELL, JUSTICE"),
    ("90888", "KAR KORNER, INC.", "", "LINK, KYLE"),
    ("82722", "KINGBEE RENTALS, LLC", "", "STORM, VICTORIA"),
    ("74623", "UNITED STATES POSTAL SERVICE", "", ""),          # no default rep
    ("91418", "FLEET LEASE DISPOSAL, INC.", "", "EDMONDSON, SETH"),
    ("70001", "AVIS TRUCKS", "", "GIBSON, JACK"),                # rep not in the dropdown
]


def customer_page(customers=CUSTOMERS, *, total=None, columns=CUSTOMER_COLUMNS):
    rows = []
    for n, (cid, name, key, rep) in enumerate(customers, start=1):
        values = {"Id": cid, "Client": f'<A target="target" href="/backoffice/client.mcgi?id={cid}"><nobr>{name}</nobr></A>',
                  "Key": key, "CreditTerms": "Net 30", "Current": '<a href="#">Current Orders</a>', "Phone": "", "Addr1": "1 Main St",
                  "City": "Auburn Hills", "State": "MI", "Zip": "48326", "Contact": "", "defaultPrimaryrep": rep}
        rows.append(cells(n, columns, values))
    total = len(customers) if total is None else total
    return ('<html><head><title>Circle</title></head><body>' + menu_form("Customer List")
            + '<FORM name="Menu_alt" method="POST" action="?" id="Menu_alt"><INPUT type="hidden" name="FORM::DEF" value="Menu">'
            + f'<div id="paginate">Rows 1 - {len(customers)} of {total}</div>'
            + "<table>" + header(columns) + "".join(rows) + "</table></FORM></body></html>")


# ---- the Sales Rep Edit screen -----------------------------------------------

SCREEN_COLUMNS = [
    ("bill_id", "Bill To ID#"), ("billtoname", "Bill To"), ("cons_id", "Customer ID#"), ("consignor", "Customer"),
    ("original_consignor", "Original Consignor"), ("bundleparent", "Load ID"), ("load_id", "Order ID"), ("carriername", "Carrier Name"),
    ("originCityState", "Origin City, State"), ("destination", "Destination"), ("pickiup_eta_datetime", "Pickup ETA"),
    ("driver_name", "Driver Name"), ("driver_cell_phone", "Drivers Cell Phone"), ("GPSLink", "GPS City/St Link"),
    ("currentlocation", "Current Location"), ("current_location_eta", "Current Location / ETA (Edit)"),
    ("sales_rep_bulk", "Primary Sales Rep (Bulk)"), ("sales_rep1", "Sales Rep 1"),
]

DROPDOWN = ('<div id="DetailErrata"><TABLE class="listable_table"><TR><TH colspan="1">Primary Sales Rep (Bulk)</TH></TR><tr><td>Primary Sales Rep:'
            '<SELECT name="Control__sales_rep_bulk__sales_rep" id="Control__sales_rep_bulk__sales_rep" >'
            '<OPTION value="37899">AUBREY LAFAVE<OPTION value="26640">JUSTICE CROMWELL<OPTION value="31033">KYLE LINK'
            '<OPTION value="26638">MITCHELL MEYERS<OPTION value="36153">SETH EDMONDSON<OPTION value="[NONE]">Unassign'
            '<OPTION value="36426">VICTORIA STORM</SELECT> <INPUT type="button" value="Toggle" onclick="x()"> '
            '<INPUT type="button" value="Select All" onclick="y()"></td></tr></TABLE></div>')

REP_IDS = {"JUSTICE CROMWELL": "26640", "KYLE LINK": "31033", "VICTORIA STORM": "36426", "SETH EDMONDSON": "36153",
           "MITCHELL MEYERS": "26638", "AUBREY LAFAVE": "37899"}


def bulk_cell(load_id, seed):
    return (f'<INPUT type="checkbox" id="sales_rep_bulk//{load_id}" value="{load_id}" name="sales_rep_bulk//{load_id}" > '
            f'<input type="hidden" value="{load_id}" name="sales_rep_bulk_shown"> <!-- printState start-->'
            f'<INPUT name="STATESEED" value="{seed}" type="hidden"> <INPUT name="STATEVER::{seed}" value="tok{load_id}" type="hidden"> '
            f'<INPUT name="STATE::SCALAR::{seed}::sales_rep_bulk/{load_id}" value="1" type="hidden"> <!-- printState end-->')


def eta_widget(load_id):
    return (f'Location: <INPUT size="20" id="Control__seteta__{load_id}__loc" value="" name="Control__seteta__{load_id}__loc"><br>ETA: '
            f'<SELECT name="Control__seteta__{load_id}__month_eta"><OPTION value=""><OPTION value="10">Oct<OPTION value="11" SELECTED>Nov</SELECT>/'
            f'<SELECT name="Control__seteta__{load_id}__day_eta"><OPTION value=""><OPTION value="9" SELECTED>9</SELECT>'
            f'<INPUT type="hidden" name="eta_date_edit" value="{load_id}"><INPUT type="hidden" name="Control__rows" value="1">')


def row(n, load_id, *, bill_id, customer, cons_id=None, billto="N/A", consignor="N/A", sales1="", carrier="Ag Logistics LLC",
        origin="Zelienople, PA", dest="United Rentals - Albans | Altman, WV", eta="10/15/2026 17:36", box=True, columns=SCREEN_COLUMNS):
    values = {
        "bill_id": bill_id, "billtoname": billto, "cons_id": bill_id if cons_id is None else cons_id, "consignor": consignor,
        "original_consignor": customer, "bundleparent": "",
        "load_id": (f'<A name="MapId_{load_id}" class="LinkIndividual" style="color: blue" href="/backoffice/loadmaint.mcgi?load_id={load_id}" '
                    f'target="_blank" >{load_id}</A><input type=\'hidden\' name=\'load_id\' value="{load_id}">'),
        "carriername": carrier, "originCityState": origin, "destination": dest, "pickiup_eta_datetime": eta,
        "driver_name": "STANEVICIUS, ARTURAS", "driver_cell_phone": "(847) 809-3976",
        "GPSLink": f'<a href="/backoffice/gpshistory.mcgi?load_id={load_id}" target="_GPS">GPS City&amp;St</a>',
        "currentlocation": "Lubbock, TX @ 2026-11-04 12:42:51-05", "current_location_eta": eta_widget(load_id),
        "sales_rep_bulk": bulk_cell(load_id, f"0.{n}0{n}") if box else "", "sales_rep1": sales1,
    }
    return cells(n, columns, values)


def section(text):
    return f'<tr class="unshadow dv_row dv_section_header dv_sum_row" static=""><th align="left" colspan="19">Customer: {text}</th></tr>'


SPECS = {
    # Circle Logistics, listed with Justice as default: set.
    "345949": dict(n=1, load_id="345949", bill_id="71573", customer="Circle Logistics"),
    # The other Circle Logistics account is not on the Customer List: skip.
    "355408": dict(n=2, load_id="355408", bill_id="62942", customer="Circle Logistics", carrier="Worldwide Logistics Inc"),
    # Kar Korner, Bill To named on the row, Kyle by default: set (two loads).
    "354657": dict(n=3, load_id="354657", bill_id="90888", customer="Kar Korner, Inc.", billto="Kar Korner, Inc.", consignor="Kar Korner, Inc."),
    "354704": dict(n=4, load_id="354704", bill_id="90888", customer="Kar Korner, Inc.", billto="Kar Korner, Inc.", consignor="Kar Korner, Inc."),
    # Kar Korner already carrying Victoria: left alone unless --overwrite.
    "360002": dict(n=5, load_id="360002", bill_id="90888", customer="Kar Korner, Inc.", sales1="STORM, VICTORIA"),
    # Kar Korner already carrying Kyle: already set.
    "360003": dict(n=6, load_id="360003", bill_id="90888", customer="Kar Korner, Inc.", sales1="LINK, KYLE"),
    # Kingbee, Victoria by default: set.
    "355837": dict(n=7, load_id="355837", bill_id="82722", customer="Kingbee Rentals, LLC", carrier="Kolos Inc"),
    # Kingbee row without a bulk checkbox: skip.
    "360005": dict(n=8, load_id="360005", bill_id="82722", customer="Kingbee Rentals, LLC", box=False),
    # USPS has no default rep: skip.
    "356131": dict(n=9, load_id="356131", bill_id="74623", customer="United States Postal Service"),
    # Avis: default rep Jack Gibson is not in the dropdown: skip.
    "360001": dict(n=10, load_id="360001", bill_id="70001", customer="Avis Trucks"),
    # Blank Bill To id, Customer id listed: matched by customer id -> Seth.
    "360004": dict(n=11, load_id="360004", bill_id="", cons_id="91418", customer="Fleet Lease Disposal, Inc."),
}
LAYOUT = [("CIRCLE LOGISTICS", ["345949", "355408"]), ("KAR KORNER, INC.", ["354657", "354704", "360002", "360003"]),
          ("KINGBEE RENTALS, LLC", ["355837", "360005"]), ("UNITED STATES POSTAL SERVICE", ["356131"]),
          ("AVIS TRUCKS", ["360001"]), ("FLEET LEASE DISPOSAL, INC.", ["360004"])]
ALL_LOADS = [lid for _, ids in LAYOUT for lid in ids]
BOXED = [lid for lid in ALL_LOADS if SPECS[lid].get("box", True)]


def form_head(marker, columns):
    return ('<FORM name="Menu_alt" method="POST" action="?" autocomplete="off" id="Menu_alt">'
            '<INPUT type="hidden" name="FORM::DEF" value="Menu"><INPUT type="hidden" name="source_time" value="1794250198">'
            f'<INPUT type="hidden" name="marker" value="{marker}"><INPUT type="hidden" name="start" value="-1">'
            '<INPUT type="hidden" name="Control__saved_id" value="2080"><INPUT type="hidden" name="Control__saved_type" value="priv">'
            '<INPUT type="hidden" name="Menu__SUBMITTED" value="1">'
            '<SELECT name="Menu__SalesRep" size="6" multiple><OPTION value="[ANY]"> Assigned<OPTION value="31033">KYLE LINK</SELECT>'
            '<INPUT type="checkbox" name="Menu__SalesRep__SalesRep" value="[NONE]" CHECKED id="Menu__SalesRep__SalesRep:[NONE]">'
            '<INPUT type="checkbox" name="Menu__Unchecked" value="Y">'
            '<SELECT name="Menu__perPage"><OPTION value="100">100<OPTION value="All" SELECTED>All</SELECT>'
            + "".join(f'<INPUT type="hidden" name="Menu__ViewFields" value="{key}">' for key, _ in columns)
            + '<INPUT use_XL="1" value="Submit" type="submit" name="Menu__is_submitted"><INPUT type="button" name="x" value="Tabstop By Row">')


FORM_TAIL = '<INPUT name="Menu__is_submitted" value="Submit" type="submit"></FORM>'


EMPTY_PAGE = ('<html><head><title>Circle</title></head><body>' + menu_form("Sales Rep Edit")
              + '<div id="LogoImage" style="display:none"></div><a name="View" id="ViewLoc"></a><!-- Listable view processing --> '
              "<div id='no_results_found'>No Results Found</div></body></html>")


def screen(*, without=(), replace=None, form=True, dropdown=True, marker="first", empty_page=False, columns=SCREEN_COLUMNS):
    """The Sales Rep Edit screen. ``empty_page`` is what CargoTel renders when no load matches:
    the Menu form and a No Results Found div, no grid, no Menu_alt form, no dropdown (live 2026-11-09)."""

    if empty_page:
        return EMPTY_PAGE
    parts = []
    shown = 0
    for title, ids in LAYOUT:
        members = [lid for lid in ids if lid not in without]
        if not members:
            continue
        parts.append(section(title))
        for lid in members:
            spec = dict(SPECS[lid])
            if replace and lid in replace:
                spec.update(replace[lid])
            parts.append(row(columns=columns, **spec))
            shown += 1
    grid = "<table>" + header(columns) + "".join(parts) + "</table>"
    body = (form_head(marker, columns) if form else "") + f'<div id="paginate">Rows 1 - {shown} of {shown}</div>' \
        + (DROPDOWN if dropdown else "") + grid + (FORM_TAIL if form else "")
    return '<html><head><title>Circle</title></head><body>' + menu_form("Sales Rep Edit") + body + "</body></html>"


class FakeCargoTel:
    """Answers a bulk POST like the live screen: the ticked loads get the rep and, being
    assigned, leave the unassigned-only screen; once nothing is left the bare No Results
    Found page comes back. ``shown`` is the set of loads on its screen (default: all of
    them), ``stuck`` loads stay on the screen unassigned (the write did not take), loads in
    ``vanish_after_first`` leave the screen after the first POST as if someone else had
    assigned them, and a rep in ``signin_for`` gets the sign-in page."""

    def __init__(self, *, shown=None, stuck=(), vanish_after_first=(), signin_for=()):
        self.posts = []
        self.assigned = {}
        self.shown = set(ALL_LOADS if shown is None else shown)
        self.stuck = set(stuck)
        self.vanish_after_first = set(vanish_after_first)
        self.signin_for = set(signin_for)

    def __call__(self, data):
        self.posts.append(data)
        fields = dict(data)
        rep = fields[BULK_SELECT]
        if rep in self.signin_for:
            return 403, SIGNIN_PAGE
        for name, value in data:
            if name.startswith("sales_rep_bulk//") and value not in self.stuck:
                self.assigned[value] = rep
        remaining = self.shown - set(self.assigned) - self.vanish_after_first
        if not remaining:
            return 200, screen(empty_page=True)
        return 200, screen(without=set(ALL_LOADS) - remaining, marker=f"after{len(self.posts)}")


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


class NormNameTests(unittest.TestCase):
    def test_last_first_and_first_last_meet(self):
        self.assertEqual(norm_name("CROMWELL, JUSTICE"), "JUSTICE CROMWELL")
        self.assertEqual(norm_name("Justice Cromwell"), "JUSTICE CROMWELL")
        self.assertEqual(norm_name("  justice   cromwell "), "JUSTICE CROMWELL")
        self.assertEqual(norm_name("Link,Kyle"), "KYLE LINK")
        self.assertEqual(norm_name("J. Cromwell"), "J CROMWELL")

    def test_empty(self):
        self.assertEqual(norm_name(""), "")
        self.assertEqual(norm_name(None), "")


class CustomerListTests(unittest.TestCase):
    def test_parses_every_customer(self):
        customers = parse_customers(customer_page())
        self.assertEqual(len(customers), 7)
        self.assertEqual(customers["62943"].name, "STELLANTIS (FCA)")
        self.assertEqual(customers["62943"].key, "FCA")
        self.assertEqual(customers["62943"].default_rep, "CROMWELL, JUSTICE")
        self.assertEqual(customers["74623"].default_rep, "")
        self.assertEqual(customers["90888"].default_rep, "LINK, KYLE")

    def test_missing_column_is_an_error(self):
        columns = [c for c in CUSTOMER_COLUMNS if c[0] != "defaultPrimaryrep"]
        with self.assertRaisesRegex(RuntimeError, "defaultPrimaryrep"):
            parse_customers(customer_page(columns=columns))

    def test_partial_page_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "rows 1-7 of 57"):
            parse_customers(customer_page(total=57))

    def test_signin_page_is_an_error(self):
        with self.assertRaisesRegex(RuntimeError, "sign-in"):
            parse_customers(SIGNIN_PAGE, 403)


class ScreenTests(unittest.TestCase):
    def test_parses_loads_with_bill_to_and_names(self):
        grid, loads = parse_screen(screen())
        self.assertEqual([load.load_id for load in loads], ALL_LOADS)
        by_id = {load.load_id: load for load in loads}
        self.assertEqual(by_id["345949"].bill_id, "71573")
        self.assertEqual(by_id["345949"].cons_id, "71573")
        self.assertEqual(by_id["345949"].customer, "Circle Logistics")          # N/A Bill To -> Original Consignor
        self.assertEqual(by_id["354657"].customer, "Kar Korner, Inc.")           # Bill To named on the row
        self.assertEqual(by_id["360002"].sales_rep, "STORM, VICTORIA")
        self.assertEqual(by_id["345949"].sales_rep, "")
        self.assertEqual(by_id["360004"].bill_id, "")
        self.assertEqual(by_id["360004"].cons_id, "91418")
        self.assertEqual(by_id["345949"].section, "CIRCLE LOGISTICS")
        self.assertEqual(by_id["345949"].carrier, "Ag Logistics LLC")
        self.assertEqual(by_id["345949"].pickup_eta, "10/15/2026 17:36")
        self.assertFalse(grid.is_partial)

    def test_section_is_the_fallback_name(self):
        html = screen(replace={"345949": dict(customer="N/A", billto="N/A", consignor="N/A")})
        _, loads = parse_screen(html)
        self.assertEqual(loads[0].customer, "CIRCLE LOGISTICS")

    def test_empty_screen(self):
        page = screen(empty_page=True)
        self.assertIsNone(screen_problem(page, 200))
        grid, loads = parse_screen(page)
        self.assertEqual(loads, [])
        self.assertEqual(grid.rows, [])
        with self.assertRaisesRegex(RuntimeError, "dropdown"):
            parse_rep_options(page)

    def test_rep_options(self):
        options = parse_rep_options(screen())
        self.assertEqual(options, REP_IDS)
        self.assertNotIn(UNASSIGN, options.values())

    def test_boxes(self):
        self.assertEqual(parse_boxes(menu_alt_form(screen())), set(BOXED))

    def test_not_the_screen(self):
        self.assertIsNone(screen_problem(screen(), 200))
        self.assertFalse(is_empty_screen(screen()))
        self.assertTrue(is_empty_screen(screen(empty_page=True)))
        self.assertIn("HTTP 500", screen_problem(screen(), 500))
        self.assertIn("sign-in", screen_problem(SIGNIN_PAGE, 403))
        self.assertIn("Menu_alt", screen_problem(screen(form=False), 200))
        self.assertIn("dropdown", screen_problem(screen(dropdown=False), 200))
        self.assertIn("CargoTel reported", screen_problem(screen().replace("<body>", "<body>An Error was encountered: <b>boom</b>"), 200))
        with self.assertRaisesRegex(RuntimeError, "not the Sales Rep Edit screen"):
            parse_screen(screen(form=False))

    def test_missing_bill_to_column(self):
        columns = [c for c in SCREEN_COLUMNS if c[0] != "bill_id"]
        with self.assertRaisesRegex(RuntimeError, "bill_id"):
            parse_screen(screen(columns=columns))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.customers = parse_customers(customer_page())
        html = screen()
        self.grid, self.loads = parse_screen(html)
        self.options = parse_rep_options(html)
        self.boxes = parse_boxes(menu_alt_form(html))

    def decisions(self, **kw):
        return {d.load.load_id: d for d in plan(self.loads, self.customers, self.options, boxes=self.boxes, **kw)}

    def test_sets_and_skips(self):
        d = self.decisions()
        self.assertEqual((d["345949"].action, d["345949"].rep_name, d["345949"].rep_id), (SET, "JUSTICE CROMWELL", "26640"))
        self.assertEqual((d["354657"].action, d["354657"].rep_id), (SET, "31033"))
        self.assertEqual((d["354704"].action, d["354704"].rep_id), (SET, "31033"))
        self.assertEqual((d["355837"].action, d["355837"].rep_id), (SET, "36426"))
        self.assertEqual(d["355408"].action, SKIP)
        self.assertIn("62942", d["355408"].reason)
        self.assertIn("not on the Customer List", d["355408"].reason)
        self.assertEqual(d["356131"].action, SKIP)
        self.assertIn("no Default Primary Rep", d["356131"].reason)
        self.assertEqual(d["360001"].action, SKIP)
        self.assertIn("GIBSON, JACK", d["360001"].reason)
        self.assertIn("not in the Primary Sales Rep dropdown", d["360001"].reason)
        self.assertEqual(d["360003"].action, SKIP)
        self.assertEqual(d["360003"].reason, "already set")
        self.assertEqual(d["360002"].action, SKIP)
        self.assertIn("--overwrite", d["360002"].reason)
        self.assertEqual(d["360005"].action, SKIP)
        self.assertIn("checkbox", d["360005"].reason)

    def test_blank_bill_to_matches_by_customer_id(self):
        d = self.decisions()["360004"]
        self.assertEqual((d.action, d.rep_name, d.rep_id), (SET, "SETH EDMONDSON", "36153"))
        self.assertIn("matched by customer id 91418", d.reason)

    def test_overwrite(self):
        d = self.decisions(overwrite=True)
        self.assertEqual((d["360002"].action, d["360002"].rep_id), (SET, "31033"))
        self.assertEqual(d["360003"].action, SKIP)   # already Kyle

    def test_groups_by_rep_in_name_order(self):
        planned = groups(plan(self.loads, self.customers, self.options, boxes=self.boxes))
        self.assertEqual([(name, rep_id, [d.load.load_id for d in members]) for name, rep_id, members in planned], [
            ("JUSTICE CROMWELL", "26640", ["345949"]),
            ("KYLE LINK", "31033", ["354657", "354704"]),
            ("SETH EDMONDSON", "36153", ["360004"]),
            ("VICTORIA STORM", "36426", ["355837"]),
        ])

    def test_select_filters(self):
        decisions = select(plan(self.loads, self.customers, self.options, boxes=self.boxes), only_load=["354657"])
        self.assertEqual([d.load.load_id for d in decisions if d.action == SET], ["354657"])
        self.assertIn("--only-load", next(d for d in decisions if d.load.load_id == "354704").reason)

        decisions = select(plan(self.loads, self.customers, self.options, boxes=self.boxes), only_rep="kyle link")
        self.assertEqual([d.load.load_id for d in decisions if d.action == SET], ["354657", "354704"])

        decisions = select(plan(self.loads, self.customers, self.options, boxes=self.boxes), only_customer="kingbee")
        self.assertEqual([d.load.load_id for d in decisions if d.action == SET], ["355837"])
        decisions = select(plan(self.loads, self.customers, self.options, boxes=self.boxes), only_customer="90888")
        self.assertEqual([d.load.load_id for d in decisions if d.action == SET], ["354657", "354704"])

        decisions = select(plan(self.loads, self.customers, self.options, boxes=self.boxes), max_changes=2)
        self.assertEqual([d.load.load_id for d in decisions if d.action == SET], ["345949", "354657"])
        self.assertIn("--max-changes 2", next(d for d in decisions if d.load.load_id == "354704").reason)

        decisions = select(plan(self.loads, self.customers, self.options, boxes=self.boxes), max_changes=-1)
        self.assertEqual(sum(1 for d in decisions if d.action == SET), 5)

    def test_summary(self):
        summary = summarize(plan(self.loads, self.customers, self.options, boxes=self.boxes))
        self.assertEqual((summary["loads"], summary["set"], summary["skipped"]), (11, 5, 6))
        self.assertEqual(summary["by_rep"], {"JUSTICE CROMWELL": 1, "KYLE LINK": 2, "SETH EDMONDSON": 1, "VICTORIA STORM": 1})


class BulkPostTests(unittest.TestCase):
    def test_form_fields_follow_the_browser(self):
        fields = form_fields(menu_alt_form(screen()))
        names = [n for n, _ in fields]
        self.assertIn(("Menu__SalesRep__SalesRep", "[NONE]"), fields)          # checked box
        self.assertNotIn("Menu__Unchecked", names)                             # unchecked box
        self.assertNotIn("Menu__SalesRep", names)                              # empty multi-select
        self.assertIn(("Menu__perPage", "All"), fields)                        # selected option
        self.assertIn(("Control__seteta__345949__month_eta", "11"), fields)    # the ETA widget as shown
        self.assertIn((BULK_SELECT, "37899"), fields)                          # first option when none selected
        self.assertNotIn(("x", "Tabstop By Row"), fields)                      # buttons left out
        self.assertEqual(names.count("Menu__is_submitted"), 0)                 # unpressed Submit buttons are not sent; build_bulk_post adds the press

    def test_builds_the_post_for_one_rep(self):
        data = build_bulk_post(screen(), "31033", ["354657", "354704"])
        ticked = [(n, v) for n, v in data if n.startswith("sales_rep_bulk//")]
        self.assertEqual(ticked, [("sales_rep_bulk//354657", "354657"), ("sales_rep_bulk//354704", "354704")])
        self.assertEqual([v for n, v in data if n == BULK_SELECT], ["31033"])
        self.assertEqual(data[-1], ("Menu__is_submitted", "Submit"))
        self.assertEqual(sum(1 for n, _ in data if n == "Menu__is_submitted"), 1)
        self.assertEqual(sum(1 for n, _ in data if n == "sales_rep_bulk_shown"), len(BOXED))
        self.assertEqual(sum(1 for n, _ in data if n == "STATESEED"), len(BOXED))
        self.assertIn(("Menu__SalesRep__SalesRep", "[NONE]"), data)
        self.assertIn(("Control__saved_id", "2080"), data)
        self.assertEqual([v for n, v in data if n == "Menu__ViewFields"], [k for k, _ in SCREEN_COLUMNS])
        # document order: the box comes right before its row's hidden sales_rep_bulk_shown
        names = [n for n, _ in data]
        i = names.index("sales_rep_bulk//354657")
        self.assertEqual(data[i + 1], ("sales_rep_bulk_shown", "354657"))

    def test_refusals(self):
        with self.assertRaisesRegex(ValueError, "Unassign"):
            build_bulk_post(screen(), UNASSIGN, ["354657"])
        with self.assertRaisesRegex(ValueError, "Unassign"):
            build_bulk_post(screen(), "", ["354657"])
        with self.assertRaisesRegex(ValueError, "no loads"):
            build_bulk_post(screen(), "31033", [])
        with self.assertRaisesRegex(RuntimeError, "999999"):
            build_bulk_post(screen(), "31033", ["354657", "999999"])
        with self.assertRaisesRegex(RuntimeError, "360005"):
            build_bulk_post(screen(), "36426", ["360005"])   # row without a box
        with self.assertRaisesRegex(RuntimeError, "Menu_alt"):
            build_bulk_post(screen(form=False), "31033", ["354657"])


class VerifyTests(unittest.TestCase):
    def setUp(self):
        html = screen()
        _, loads = parse_screen(html)
        customers = parse_customers(customer_page())
        self.decisions = plan(loads, customers, parse_rep_options(html), boxes=parse_boxes(menu_alt_form(html)))
        self.kyle = [d for d in self.decisions if d.rep_id == "31033" and d.action == SET]

    def test_gone_or_showing_the_rep_is_verified(self):
        after, _ = parse_screen(screen(without={"354657"}, replace={"354704": dict(sales1="LINK, KYLE")}))
        verify(after, self.kyle)
        self.assertEqual([(d.load.load_id, d.result) for d in self.kyle], [("354657", VERIFIED), ("354704", VERIFIED)])
        self.assertEqual(self.kyle[0].note, "left the unassigned screen")
        self.assertEqual(self.kyle[1].note, "Sales Rep 1 shows the rep")

    def test_still_unassigned_is_failed(self):
        after, _ = parse_screen(screen(without={"354657"}))
        verify(after, self.kyle)
        self.assertEqual([(d.load.load_id, d.result) for d in self.kyle], [("354657", VERIFIED), ("354704", FAILED)])
        self.assertIn("still on the screen", self.kyle[1].note)
        self.assertIn("(none)", self.kyle[1].note)

    def test_skips_are_not_touched(self):
        after, _ = parse_screen(screen(empty_page=True))
        verify(after, self.decisions)
        self.assertTrue(all(d.result == "" for d in self.decisions if d.action == SKIP))
        self.assertTrue(all(d.result == VERIFIED for d in self.decisions if d.action == SET))


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self.page = screen()
        _, loads = parse_screen(self.page)
        customers = parse_customers(customer_page())
        self.decisions = plan(loads, customers, parse_rep_options(self.page), boxes=parse_boxes(menu_alt_form(self.page)))

    def test_one_post_per_rep_chained_off_the_previous_page(self):
        cargotel = FakeCargoTel()
        last = apply_changes(self.page, self.decisions, cargotel, now=NOW)
        self.assertEqual(len(cargotel.posts), 4)
        self.assertEqual([dict(p)[BULK_SELECT] for p in cargotel.posts], ["26640", "31033", "36153", "36426"])
        self.assertEqual(cargotel.assigned, {"345949": "26640", "354657": "31033", "354704": "31033", "360004": "36153", "355837": "36426"})
        # the second POST was built from the page the first one rendered
        self.assertEqual([dict(p)["marker"] for p in cargotel.posts], ["first", "after1", "after2", "after3"])
        self.assertTrue(all(d.result == VERIFIED for d in self.decisions if d.action == SET))
        self.assertTrue(all(d.result == "" for d in self.decisions if d.action == SKIP))
        _, remaining = parse_screen(last)
        self.assertEqual([load.load_id for load in remaining], ["355408", "360002", "360003", "360005", "356131", "360001"])

    def test_a_load_that_did_not_take_is_failed_and_the_rest_go_on(self):
        cargotel = FakeCargoTel(stuck={"354704"})
        apply_changes(self.page, self.decisions, cargotel, now=NOW)
        by_id = {d.load.load_id: d for d in self.decisions}
        self.assertEqual(by_id["354657"].result, VERIFIED)
        self.assertEqual(by_id["354704"].result, FAILED)
        self.assertEqual(by_id["355837"].result, VERIFIED)
        self.assertEqual(len(cargotel.posts), 4)

    def test_an_unusable_response_stops_the_run(self):
        cargotel = FakeCargoTel(signin_for={"31033"})
        apply_changes(self.page, self.decisions, cargotel, now=NOW)
        by_id = {d.load.load_id: d for d in self.decisions}
        self.assertEqual(by_id["345949"].result, VERIFIED)              # Justice went first
        self.assertEqual(by_id["354657"].result, FAILED)                # Kyle's POST got the sign-in page
        self.assertIn("sign-in", by_id["354657"].note)
        self.assertEqual(by_id["360004"].result, "")                    # Seth and Victoria were never posted
        self.assertEqual(by_id["355837"].result, "")
        self.assertEqual(len(cargotel.posts), 2)

    def test_last_post_empties_the_screen(self):
        """The live 15:03 ET case: the POST that assigns the last loads comes back as the bare
        No Results Found page, which means every posted load left the screen."""

        skips = {d.load.load_id for d in self.decisions if d.action == SKIP}
        page = screen(without=skips)
        _, loads = parse_screen(page)
        decisions = plan(loads, parse_customers(customer_page()), parse_rep_options(page), boxes=parse_boxes(menu_alt_form(page)))
        cargotel = FakeCargoTel(shown=set(ALL_LOADS) - skips)
        last = apply_changes(page, decisions, cargotel, now=NOW)
        self.assertEqual(len(cargotel.posts), 4)
        self.assertEqual([(d.load.load_id, d.result) for d in decisions],
                         [("345949", VERIFIED), ("354657", VERIFIED), ("354704", VERIFIED), ("355837", VERIFIED), ("360004", VERIFIED)])
        self.assertEqual(decisions[3].note, "left the unassigned screen")
        self.assertTrue(screen_problem(last, 200) is None and parse_screen(last)[1] == [])

    def test_a_load_gone_before_its_post_is_not_claimed(self):
        cargotel = FakeCargoTel(vanish_after_first={"355837"})
        apply_changes(self.page, self.decisions, cargotel, now=NOW)
        self.assertEqual([dict(p)[BULK_SELECT] for p in cargotel.posts], ["26640", "31033", "36153"])   # no POST for Victoria
        victoria = next(d for d in self.decisions if d.load.load_id == "355837")
        self.assertEqual((victoria.action, victoria.result), (SKIP, ""))
        self.assertIn("before this rep's POST", victoria.reason)
        self.assertNotIn("355837", cargotel.assigned)

    def test_pages_are_saved(self):
        folder = tempfile.mkdtemp()
        try:
            apply_changes(self.page, self.decisions, FakeCargoTel(), save_pages=folder, now=NOW)
            names = sorted(os.listdir(folder))
            self.assertEqual(len(names), 4)
            self.assertTrue(names[0].startswith("after_20261109_1430_1_26640"))
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class RunOnceTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp()
        self.outbox = os.path.join(self.work, "outbox")
        self.reports = os.path.join(self.work, "reports")
        self.changes = os.path.join(self.work, "state", "changes.jsonl")

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def run_once(self, **kw):
        with redirect_stdout(io.StringIO()) as out:
            summary = run_once(now=NOW, screen_html=screen(), customers_html=customer_page(), outbox=self.outbox,
                               reports=self.reports, changes_log=self.changes, **kw)
        return summary, out.getvalue()

    def test_dry_run_writes_the_plan_and_posts_nothing(self):
        cargotel = FakeCargoTel()
        summary, out = self.run_once(post=cargotel)
        self.assertEqual(cargotel.posts, [])
        self.assertEqual((summary["set"], summary["verified"], summary["failed"], summary["skipped"]), (5, 0, 0, 6))
        self.assertFalse(summary["apply"])
        self.assertEqual(os.listdir(self.outbox), ["sales_rep_sync_2026-11-09_1430.json"])
        with open(summary["report"], encoding="utf-8") as fh:
            report = json.load(fh)
        self.assertEqual(len(report["decisions"]), 11)
        self.assertEqual({d["result"] for d in report["decisions"] if d["action"] == SET}, {DRY_RUN})
        self.assertFalse(os.path.exists(self.changes))
        self.assertFalse(os.path.exists(self.reports))
        self.assertIn("354657", out)
        self.assertIn("dry-run", out)

    def test_apply_writes_report_and_change_log(self):
        cargotel = FakeCargoTel(stuck={"354704"})
        summary, _ = self.run_once(apply=True, post=cargotel)
        self.assertEqual(len(cargotel.posts), 4)
        self.assertEqual((summary["set"], summary["verified"], summary["failed"]), (5, 4, 1))
        self.assertTrue(summary["apply"])
        self.assertEqual(os.listdir(self.reports), ["sales_rep_sync_2026-11-09_1430.json"])
        self.assertFalse(os.path.exists(self.outbox))
        with open(self.changes, encoding="utf-8") as fh:
            lines = [json.loads(line) for line in fh]
        self.assertEqual(len(lines), 5)
        self.assertEqual({(line["load_id"], line["result"]) for line in lines},
                         {("345949", VERIFIED), ("354657", VERIFIED), ("354704", FAILED), ("360004", VERIFIED), ("355837", VERIFIED)})
        self.assertEqual(lines[0]["at"], "2026-11-09T14:30:00")
        self.assertIn("rep_id", lines[0])

    def test_empty_screen_is_nothing_to_do(self):
        cargotel = FakeCargoTel()
        with redirect_stdout(io.StringIO()):
            summary = run_once(now=NOW, screen_html=screen(empty_page=True), customers_html=customer_page(), apply=True, post=cargotel,
                               outbox=self.outbox, reports=self.reports, changes_log=self.changes)
        self.assertEqual(cargotel.posts, [])
        self.assertEqual((summary["loads"], summary["set"], summary["screen_rows"]), (0, 0, 0))
        self.assertFalse(os.path.exists(self.changes))

    def test_only_load_and_overwrite(self):
        cargotel = FakeCargoTel()
        summary, _ = self.run_once(apply=True, post=cargotel, only_load=["360002"], overwrite=True)
        self.assertEqual(len(cargotel.posts), 1)
        self.assertEqual(cargotel.assigned, {"360002": "31033"})
        self.assertEqual((summary["set"], summary["verified"]), (1, 1))

    def test_main_list_mode_offline(self):
        folder = tempfile.mkdtemp()
        try:
            screen_path, customers_path = os.path.join(folder, "screen.html"), os.path.join(folder, "customers.html")
            with open(screen_path, "w", encoding="utf-8") as fh:
                fh.write(screen())
            with open(customers_path, "w", encoding="utf-8") as fh:
                fh.write(customer_page())
            with redirect_stdout(io.StringIO()) as out:
                rc = main(["--list", "--from-html", screen_path, "--customers-html", customers_path, "--now", "2026-11-09T14:30",
                           "--outbox", self.outbox, "--reports", self.reports, "--changes-log", self.changes])
            self.assertEqual(rc, 0)
            self.assertIn("JUSTICE CROMWELL", out.getvalue())
            self.assertFalse(os.path.exists(self.outbox))
            self.assertFalse(os.path.exists(self.changes))
            with redirect_stdout(io.StringIO()):
                rc = main(["--apply", "--from-html", screen_path, "--customers-html", customers_path])
            self.assertEqual(rc, 2)
        finally:
            shutil.rmtree(folder, ignore_errors=True)


class LambdaArgsTests(unittest.TestCase):
    def test_env_and_event_become_arguments(self):
        import sales_rep_lambda  # noqa: WPS433

        work = os.path.join("w")
        args = sales_rep_lambda.sync_args(work, {"SRS_APPLY": "1", "SRS_MAX_CHANGES": "50", "SRS_OVERWRITE": "1", "SRS_SAVE_PAGES": "1"},
                                          NOW, {"only_load": "354657"})
        self.assertIn("--apply", args)
        self.assertIn("--overwrite", args)
        self.assertEqual(args[args.index("--max-changes") + 1], "50")
        self.assertEqual(args[args.index("--only-load") + 1], "354657")
        self.assertEqual(args[args.index("--save-pages") + 1], os.path.join(work, "pages"))
        self.assertEqual(args[args.index("--changes-log") + 1], os.path.join(work, "state", "changes.jsonl"))

        args = sales_rep_lambda.sync_args(work, {"SRS_APPLY": "1"}, NOW, {"dry_run": True})
        self.assertNotIn("--apply", args)
        self.assertNotIn("--overwrite", args)
        self.assertEqual(args[args.index("--max-changes") + 1], "200")

        args = sales_rep_lambda.sync_args(work, {}, NOW, None)
        self.assertNotIn("--apply", args)


if __name__ == "__main__":
    unittest.main()
