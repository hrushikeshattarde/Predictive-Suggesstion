"""Unit tests for cargotel_search.py. Run with ``python -m unittest`` from the project root.

Fixtures reproduce the markup patterns seen on live Search.mcgi pages (2026-10-12):
the header row, the extra Map column with its one-column class shift, the inline
CgtObjectFactory hash and MapPoints script, section header rows, the pagination footer,
the empty-result page and the sign-in page.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cargotel_search import (  # noqa: E402
    DATE_FIELDS,
    SearchParams,
    build_search_params,
    is_signin_page,
    parse_grid,
    parse_menu,
)

TITLE = '<INPUT type="hidden" name="Control__keepTitle" value="General Load Build w/  Map">'

HEADER_MAP = """
<tr static="" id="det_header_row" class="unshadow dv_row">
<th class="dv_cell dv_string dv_header oeheader" style="width: 3%" id="row_number_head">#</th>
<th id="det_header_extra" class="dv_cell dv_header oeheader"><span class="dv_header_name">Map</span></th>
<th class="dv_cell dv_cell_age_SLL_d_Inventory dv_header dv_header_age_SLL_d_Inventory oeheader" id="det_header_age_SLL_d_Inventory"><span class="dv_header_name">Age Inventory Date</span></th>
<th class="dv_cell dv_cell_load_id dv_header dv_header_load_id oeheader" id="det_header_load_id"><span class="dv_header_name">Order ID</span></th>
<th class="dv_cell dv_cell_rush dv_header dv_header_rush oeheader" id="det_header_rush"><span class="dv_header_name">Rush</span></th>
<th class="dv_cell dv_cell_bundle dv_header dv_header_bundle oeheader" id="det_header_bundle"><span class="dv_header_name">Load</span></th>
<th id="det_header_origin" class="dv_cell dv_cell_origin dv_header dv_header_origin oeheader"><span class="dv_header_name">Origin</span></th>
<th class="dv_cell dv_cell_destination dv_header dv_header_destination oeheader" id="det_header_destination"><span class="dv_header_name">Destination</span></th>
<th class="dv_cell dv_cell_carriername dv_header dv_header_carriername oeheader" id="det_header_carriername"><span class="dv_header_name">Carrier Name</span></th>
</tr>
"""


def map_row(n, load_id, age, origin_city, dest_city, orig_zip, dest_zip, lat, lon, link_class="LinkIndividual"):
    # Classes are deliberately shifted by one from the content, as CargoTel renders them.
    return f"""
