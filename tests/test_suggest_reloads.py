"""Unit tests for suggest_reloads.py (``python -m unittest``).

Fixtures mirror the live markup seen on 2026-10-12: the loadmaint.mcgi min_/max_ cells,
the ``ass_user`` dispatch-rep select, the "Automobile <VIN> | <year make model>" vehicle
lines, and a General Load Build w/ Map grid with the one-column class shift, bundle
members linking their parent in the Load column, and the radius-center echo.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cargotel_search import parse_grid  # noqa: E402
from suggest_reloads import (  # noqa: E402
    DispatchedLoad,
    Stop,
    group_candidates,
    parse_load_page,
    pick_suggestions,
    render_email,
    suggest,
)

LOAD_PAGE = """
<html><body>
<td id='min_customer' valign=top>Stellantis (fca) (FCA)<br>Auburn Hills, MI 48326</td>
<td id='min_origin' valign=top>10-Oct-2026 Sat 3 PM EST<br>Port Of Jacksonville Import Ss (JXNVLPT)<br>Jacksonville, FL 32206, 491200301</td>
<td id='min_destination' valign=top>12-Oct-2026 Mon 11 AM EST<br>Bettenhausen Motor Sales Inc (54982)<br>Orland Hills, IL 60477, 381457000</td>
<td id='min_carrier' valign=top>88 Transpoort Llc<br>Lombard, IL</td>
<td id='max_carrier'><font size=2>ID: 80542<br> Phone: (561) 223-0920<br> Eugen Padureanu <br> 88 TRANSPOORT LLC<br> 237 ARBORETUM DR<br> LOMBARD, IL <br> Email: <a href="mailto:dispatch@example.com">dispatch@example.com</a><br> Fax: (000) 000-0000<br> </font></td>
<input type="hidden" name="CarrierSelIDNames" value="{field_name}Carriers_344141">
<input type="hidden" id="selCar_344141" name="selCar_344141" value="80542">
<div selected_carrier_id="carrier_id" selected_carrier_value="80542" selected_carrier_name="88 TRANSPOORT LLC"></div>
<td>Dispatch Rep:</td><TD><select name="ass_user"><option value=0>Click here to assign User</option>
<option value="26636">ADMIN, CARGOTEL <option value="39093" SELECTED>BUJAGA, AMOSSI
<option value="26640">CROMWELL, JUSTICE </select></TD>
<td>Pri. Sales Rep</td><TD><select name="sales_rep"><option value=0>None</option><option value="27877" SELECTED>MITCHELL, STUART
<option value="26640">CROMWELL, JUSTICE </select></TD>
<td>Sec. Sales Rep</td><TD><select name="sales_rep2"><option value=0 SELECTED>None</option><option value="26640">CROMWELL, JUSTICE </select></TD>
<table><tr><td><font>Stellantis (fca)<br>Automobile VYF9AZ2D1V5000364<br>2027 FIAT TOPOLINO DOLCE VITA<br><a href="#">x</a></font></td></tr>
<tr><td><font>Stellantis (fca)<br>Automobile VYF9AZ2D3V5000379<br>2027 FIAT TOPOLINO DOLCE VITA<br><a href="#">x</a></font></td></tr>
<tr><td><font>Stellantis (fca)<br>Automobile VYF9AZ2D3V5000379<br>2027 FIAT TOPOLINO DOLCE VITA<br><a href="#">x</a></font></td></tr></table>
</body></html>
"""

UNASSIGNED_PAGE = LOAD_PAGE.replace('value="80542"', 'value=""').replace(
    'selected_carrier_name="88 TRANSPOORT LLC"', 'selected_carrier_name=""'
).replace("<td id='min_carrier' valign=top>88 Transpoort Llc<br>Lombard, IL</td>", "<td id='min_carrier'></td>").replace(
    "ID: 80542<br> Phone: (561) 223-0920<br> Eugen Padureanu <br> 88 TRANSPOORT LLC<br> 237 ARBORETUM DR<br> LOMBARD, IL <br> Email: <a href=\"mailto:dispatch@example.com\">dispatch@example.com</a><br> Fax: (000) 000-0000<br> ",
    '<a href="#">[Edit]</a><br>',
)

DELIVERED_PAGE = """
<html><body>
<input type="hidden" name="loadmaint_form__shipper_id" value="">
<td id='min_customer' valign=top>Elrac, Llc (EM)<br>Latham, NY 12110</td>
<td id='min_origin' valign=top>09-Oct-2026 Fri 5 PM EST<br>Enterprise Group 24 - Uniondale<br>Uniondale, NY 11553, 179192000</td>
<td id='min_destination' valign=top>11-Oct-2026 Sun 3 PM<br>Enterprise Group 44 - Waukesha Wi<br>Waukesha, WI 53186, 337160000</td>
<td id='min_carrier' valign=top>R G Motors Transport Llc<br>Pensacola, FL 32503</td>
<td id='max_carrier'><font size=2>ID: 71699<br> Phone: (850) 555-0100<br> General Contact <br> R G MOTORS TRANSPORT LLC<br> 6207 N PALAFOX ST<br> PENSACOLA, FL 32503 <br> Email: <a href="mailto:rgmotors@example.com">rgmotors@example.com</a><br> Fax: (000) 000-0000<br> </font></td>
<td>Dispatch Rep:</td><TD><select name="ass_user"><option value=0>Click here to assign User</option>
<option value="31033" SELECTED>LINK, KYLE
<option value="26640">CROMWELL, JUSTICE </select></TD>
<table><tr><td><font>Elrac<br>Automobile 1FTBW2CG5TKA12345<br>2026 FORD TRANSIT<br><a href="#">x</a></font></td></tr></table>
</body></html>
"""

HEADER = """
<tr static="" id="det_header_row" class="unshadow dv_row">
<th class="dv_cell dv_string dv_header oeheader" id="row_number_head">#</th>
<th id="det_header_extra" class="dv_cell dv_header oeheader"><span class="dv_header_name">Map</span></th>
<th class="dv_cell dv_cell_age_SLL_d_Inventory dv_header dv_header_age_SLL_d_Inventory oeheader"><span class="dv_header_name">Age Inventory Date</span></th>
<th class="dv_cell dv_cell_load_id dv_header dv_header_load_id oeheader"><span class="dv_header_name">Order ID</span></th>
<th class="dv_cell dv_cell_bundle dv_header dv_header_bundle oeheader"><span class="dv_header_name">Load</span></th>
<th class="dv_cell dv_cell_origin dv_header dv_header_origin oeheader"><span class="dv_header_name">Origin</span></th>
<th class="dv_cell dv_cell_destination dv_header dv_header_destination oeheader"><span class="dv_header_name">Destination</span></th>
<th class="dv_cell dv_cell_model dv_header dv_header_model oeheader"><span class="dv_header_name">Model</span></th>
</tr>
"""

RADIUS_ECHO = (
    "<table><tr><td>From <a href=\"\" onclick=\"radius_fill_table('60477 ORLAND HILLS, IL','', 1.0, 75.0, 41.57, -87.79, 'red', 1);\">x</a></td></tr></table>"
)


def row(n, load_id, *, parent=None, origin="Elwood, Il 60421", orig_zip="60421", lat=41.42, lon=-88.11,
        dest="Rhinelander, WI 54501", dest_zip="54501", dest_state="WI", miles="320", price="469.96", age="3",
        model="2500", carrier_id="", orig_state="IL"):
    kind = "BundleChild" if parent else "Individual"
    link_class = "LinkBundleChild" if parent else "LinkIndividual"
    bundle_cell = (f'<FONT class="functions"><A href="/backoffice/loadmaint.mcgi?load_id={parent}">{parent}</A><BR></FONT>'
                   if parent else "Load 1 2 3 4 5 6 7 8 9 10 Or Existing")
    return f"""
<tr id="det_row_{n}"><td class="dv_linenumber">{n}</td>
<td class="dv_cell listable_cell dv_plain dv_cell_age_SLL_d_Inventory" id="DetCell_{n}_0"><script type="text/javascript">
(CgtObjectFactory('Load',{load_id}, {{orig_state:'{orig_state}',type:'{kind}',det_cnt:'1',popup_data:'',dest_zip:'{dest_zip}',weight:'4895',dest_state:'{dest_state}',price:'{price}',bill_id:'1',carrier_id:'{carrier_id}',miles:'{miles}',shipper_id:'2',orig_zip:'{orig_zip}',model:'{model}',cons_id:'3',dest_id:'4',id:'{load_id}',size:'1.67',_CgtClassName:'Load',orig_id:'5'}}));
MapPoints[{n}] = {{ origin: {{tzip:"{orig_zip}", rzip:"{dest}", size: 1.67, lat:{lat}, lon:{lon}, load_id:{load_id}, info:"", cs:'{origin}', canbundle:0,is_rush:0,inv_date:"" }}, dest: {{tzip:"{dest_zip}", rzip:"{origin}", size: 1.67, lat:45.6, lon:-89.4, info:"", load_id:{load_id}, cs:'{dest}', canbundle:0,is_rush:0,inv_date:"" }} }};
</script></td>
<td class="dv_cell listable_cell dv_number dv_cell_load_id" id="DetCell_{n}_1">{age}</td>
<td class="dv_cell listable_cell dv_number dv_cell_bundle" id="DetCell_{n}_2"><A name="MapId_{load_id}" class="{link_class}" href="/backoffice/loadmaint.mcgi?load_id={load_id}" target="_blank">{load_id}</A><input type='hidden' name='load_id' value="{load_id}"></td>
<td class="dv_cell listable_cell dv_plain dv_cell_origin" id="DetCell_{n}_3">{bundle_cell}</td>
<td class="dv_cell listable_cell dv_plain dv_cell_destination" id="DetCell_{n}_4"><B>Logistics Park</B><BR>{origin.rsplit(' ', 1)[0]}</td>
<td class="dv_cell listable_cell dv_plain dv_cell_model" id="DetCell_{n}_5"><b>Dealer</b><br>{dest.rsplit(' ', 1)[0]}</td>
<td class="dv_cell listable_cell dv_plain dv_cell_EXTRA" id="DetCell_{n}_6">{model}</td>
</tr>"""


def grid_page(rows, echo=True):
    body = "".join(rows)
    return ("<html><body>" + (RADIUS_ECHO if echo else "") + '<INPUT type="hidden" name="Control__keepTitle" value="General Load Build w/  Map"><table>'
            + HEADER + body + f"</table>{len(rows)} rows returned in 0.1s Rows 1 - {len(rows)} of {len(rows)}</body></html>")


def standard_rows():
    return [
        # built load P1: 2 units to WI, 21 mi away
        row(1, 500001, parent="700001", dest="Rhinelander, WI 54501"),
        row(2, 500002, parent="700001", dest="Cloverland, WI 54521", dest_zip="54521"),
        # built load P2: 3 units to IN (the carrier's home state), 21 mi away
        row(3, 500003, parent="700002", dest="Fort Wayne, IN 46802", dest_zip="46802", dest_state="IN", model="1500"),
        row(4, 500004, parent="700002", dest="Fort Wayne, IN 46802", dest_zip="46802", dest_state="IN", model="1500"),
        row(5, 500005, parent="700002", dest="Fort Wayne, IN 46802", dest_zip="46802", dest_state="IN", model="3500"),
        # single orders at various distances; 500010 is already assigned to a carrier
        row(6, 500006, dest="Burlington, WI 53105", dest_zip="53105", miles="117", age="1"),
        row(7, 500007, origin="Champaign, Il 61822", orig_zip="61822", lat=40.11, lon=-88.24, dest="Burnsville, MN 55337", dest_state="MN", age="9"),
        row(8, 500008, dest="Jacksonville, FL 32206", dest_zip="32206", dest_state="FL", price="0.00"),
        row(9, 500009, dest="Antigo, WI 54409", dest_zip="54409", miles="307"),
        row(10, 500010, dest="Madison, WI 53703", dest_zip="53703", carrier_id="80542"),
        row(11, 500011, dest="Abrams, WI 54101", dest_zip="54101", miles="259", age="0"),
    ]


def dispatched():
    return DispatchedLoad(
        load_id="344141",
        origin=Stop(city="Jacksonville", state="FL", zip="32206"),
        destination=Stop(name="Bettenhausen", city="Orland Hills", state="IL", zip="60477", when="12-Oct-2026 Mon 11 AM EST"),
    )


class LoadPageTests(unittest.TestCase):
    def test_fields(self):
        load = parse_load_page(LOAD_PAGE, "344141")
        self.assertEqual(load.load_id, "344141")
        self.assertEqual(load.customer, "Stellantis (fca) (FCA)")
        self.assertEqual((load.origin.city, load.origin.state, load.origin.zip), ("Jacksonville", "FL", "32206"))
        self.assertEqual(load.origin.date, "2026-10-10")
        self.assertEqual(load.destination.name, "Bettenhausen Motor Sales Inc (54982)")
        self.assertEqual((load.destination.city, load.destination.state, load.destination.zip), ("Orland Hills", "IL", "60477"))
        self.assertEqual(load.destination.when, "12-Oct-2026 Mon 11 AM EST")
        self.assertEqual(load.destination.date, "2026-10-12")
        self.assertEqual(load.carrier.client_id, "80542")
        self.assertEqual(load.carrier.name, "88 TRANSPOORT LLC")
        self.assertEqual(load.carrier.contact_name, "Eugen Padureanu")
        self.assertEqual(load.carrier.email, "dispatch@example.com")
        self.assertEqual(load.carrier.phone, "(561) 223-0920")
        self.assertEqual((load.carrier.city, load.carrier.state), ("Lombard", "IL"))
        self.assertEqual((load.dispatch_rep_id, load.dispatch_rep), ("39093", "BUJAGA, AMOSSI"))
        self.assertEqual((load.sales_rep_id, load.sales_rep), ("27877", "MITCHELL, STUART"))
        self.assertIsNone(load.sales_rep2)  # "None" option selected
        self.assertEqual(load.contact_rep_display, "Stuart Mitchell")
        self.assertEqual(load.cc_reps, ["MITCHELL, STUART"])
        self.assertEqual(load.units, 2)  # duplicate VIN line collapses
        self.assertEqual(load.vehicles[0]["description"], "2027 FIAT TOPOLINO DOLCE VITA")
        self.assertTrue(load.is_dispatched)
        self.assertEqual(load.vehicle_summary(), "2 x 2027 FIAT TOPOLINO DOLCE VITA")

    def test_unassigned_load(self):
        load = parse_load_page(UNASSIGNED_PAGE, "344141")
        self.assertFalse(load.is_dispatched)
        self.assertIsNone(load.carrier.client_id)
        self.assertIsNone(load.carrier.email)
        self.assertEqual(load.destination.zip, "60477")
        self.assertEqual(load.load_id, "344141")

    def test_load_id_from_page_when_not_given(self):
        self.assertEqual(parse_load_page(LOAD_PAGE).load_id, "344141")

    def test_rep_name_with_cargotel_doubled_apostrophes(self):
        page = LOAD_PAGE.replace('<option value="39093" SELECTED>BUJAGA, AMOSSI', '<option value="28048" SELECTED>O\'\'\'\'\'\'\'\'NEILL, BRIANNE')
        load = parse_load_page(page, "344141")
        self.assertEqual(load.dispatch_rep, "O'NEILL, BRIANNE")
        self.assertEqual(load.rep_display, "Brianne O'Neill")

    def test_delivered_page_without_carrier_widget(self):
        load = parse_load_page(DELIVERED_PAGE, "343512")
        self.assertEqual(load.carrier.client_id, "71699")
        self.assertEqual(load.carrier.name, "R G Motors Transport Llc")
        self.assertIsNone(load.carrier.contact_name)  # "General Contact" is a placeholder
        self.assertEqual(load.carrier.email, "rgmotors@example.com")
        self.assertEqual((load.carrier.city, load.carrier.state), ("Pensacola", "FL"))
        self.assertTrue(load.is_dispatched)
        self.assertEqual((load.destination.city, load.destination.state, load.destination.zip), ("Waukesha", "WI", "53186"))
        self.assertEqual(load.destination.when, "11-Oct-2026 Sun 3 PM")
        self.assertEqual(load.dispatch_rep, "LINK, KYLE")
        self.assertEqual(load.rep_display, "Kyle Link")
        self.assertIsNone(load.sales_rep)
        self.assertIsNone(load.contact_rep_display)  # no sales rep: nobody else stands in
        self.assertEqual(load.cc_reps, [])
        self.assertEqual(load.units, 1)
        email = render_email(load, [], delivered_at="10/11/2026 15:03")
        self.assertIn("Hi R G Motors Transport Llc,", email["body"])
        self.assertIn("You dropped load 343512 (2026 FORD TRANSIT) in Waukesha, WI 53186 on 10/11/2026.", email["body"])
        self.assertTrue(email["needs_sales_rep"])
        self.assertIn("[SALES REP NOT ASSIGNED]", email["body"])
        self.assertNotIn("Kyle Link", email["body"])


class GroupingTests(unittest.TestCase):
    def setUp(self):
        self.grid = parse_grid(grid_page(standard_rows()))

    def test_grid_fixture_parses_like_the_live_page(self):
        self.assertEqual(len(self.grid.rows), 11)
        self.assertTrue(self.grid.class_shift)
        self.assertEqual(self.grid.radius_center.label, "60477 ORLAND HILLS, IL")
        self.assertEqual(self.grid.rows[0].cells["bundle"].load_links, ["700001"])
        self.assertEqual(self.grid.rows[0].text("origin"), "Logistics Park | Elwood, Il")

    def test_bundles_grouped_and_assigned_rows_dropped(self):
        loads, orders = group_candidates(self.grid)
        self.assertEqual(sorted(s.load_id for s in loads), ["700001", "700002"])
        by_id = {s.load_id: s for s in loads}
        self.assertEqual(by_id["700002"].units, 3)
        self.assertEqual(by_id["700002"].member_ids, ["500003", "500004", "500005"])
        self.assertEqual(by_id["700002"].dest_states, ["IN"])
        self.assertEqual(by_id["700002"].models, [("1500", 2), ("3500", 1)])
        self.assertAlmostEqual(by_id["700002"].price, 3 * 469.96, places=2)
        self.assertEqual(by_id["700001"].destinations, [("Rhinelander, WI 54501", 1), ("Cloverland, WI 54521", 1)])
        self.assertEqual(sorted(s.load_id for s in orders), ["500006", "500007", "500008", "500009", "500011"])
        self.assertNotIn("500010", [s.load_id for s in orders])
        self.assertAlmostEqual(by_id["700001"].deadhead_miles, 19.5, delta=0.5)
        self.assertIsNone(next(s for s in orders if s.load_id == "500008").price)

    def test_exclude_ids(self):
        loads, orders = group_candidates(self.grid, {"700001", "500006"})
        self.assertEqual([s.load_id for s in loads], ["700002"])
        self.assertNotIn("500006", [s.load_id for s in orders])


class PickingTests(unittest.TestCase):
    def setUp(self):
        self.loads, self.orders = group_candidates(parse_grid(grid_page(standard_rows())))

    def test_loads_first_then_top_up_with_orders(self):
        picks = pick_suggestions(self.loads, self.orders, home_state="IN", back_state="FL", min_n=5, max_n=10)
        self.assertEqual([p.kind for p in picks], ["load", "load", "order", "order", "order"])
        self.assertEqual(picks[0].load_id, "700002")  # heads to the carrier's home state
        self.assertEqual(picks[0].direction, "home")
        self.assertEqual(picks[1].load_id, "700001")
        order_ids = [p.load_id for p in picks[2:]]
        self.assertIn("500008", order_ids)  # back toward Jacksonville, FL ranks first among orders
        self.assertEqual(picks[2].load_id, "500008")
        self.assertEqual(picks[2].direction, "back")
        self.assertNotIn("500007", order_ids)  # Champaign is ~115 mi away, loses to the Elwood orders

    def test_max_cap_and_orders_only(self):
        picks = pick_suggestions(self.loads, self.orders, min_n=5, max_n=1)
        self.assertEqual(len(picks), 1)
        self.assertEqual(picks[0].kind, "load")
        picks = pick_suggestions([], self.orders, min_n=5, max_n=10)
        self.assertEqual(len(picks), 5)
        self.assertTrue(all(p.kind == "order" for p in picks))
        self.assertEqual(pick_suggestions([], [], min_n=5, max_n=10), [])

    def test_enough_loads_means_no_orders(self):
        picks = pick_suggestions(self.loads, self.orders, min_n=2, max_n=10)
        self.assertEqual([p.kind for p in picks], ["load", "load"])

    def test_age_breaks_ties(self):
        # Same origin and direction: the oldest inventory (age 3, 1, 0 days) goes first.
        orders = [s for s in self.orders if s.load_id in ("500006", "500009", "500011")]
        picks = pick_suggestions([], orders, min_n=3, max_n=3)
        self.assertEqual([p.load_id for p in picks], ["500009", "500006", "500011"])
        self.assertLess(picks[0].score, picks[1].score)

    def test_price_breaks_ties(self):
        orders = [s for s in self.orders if s.load_id in ("500008", "500011")]
        for s in orders:
            s.age_days = 0
        picks = pick_suggestions([], orders, home_state="FL", back_state="FL", min_n=2, max_n=2)
        # 500008 heads home (FL) but is unpriced; the direction penalty still wins over the price bonus.
        self.assertEqual([p.load_id for p in picks], ["500008", "500011"])
        picks = pick_suggestions([], orders, min_n=2, max_n=2)
        self.assertEqual([p.load_id for p in picks], ["500011", "500008"])


class EmailTests(unittest.TestCase):
    def test_render(self):
        load = parse_load_page(LOAD_PAGE, "344141")
        grid = parse_grid(grid_page(standard_rows()))
        result = suggest(load, grid)
        picks = result.pop("picks_objects")
        email = render_email(load, picks)
        self.assertEqual(email["subject"], "Next loads near Orland Hills, IL after load 344141")
        self.assertEqual(email["to"], "dispatch@example.com")
        self.assertIn("Stuart Mitchell", email["cc"])  # the sales rep, not the dispatch rep
        self.assertIn("call Stuart Mitchell to book", email["body"])
        self.assertTrue(email["body"].rstrip().endswith("Stuart Mitchell\nSales Rep, Circle Logistics"))
        self.assertNotIn("Amossi Bujaga", email["body"])  # the dispatch rep is never named
        self.assertFalse(email["needs_sales_rep"])
        signed = render_email(load, picks, rep_signature={"title": "Sales Manager", "email": "stuart@example.com"})
        self.assertTrue(signed["body"].rstrip().endswith("Stuart Mitchell\nSales Manager, Circle Logistics\nstuart@example.com"))
        self.assertNotRegex(signed["body"], r"\(\d{3}\) \d{3}-\d{4}")
        self.assertIn("Hi Eugen Padureanu,", email["body"])
        self.assertIn("delivers 2 units to Orland Hills, IL 60477 on 12-Oct-2026 Mon 11 AM EST", email["body"])
        # The carrier on this page is based in IL, so neither built load heads "home" and
        # the two tie on score; the lower id goes first.
        self.assertIn("1. Load 700001: 2 units (2 x 2500) from Elwood, Il 60421 (20 mi from Orland Hills, IL) to Rhinelander, WI 54501 (1), Cloverland, WI 54521 (1), about 320 road mi - call for rate", email["body"])
        self.assertIn("2. Load 700002: 3 units (2 x 1500, 3500) from Elwood, Il 60421 (20 mi from Orland Hills, IL) to Fort Wayne, IN 46802, about 320 road mi - call for rate", email["body"])
        self.assertIn("3. Order 500008:", email["body"])  # back toward Jacksonville, FL leads the orders
        self.assertNotIn("$", email["body"])
        self.assertEqual(result["search"]["built_loads"], 2)
        self.assertEqual(result["search"]["single_orders"], 5)
        self.assertEqual(len(result["picks"]), 5)

    def test_show_pay_and_rep_email(self):
        load = parse_load_page(LOAD_PAGE, "344141")
        grid = parse_grid(grid_page(standard_rows()))
        picks = suggest(load, grid).pop("picks_objects")
        email = render_email(load, picks, show_pay=True, rep_email="rep@circledelivers.com")
        self.assertIn("$1,409.88", email["body"])
        self.assertEqual(email["cc"], "rep@circledelivers.com")

    def test_demo_mode_without_load_page(self):
        load = DispatchedLoad(load_id="demo", destination=Stop(zip="60477"))
        grid = parse_grid(grid_page(standard_rows()))
        picks = suggest(load, grid).pop("picks_objects")
        email = render_email(load, picks)
        self.assertIn("Next loads near 60477 after load demo", email["subject"])
        self.assertTrue(email["to"].startswith("<carrier dispatcher email"))

    def test_delivered_wording(self):
        load = parse_load_page(LOAD_PAGE, "344141")
        grid = parse_grid(grid_page(standard_rows()))
        picks = suggest(load, grid).pop("picks_objects")
        email = render_email(load, picks, delivered_at="10/12/2026 11:40")
        self.assertIn("You dropped load 344141 (2 x 2027 FIAT TOPOLINO DOLCE VITA) in Orland Hills, IL 60477 on 10/12/2026.", email["body"])
        self.assertNotIn("delivers", email["body"])
        email = render_email(load, picks, delivered_at="10/12/2026 11:40", trip_loads=["344100", "344120", "344141"])
        self.assertIn("You dropped loads 344100, 344120 and 344141 (2 x 2027 FIAT TOPOLINO DOLCE VITA), the last one in Orland Hills, IL 60477 on 10/12/2026.", email["body"])


if __name__ == "__main__":
    unittest.main()