<tr class="unshadow dv_row listable_row listable_row1" style="static" id="det_row_{n}" ="">
<td class="dv_cell dv_string dv_cell dv_linenumber listable_cell" valign="TOP"><nobr>{n}<INPUT type="hidden" value="{n}" name="Control__rows" id="Control__rows"></nobr></td>
<td style="" class="dv_cell listable_cell dv_plain dv_cell_age_SLL_d_Inventory" id="DetCell_{n}_0" valign="top"><nobr>
    <Input id="MAP_origin_{n}" value="1" onclick="MapCheck(this, 'origin',{n})" type="checkbox" CHECKED>
    </nobr>
    <script type="text/javascript">
    (CgtObjectFactory('Load',{load_id}, {{orig_state:'IA',type:'BundleChild',det_cnt:'1',popup_data:'<b>Order ID</b>: {load_id}\\n<b>Origin</b>: 2109 FOURTH AVE S, CLEAR LAKE, IA {orig_zip}, Phone: (515) 555-0100\\nIt\\'s a test\\n',dest_zip:'{dest_zip}',weight:'4895',dest_state:'WI',price:'0.00',bill_id:'85319',carrier_id:'',miles:'314',shipper_id:'62942',orig_zip:'{orig_zip}',model:'PROMASTER',cons_id:'85319',dest_id:'91003',id:'{load_id}',size:'1.67',_CgtClassName:'Load',orig_id:'88793'}}));
    MapPoints[{n}] = {{
origin: {{tzip:"{orig_zip}", rzip:"{dest_city} {dest_zip}", size: 1.67, lat:{lat}, lon:{lon}, load_id:{load_id}, info:"", cs:'{origin_city} {orig_zip}', canbundle:0,is_rush:0,inv_date:"0-09 " }},
dest: {{tzip:"{dest_zip}", rzip:"{origin_city} {orig_zip}", size: 1.67, lat:44.2999993523149, lon:-88.5300088889518, info:"", load_id:{load_id}, cs:'{dest_city} {dest_zip}', canbundle:0,is_rush:0,inv_date:"0-09 " }}
    }};
    </script>
    </td>
<td id="DetCell_{n}_1" style="" class="dv_cell listable_cell dv_number dv_cell_load_id" valign="top">{age}</td>
<td style="" class="dv_cell listable_cell dv_number dv_cell_rush" id="DetCell_{n}_2" valign="top"><A name="MapId_{load_id}" class="{link_class}" style="color: #006666" href="/backoffice/loadmaint.mcgi?load_id={load_id}" target="_blank" >{load_id}</A><input type='hidden' name='load_id' value="{load_id}"></td>
<td valign="top" class="dv_cell listable_cell dv_plain dv_cell_bundle" style="" id="DetCell_{n}_3">No</td>
<td valign="top" class="dv_cell listable_cell dv_plain dv_cell_origin" style="" id="DetCell_{n}_4"><FONT class="functions"><A style="color: blue" href="/backoffice/loadmaint.mcgi?load_id=344143">344143</A><BR></FONT></td>
<td style="" id="DetCell_{n}_5" class="dv_cell listable_cell dv_plain dv_cell_destination" valign="top"><B>2109 Fourth Ave S</B><BR>{origin_city}</td>
<td valign="top" style="" id="DetCell_{n}_6" class="dv_cell listable_cell dv_plain dv_cell_carriername"><b>W6331 Wally Wy</b><br>{dest_city}</td>
<td style="" id="DetCell_{n}_7" class="dv_cell listable_cell dv_plain dv_cell_EXTRA" valign="top">Acme &amp; Sons Transport</td>
</tr>
"""


PAGE_MAP = (
    "<html><body>" + TITLE + "<table>" + HEADER_MAP
    + '<tr static="" class="unshadow dv_row dv_section_header dv_sum_row"><th colspan="17" align="left">Origin: 2109 FOURTH AVE S</th></tr>'
    + map_row(1, 344134, 3, "Clear Lake, IA", "Greenville, WI", "50428", "54942", 43.133009, -93.362922, "LinkBundleChild")
    + map_row(2, 344135, 5, "Mason City, IA", "Appleton, WI", "50401", "54911", 43.153, -93.201)
    + '<tr static="" class="unshadow dv_row dv_section_header dv_sum_row"><th colspan="17" align="left">Origin: 500 MAIN ST</th></tr>'
    + map_row(3, 344136, 7, "Ames, IA", "Madison, WI", "50010", "53703", 42.034, -93.62)
    + "</table><script>hideDropList(); 3 rows returned in 0.0273s</script>"
    + "First 30 | &lt;&lt; Rows 1 - 3 of 3 &gt;&gt; [1-30]</body></html>"
)

HEADER_PLAIN = """
<tr static="" id="det_header_row" class="unshadow dv_row">
<th class="dv_cell dv_string dv_header oeheader" style="width: 3%" id="row_number_head">#</th>
<th class="dv_cell dv_cell_status dv_header dv_header_status oeheader" id="det_header_status"><span class="dv_header_name">Status</span></th>
<th class="dv_cell dv_cell_load_id dv_header dv_header_load_id oeheader" id="det_header_load_id"><span class="dv_header_name">Order ID</span></th>
<th class="dv_cell dv_cell_carriername dv_header dv_header_carriername oeheader" id="det_header_carriername"><span class="dv_header_name">Carrier Name</span></th>
</tr>
"""

PAGE_PLAIN = (
    '<html><body><INPUT type="hidden" value="Assigned Carrier Report" name="Control__keepTitle"><table>' + HEADER_PLAIN
    + '<tr id="det_row_1"><td class="dv_linenumber">1</td>'
    + '<td class="dv_cell listable_cell dv_plain dv_cell_status" id="DetCell_1_0">Dispatched</td>'
    + '<td class="dv_cell listable_cell dv_number dv_cell_load_id" id="DetCell_1_1"><A name="MapId_343514" class="LinkIndividual" href="/backoffice/loadmaint.mcgi?load_id=343514" target="_blank">343514</A><input type=\'hidden\' name=\'load_id\' value="343514"></td>'
    + '<td class="dv_cell listable_cell dv_plain dv_cell_carriername" id="DetCell_1_2">88 Transpoort LLC</td></tr>'
    + "</table>199 rows returned in 0.1s. Rows 1 - 100 of 199</body></html>"
)

PAGE_EMPTY = (
    '<html><body><INPUT type="hidden" name="Control__keepTitle" value="VIN Re-decode">'
    '<INPUT type="hidden" value="1" name="Menu__SUBMITTED"><div id="ViewLoc"> No Results Found </div></body></html>'
)

PAGE_SIGNIN = (
    "<html><head><title>Circle | Sign in</title></head><body><form>"
    '<input name="_utf8"><input name="destination"><input name="credential_0">'
    '<input type="password" name="credential_1"></form></body></html>'
)

MENU_JS = """
var menu = [ { "text":"Dispatch", "url":null, "descr":"", "by":26636, "priv":"Dispatch", "submenu": { "id":"sub_Dispatch", "itemdata":[
  { "text":"General Dispatch", "url":null, "descr":"", "priv":"Dispatch", "submenu": { "id":"sub_General Dispatch", "itemdata":[
    { "text":"Engineering Map ", "url":"/backoffice/Search.mcgi?expand_saved=1876&expand_saved_type=priv", "descr":"View available orders in a Grid format", "by":26640, "priv":"Dispatch"},
    { "text":"General Load Build w/  Map", "url":"/backoffice/Search.mcgi?expand_saved=1788&expand_saved_type=priv", "descr":"View available orders in a Grid format", "by":26640, "priv":"Dispatch"}
  ] } },
  { "text":"USPS - Load Build w/Map", "url":"/backoffice/Search.mcgi?expand_saved=1648&expand_saved_type=priv", "descr":"View available orders in a Grid format", "by":26640, "priv":"Dispatch"}
] } },
{ "text":"Reports", "url":null, "submenu": { "id":"sub_Reports", "itemdata":[
  { "text":"Other", "url":"/backoffice/other.mcgi", "descr":"" }
] } } ];
"""


class MapGridTests(unittest.TestCase):
    def setUp(self):
        self.grid = parse_grid(PAGE_MAP)

    def test_title_and_columns(self):
        self.assertEqual(self.grid.title, "General Load Build w/  Map")
        self.assertEqual(
            self.grid.column_keys,
            ["extra", "age_SLL_d_Inventory", "load_id", "rush", "bundle", "origin", "destination", "carriername"],
        )
        self.assertEqual(self.grid.columns[2].label, "Order ID")
        self.assertEqual(self.grid.columns[0].label, "Map")

    def test_rows_and_footer(self):
        self.assertEqual(len(self.grid.rows), 3)
        self.assertEqual((self.grid.row_from, self.grid.row_to, self.grid.total_rows), (1, 3, 3))
        self.assertEqual(self.grid.rows_returned, 3)
        self.assertFalse(self.grid.is_partial)
        self.assertFalse(self.grid.no_results)
        self.assertFalse(self.grid.signin_page)

    def test_cells_are_mapped_by_header_position_not_by_class(self):
        row = self.grid.rows[0]
        self.assertEqual(row.load_id, "344134")
        self.assertEqual(row.link_class, "LinkBundleChild")
        self.assertEqual(row.text("age_SLL_d_Inventory"), "3")
        self.assertEqual(row.text("load_id"), "344134")
        self.assertEqual(row.text("rush"), "No")
        self.assertEqual(row.lines("origin"), ["2109 Fourth Ave S", "Clear Lake, IA"])
        self.assertEqual(row.text("origin"), "2109 Fourth Ave S | Clear Lake, IA")
        self.assertEqual(row.lines("destination"), ["W6331 Wally Wy", "Greenville, WI"])
        self.assertEqual(row.text("carriername"), "Acme & Sons Transport")
        self.assertEqual(row.cells["bundle"].load_links, ["344143"])
        self.assertTrue(self.grid.class_shift)
        self.assertTrue(any("shifted" in w for w in self.grid.warnings))

    def test_load_hash(self):
        load = self.grid.rows[0].load
        self.assertEqual(load["orig_zip"], "50428")
        self.assertEqual(load["dest_zip"], "54942")
        self.assertEqual(load["miles"], "314")
        self.assertEqual(load["carrier_id"], "")
        self.assertEqual(load["id"], "344134")
        self.assertEqual(load["type"], "BundleChild")
        self.assertIn("It's a test", load["popup_data"])
        self.assertIn("\n", load["popup_data"])

    def test_map_points(self):
        row = self.grid.rows[0]
        self.assertAlmostEqual(row.origin.lat, 43.133009)
        self.assertAlmostEqual(row.origin.lon, -93.362922)
        self.assertEqual(row.origin.zip, "50428")
        self.assertEqual(row.origin.city_state, "Clear Lake, IA 50428")
        self.assertEqual(row.origin.load_id, "344134")
        self.assertAlmostEqual(row.dest.lat, 44.2999993523149)
        self.assertEqual(row.dest.zip, "54942")

    def test_sections(self):
        self.assertEqual([r.section for r in self.grid.rows],
                         ["Origin: 2109 FOURTH AVE S", "Origin: 2109 FOURTH AVE S", "Origin: 500 MAIN ST"])

    def test_flat_records(self):
        records = self.grid.records()
        self.assertEqual(len(records), 3)
        first = records[0]
        self.assertNotIn("extra", first)
        self.assertEqual(first["load_id"], "344134")
        self.assertEqual(first["origin"], "2109 Fourth Ave S | Clear Lake, IA")
        self.assertEqual(first["hash_orig_zip"], "50428")
        self.assertNotIn("hash_popup_data", first)
        self.assertAlmostEqual(first["origin_lat"], 43.133009)
        self.assertEqual(first["dest_zip"], "54942")
        self.assertEqual(records[2]["section"], "Origin: 500 MAIN ST")


class PlainGridTests(unittest.TestCase):
    def test_aligned_classes_and_partial_page(self):
        grid = parse_grid(PAGE_PLAIN)
        self.assertEqual(grid.title, "Assigned Carrier Report")
        self.assertEqual(grid.column_keys, ["status", "load_id", "carriername"])
        self.assertFalse(grid.class_shift)
        self.assertEqual(len(grid.rows), 1)
        row = grid.rows[0]
        self.assertEqual(row.load_id, "343514")
        self.assertEqual(row.text("status"), "Dispatched")
        self.assertEqual(row.text("carriername"), "88 Transpoort LLC")
        self.assertIsNone(row.origin)
        self.assertEqual(row.load, {})
        self.assertEqual((grid.row_from, grid.row_to, grid.total_rows), (1, 100, 199))
        self.assertTrue(grid.is_partial)


class EdgeCaseTests(unittest.TestCase):
    def test_no_results(self):
        grid = parse_grid(PAGE_EMPTY)
        self.assertTrue(grid.no_results)
        self.assertFalse(grid.signin_page)
        self.assertEqual(grid.rows, [])
        self.assertEqual(grid.columns, [])
        self.assertEqual(grid.title, "VIN Re-decode")

    def test_signin_page(self):
        self.assertTrue(is_signin_page(PAGE_SIGNIN))
        self.assertTrue(is_signin_page("<html>anything</html>", status=403))
        self.assertFalse(is_signin_page(PAGE_EMPTY))
        self.assertFalse(is_signin_page(PAGE_MAP))
        grid = parse_grid(PAGE_SIGNIN, status=403)
        self.assertTrue(grid.signin_page)
        self.assertTrue(any("sign-in" in w for w in grid.warnings))

    def test_unkeyed_header_columns_take_their_label_as_key(self):
        # The radius view prepends "Origin Dist" / "Dest Dist" / "Total" with no dv_header_ key.
        header = HEADER_PLAIN.replace(
            "</tr>",
            '<th class="dv_cell dv_header oeheader"><span class="dv_header_name">Origin Dist</span></th></tr>',
        )
        page = PAGE_PLAIN.replace(HEADER_PLAIN, header).replace(
            "88 Transpoort LLC</td></tr>",
            '88 Transpoort LLC</td><td class="dv_cell listable_cell dv_number dv_cell_EXTRA" id="DetCell_1_3">12.5</td></tr>',
        )
        grid = parse_grid(page)
        self.assertEqual(grid.column_keys, ["status", "load_id", "carriername", "origin_dist"])
        self.assertEqual(grid.rows[0].text("origin_dist"), "12.5")

    def test_cells_without_header_fall_back_to_class_keys(self):
        page = PAGE_PLAIN.replace('id="det_header_row"', 'id="nope"')
        grid = parse_grid(page)
        self.assertEqual(grid.columns, [])
        self.assertEqual(grid.rows[0].text("status"), "Dispatched")
        self.assertTrue(any("no det_header_row" in w for w in grid.warnings))


class SearchParamsTests(unittest.TestCase):
    def test_minimal(self):
        self.assertEqual(
            build_search_params(SearchParams("1788")),
            {"expand_saved": "1788", "expand_saved_type": "priv"},
        )

    def test_radius_and_dates(self):
        params = build_search_params(
            SearchParams("1788", radius_origin="48326", radius_miles=150,
                         date_field="carrier_assigned", date_window="Last 30 days",
                         per_page="All", extra={"Menu__DispatchRep": "ANTHONY STAVALE"})
        )
        # Radius row 1: the only row CargoTel applies (row 2 is echoed back but ignored).
        self.assertEqual(params["Menu__radius__radiuses"], "1")
        self.assertEqual(params["Menu__radius__fs_radiuses"], "1")
        self.assertEqual(params["Menu__radius__1__orig"], "48326")
        self.assertEqual(params["Menu__radius__1__oradius"], "150")
        self.assertNotIn("Menu__radius__2__orig", params)
        self.assertEqual(params["Menu__newDate"], DATE_FIELDS["carrier_assigned"])
        self.assertEqual(params["Menu__newDateMne"], "Last 30 days")
        self.assertEqual(params["Menu__perPage"], "All")
        self.assertEqual(params["Menu__DispatchRep"], "ANTHONY STAVALE")

    def test_raw_date_code_passes_through(self):
        params = build_search_params(SearchParams("1587", date_field="SLL_d_O"))
        self.assertEqual(params["Menu__newDate"], "SLL_d_O")

    def test_dest_radius(self):
        params = build_search_params(SearchParams("1788", radius_dest="Grapevine, TX", radius_dest_miles=50))
        self.assertEqual(params["Menu__radius__1__dest"], "Grapevine, TX")
        self.assertEqual(params["Menu__radius__1__dradius"], "50")
        self.assertNotIn("Menu__radius__1__orig", params)

    def test_bad_radius(self):
        with self.assertRaises(ValueError):
            build_search_params(SearchParams("1788", radius_origin="48326", radius_miles=100))


RADIUS_ECHO = (
    '<table width="100%" border="1"><tr><th>Radius Searches</th></tr><tr><td>From <a href="" '
    "onclick=\"radius_fill_table('48326 LAKE ANGELUS, MI','', 94838598103592.0, 75.0, 42.66, -83.27, 'red', 948385);\">"
    "48326 (LAKE ANGELUS, MI)</a></td></tr></table>"
)


class RadiusCenterTests(unittest.TestCase):
    def test_center_parsed_and_distances_computed(self):
        grid = parse_grid(PAGE_MAP.replace("<table>", RADIUS_ECHO + "<table>", 1))
        self.assertIsNotNone(grid.radius_center)
        self.assertEqual(grid.radius_center.label, "48326 LAKE ANGELUS, MI")
        self.assertEqual(grid.radius_center.side, "origin")
        self.assertAlmostEqual(grid.radius_center.lat, 42.66)
        self.assertAlmostEqual(grid.radius_center.lon, -83.27)
        record = grid.records()[0]
        # Clear Lake, IA (43.133, -93.363) is about 510 great-circle miles from Lake Angelus, MI.
        self.assertAlmostEqual(record["origin_miles"], 511, delta=3)
        self.assertIsNotNone(record["dest_miles"])
        self.assertEqual(grid.within(600), grid.rows)
        self.assertEqual(grid.within(100), [])
        self.assertEqual(len(grid.within(30, lat=43.133009, lon=-93.362922)), 2)
        self.assertEqual(len(grid.within(30, lat=43.133009, lon=-93.362922, side="dest")), 0)
        self.assertEqual(len(grid.within(30, lat=43.133009, lon=-93.362922, side="either")), 2)
        self.assertIn("radius_center=", grid.summary())

    def test_dest_side_center(self):
        echo = RADIUS_ECHO.replace("'48326 LAKE ANGELUS, MI',''", "'','76051 GRAPEVINE, TX'")
        grid = parse_grid(PAGE_MAP.replace("<table>", echo + "<table>", 1))
        self.assertEqual(grid.radius_center.side, "dest")
        self.assertEqual(grid.radius_center.label, "76051 GRAPEVINE, TX")

    def test_no_center_means_no_miles_and_within_needs_a_point(self):
        grid = parse_grid(PAGE_MAP)
        self.assertIsNone(grid.radius_center)
        self.assertNotIn("origin_miles", grid.records()[0])
        with self.assertRaises(ValueError):
            grid.within(100)


class MenuTests(unittest.TestCase):
    def test_paths_and_saved_ids(self):
        items = parse_menu(MENU_JS)
        by_text = {i.text: i for i in items}
        self.assertEqual(by_text["Engineering Map"].path, ["Dispatch", "General Dispatch"])
        self.assertEqual(by_text["Engineering Map"].saved_id, "1876")
        self.assertEqual(by_text["Engineering Map"].saved_type, "priv")
        self.assertEqual(by_text["General Load Build w/  Map"].saved_id, "1788")
        self.assertEqual(by_text["USPS - Load Build w/Map"].path, ["Dispatch"])
        self.assertEqual(by_text["Other"].path, ["Reports"])
        self.assertIsNone(by_text["Other"].saved_id)
        self.assertEqual(len(items), 4)


if __name__ == "__main__":
    unittest.main()
