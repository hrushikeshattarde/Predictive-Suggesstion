"""
Phase 0 grid parser and fetcher for CargoTel ``Search.mcgi`` saved searches.

CargoTel has no API. Its back-office menu (``/cargotel/menu_js.mcgi``) is a JSON-ish
tree whose leaves are saved searches at ``/backoffice/Search.mcgi?expand_saved=<id>
&expand_saved_type=priv``. ``Search.mcgi`` accepts its own form fields as GET
parameters, so a saved search can be pulled with a server-side radius filter, a date
window and a page size, and the HTML grid it returns can be parsed offline.

What this module knows about the grid (verified against live pages on 2026-10-12):

* The header row is ``<tr id="det_header_row">``. Each ``<th>`` carries the column key in
  its class (``dv_header_<key>``) and its label in ``<span class="dv_header_name">``. The
  first ``<th>`` (``row_number_head``) is the row number. Map-enabled searches add an
  extra ``<th id="det_header_extra">`` ("Map") right after it.
* Data cells are ``<td id="DetCell_<row>_<col>" class="... dv_cell_<key>">``.
  **On map-enabled searches the class key on a data cell is shifted one column from the
  cell's content** (cell 0 carries the map checkboxes but is classed as the first real
  column, cell 1 holds the first real column's value but is classed as the second, and so
  on). Cells are therefore mapped to columns by position against the header row, never
  by their own class. The class is kept only to detect and report the shift.
* On map-enabled searches, cell 0 of every row holds an inline script with
  ``CgtObjectFactory('Load', <id>, {orig_zip:'...', dest_zip:'...', miles:'...', ...})``
  and ``MapPoints[n] = { origin: {lat:..., lon:..., ...}, dest: {...} }``. Both are
  parsed; the latter is where load coordinates come from.
* Rows may be grouped under section header rows (``dv_section_header``) when the search
  is sorted; the section text is attached to each row.
* The footer says ``Rows 1 - 100 of 199`` and ``199 rows returned``. An empty result says
  ``No Results Found`` and has no header row. A stale session is answered with HTTP 403
  and the "Circle | Sign in" page (``credential_0`` / password inputs).
* A radius search is a true great-circle filter on the load's origin (or destination)
  when sent as radius row 1 (see :func:`build_search_params`). The page echoes the point
  the zip resolved to (``radius_fill_table('48326 LAKE ANGELUS, MI', ..., 42.66, -83.27``),
  which :class:`Grid` exposes as ``radius_center``; the "Origin Dist" column it adds is
  left as ``-``, so distances are recomputed locally from ``MapPoints``.

Usage::

    python cargotel_search.py 1788 --radius 48326 --miles 150 --per-page All --out loads.csv
    python cargotel_search.py 1587 --date-field CarrierAdd --window "Last 30 days" --per-page 1000 --out history.csv
    python cargotel_search.py --from-html saved_1788.html --json loads.json
    python cargotel_search.py --menu "General Dispatch"

Auth is the same S3-held ``cgt-browser-session`` cookie that ``pull_load_detail.py`` uses.
Parsing needs only the standard library; fetching needs ``requests`` and ``boto3``.
"""

from __future__ import annotations

import argparse
import bisect
import csv
import html as html_lib
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

__all__ = [
    "BASE_URL",
    "DATE_FIELDS",
    "PER_PAGE_CHOICES",
    "RADIUS_MILES",
    "SEARCH_URL",
    "CargoTelSession",
    "Cell",
    "Column",
    "Grid",
    "GridRow",
    "MapPoint",
    "MenuItem",
    "RadiusCenter",
    "SearchParams",
    "build_search_params",
    "haversine_miles",
    "is_signin_page",
    "parse_grid",
    "parse_menu",
]

BASE_URL = "https://circle.cargotel.com"
SEARCH_URL = f"{BASE_URL}/backoffice/Search.mcgi"
MENU_URL = f"{BASE_URL}/cargotel/menu_js.mcgi"

#: The radius choices CargoTel's search form offers (``Menu__radius__2__oradius``).
RADIUS_MILES = (10, 25, 50, 75, 150, 300)

#: ``Menu__perPage`` choices. ``All`` is accepted literally.
PER_PAGE_CHOICES = ("10", "30", "50", "100", "200", "500", "1000", "10000", "All")

#: Friendly names for the ``Menu__newDate`` codes used most. Any other code is passed
#: through untouched; the form lists 108 of them.
DATE_FIELDS = {
    "carrier_assigned": "CarrierAdd",
    "carrier_unassigned": "CarrierUnass",
    "driver_assigned": "DriverAdd",
    "created": "Created",
    "delivered": "Delivered",
    "delivered_status": "SLL_d_D",
    "dispatched_status": "SLL_d_O",
}

_BROWSER_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "referer": f"{BASE_URL}/exe/menuframe.mcgi",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "upgrade-insecure-requests": "1",
    "user-agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/151.0.0.0 Safari/537.36"
    ),
}


# --------------------------------------------------------------------------- #
# Models                                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class Column:
    """One grid column, in header order. ``index`` is the DetCell column number."""

    index: int
    key: str
    label: str


@dataclass
class Cell:
    """One data cell. ``text`` joins ``lines`` with ``" | "``; ``lines`` are the ``<br>``-
    separated pieces, e.g. ``["2109 Fourth Ave S", "Clear Lake, IA"]``."""

    key: str
    html: str
    text: str
    lines: list[str]
    #: Load ids linked from the cell (``loadmaint.mcgi?load_id=``), e.g. bundle members.
    load_links: list[str]
    #: Key read off the cell's own class, kept for shift detection only.
    class_key: str | None = None


@dataclass
class MapPoint:
    """An origin or destination point out of ``MapPoints[n]``."""

    lat: float | None
    lon: float | None
    zip: str | None
    city_state: str | None
    load_id: str | None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class RadiusCenter:
    """The point CargoTel resolved a radius search's zip or city to, as echoed on the
    result page (``radius_fill_table('48326 LAKE ANGELUS, MI', '', ..., 42.66, -83.27``).
    The miles echoed next to it are a display default (always 75.0), not the radius that
    was applied, so they are not kept."""

    label: str
    lat: float
    lon: float
    side: str  # "origin" or "dest"


def haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in statute miles (same formula as bookmate/geo.py)."""

    import math

    radius = 3958.7613
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(a))


@dataclass
class GridRow:
    index: int
    load_id: str | None
    #: ``LinkIndividual`` or ``LinkBundleChild`` on the load link; bundles are multi-load.
    link_class: str | None
    #: Text of the nearest section header above the row, when the grid is grouped.
    section: str | None
    cells: dict[str, Cell]
    #: The ``CgtObjectFactory('Load', ...)`` hash: orig_zip, dest_zip, orig_state,
    #: dest_state, miles, price, carrier_id, shipper_id, ... (strings, as served).
    load: dict[str, str]
    origin: MapPoint | None
    dest: MapPoint | None

    def text(self, key: str, default: str = "") -> str:
        cell = self.cells.get(key)
        return cell.text if cell is not None else default

    def lines(self, key: str) -> list[str]:
        cell = self.cells.get(key)
        return list(cell.lines) if cell is not None else []

    def miles_from(self, lat: float, lon: float, side: str = "origin") -> float | None:
        """Great-circle miles from a point to this row's origin or destination."""

        point = self.origin if side == "origin" else self.dest
        if point is None or point.lat is None or point.lon is None:
            return None
        return haversine_miles(lat, lon, point.lat, point.lon)

    def flat(self, columns: list[Column], center: RadiusCenter | None = None) -> dict[str, Any]:
        """One flat record for CSV/JSON: grid columns by key, then ``hash_*`` fields from
        the load hash, then origin/dest coordinates, then (given a center) the true
        great-circle miles from it, which CargoTel's own "Origin Dist" column leaves as
        ``-``."""

        record: dict[str, Any] = {
            "row": self.index,
            "load_id": self.load_id,
            "link_class": self.link_class,
            "section": self.section,
        }
        for column in columns:
            if column.key == "extra":
                continue
            record[column.key] = self.text(column.key)
        for key, value in self.load.items():
            if key in ("popup_data", "_CgtClassName"):
                continue
            record[f"hash_{key}"] = value
        for name, point in (("origin", self.origin), ("dest", self.dest)):
            record[f"{name}_lat"] = point.lat if point else None
            record[f"{name}_lon"] = point.lon if point else None
            record[f"{name}_zip"] = point.zip if point else None
            record[f"{name}_city_state"] = point.city_state if point else None
        if center is not None:
            for name in ("origin", "dest"):
                miles = self.miles_from(center.lat, center.lon, name)
                record[f"{name}_miles"] = round(miles, 1) if miles is not None else None
        return record


@dataclass
class Grid:
    title: str | None
    columns: list[Column]
    rows: list[GridRow]
    #: From the footer ``Rows <from> - <to> of <total>``. ``None`` when absent.
    row_from: int | None
    row_to: int | None
    total_rows: int | None
    #: From ``<n> rows returned``.
    rows_returned: int | None
    no_results: bool
    signin_page: bool
    #: True when data-cell classes were found shifted from their content (map searches).
    class_shift: bool
    warnings: list[str] = field(default_factory=list)
    #: The resolved point of a radius search, when the page was produced by one.
    radius_center: RadiusCenter | None = None

    @property
    def is_partial(self) -> bool:
        """True when the page holds fewer rows than the search matched."""

        return bool(self.total_rows and self.row_to and self.row_to < self.total_rows)

    @property
    def column_keys(self) -> list[str]:
        return [c.key for c in self.columns]

    def records(self) -> list[dict[str, Any]]:
        return [row.flat(self.columns, self.radius_center) for row in self.rows]

    def within(
        self,
        miles: float,
        lat: float | None = None,
        lon: float | None = None,
        side: str = "origin",
    ) -> list[GridRow]:
        """Rows whose origin (or ``dest``, or ``either``) lies within ``miles`` of a point.

        Defaults to the page's own radius center. A local check on the parsed
        coordinates, independent of what CargoTel's radius filter did server-side.
        """

        if lat is None or lon is None:
            if self.radius_center is None:
                raise ValueError("no radius center on this page; pass lat and lon")
            lat, lon = self.radius_center.lat, self.radius_center.lon
        sides = ("origin", "dest") if side == "either" else (side,)
        kept: list[GridRow] = []
        for row in self.rows:
            distances = [row.miles_from(lat, lon, s) for s in sides]
            if any(d is not None and d <= miles for d in distances):
                kept.append(row)
        return kept

    def summary(self) -> str:
        coords = sum(1 for r in self.rows if r.origin and r.origin.lat is not None)
        zips = sum(1 for r in self.rows if r.load.get("orig_zip"))
        parts = [
            f"title={self.title!r}",
            f"rows={len(self.rows)}",
            f"page={self.row_from}-{self.row_to} of {self.total_rows}",
            f"with_coords={coords}",
            f"with_orig_zip={zips}",
            f"columns={len(self.columns)}",
        ]
        if self.radius_center:
            c = self.radius_center
            parts.append(f"radius_center={c.label!r} ({c.lat}, {c.lon}) [{c.side}]")
        if self.class_shift:
            parts.append("class_shift=yes")
        if self.no_results:
            parts.append("no_results=yes")
        if self.signin_page:
            parts.append("SIGN-IN PAGE")
        return " ".join(parts)


@dataclass
class MenuItem:
    path: list[str]
    text: str
    url: str
    saved_id: str | None
    saved_type: str | None
    descr: str = ""


@dataclass
class SearchParams:
    """What to ask ``Search.mcgi`` for. Everything except ``saved_id`` is optional."""

    saved_id: str
    saved_type: str = "priv"
    radius_origin: str | None = None
    radius_miles: int | None = None
    radius_dest: str | None = None
    radius_dest_miles: int | None = None
    date_field: str | None = None
    date_window: str | None = None
    per_page: str | None = None
    extra: dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Parsing                                                                      #
# --------------------------------------------------------------------------- #

_HEADER_ROW_RE = re.compile(r'<tr[^>]*\bid="det_header_row"[^>]*>(.*?)</tr>', re.S | re.I)
_TH_RE = re.compile(r"<th\b([^>]*)>(.*?)</th>", re.S | re.I)
_HEADER_KEY_RE = re.compile(r"\bdv_header_([A-Za-z0-9_]+)")
_HEADER_NAME_RE = re.compile(r'<span[^>]*class="dv_header_name"[^>]*>(.*?)</span>', re.S | re.I)
_CELL_OPEN_RE = re.compile(r'<td\b[^>]*\bid="DetCell_(\d+)_(\d+)"[^>]*>', re.I)
_TD_TOKEN_RE = re.compile(r"<td\b|</td\s*>", re.I)
_CELL_CLASS_KEY_RE = re.compile(r"\bdv_cell_([A-Za-z0-9_]+)")
_SECTION_RE = re.compile(
    r'<tr[^>]*\bclass="[^"]*dv_section_header[^"]*"[^>]*>(.*?)</tr>', re.S | re.I
)
_SCRIPT_RE = re.compile(r"<script\b.*?</script>", re.S | re.I)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_LOAD_LINK_RE = re.compile(r"loadmaint\.mcgi\?load_id=(\d+)")
_MAPID_RE = re.compile(r'name="MapId_(\d+)"[^>]*class="(Link\w+)"')
_HIDDEN_LOAD_ID_RE = re.compile(r"""name=['"]load_id['"][^>]*value=['"](\d+)['"]""")
_HASH_RE = re.compile(r"CgtObjectFactory\(\s*'Load'\s*,\s*(\d+)\s*,\s*\{(.*?)\}\s*\)\s*\)", re.S)
_HASH_PAIR_RE = re.compile(r"([A-Za-z_]\w*)\s*:\s*'((?:[^'\\]|\\.)*)'", re.S)
_MAPPOINTS_RE = re.compile(r"MapPoints\[(\d+)\]\s*=\s*\{(.*?)\};", re.S)
_MAP_SIDE_RE = re.compile(r"\b(origin|dest)\s*:\s*\{(.*?)\}", re.S)
_JS_PAIR_RE = re.compile(
    r"""([A-Za-z_]\w*)\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)'|(-?\d+(?:\.\d+)?))"""
)
_JS_ESCAPE_RE = re.compile(r"\\(.)", re.S)
_FOOTER_RE = re.compile(r"Rows\s+(\d+)\s*-\s*(\d+)\s+of\s+(\d+)")
_RETURNED_RE = re.compile(r"(\d+)\s+rows returned")
_TITLE_RE = (
    re.compile(r'name="Control__keepTitle"[^>]*value="([^"]*)"'),
    re.compile(r'value="([^"]*)"[^>]*name="Control__keepTitle"'),
)
_PASSWORD_RE = re.compile(r"""type=["']?password""", re.I)
_RADIUS_CENTER_RE = re.compile(
    r"radius_fill_table\('([^']*)',\s*'([^']*)',\s*[\d.]+,\s*[\d.]+,\s*(-?\d+(?:\.\d+)?),\s*(-?\d+(?:\.\d+)?)"
)


def _parse_radius_center(html: str) -> RadiusCenter | None:
    match = _RADIUS_CENTER_RE.search(html)
    if match is None:
        return None
    origin_label, dest_label, lat, lon = match.groups()
    label, side = (origin_label, "origin") if origin_label else (dest_label, "dest")
    return RadiusCenter(label=label, lat=float(lat), lon=float(lon), side=side)


def is_signin_page(html: str, status: int | None = None) -> bool:
    """True when CargoTel answered with its sign-in page instead of a search result.

    A stale ``cgt-browser-session`` cookie is answered with HTTP 403 and the "Circle |
    Sign in" page, which carries ``credential_0`` / a password field and none of the
    search form's ``Menu__`` fields. Checked before parsing so a dead session never
    reads as an empty search.
    """

    if status == 403:
        return True
    if "Menu__SUBMITTED" in html or "det_header_row" in html:
        return False
    return "credential_0" in html or bool(_PASSWORD_RE.search(html))


def _clean_text(fragment: str) -> list[str]:
    """Visible text of an HTML fragment as ``<br>``-separated lines."""

    fragment = _SCRIPT_RE.sub(" ", fragment)
    fragment = _BR_RE.sub("\n", fragment)
    fragment = _TAG_RE.sub(" ", fragment)
    fragment = html_lib.unescape(fragment).replace("\xa0", " ")
    lines = [" ".join(line.split()) for line in fragment.split("\n")]
    return [line for line in lines if line]


def _js_unescape(value: str) -> str:
    return _JS_ESCAPE_RE.sub(
        lambda m: {"n": "\n", "r": "\r", "t": "\t"}.get(m.group(1), m.group(1)), value
    )


def _parse_js_pairs(body: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, dq, sq, num in _JS_PAIR_RE.findall(body):
        if num:
            out[key] = float(num) if "." in num else int(num)
        else:
            out[key] = _js_unescape(dq if dq else sq)
    return out


def _parse_map_point(body: str) -> MapPoint:
    raw = _parse_js_pairs(body)
    lat, lon = raw.get("lat"), raw.get("lon")
    load_id = raw.get("load_id")
    return MapPoint(
        lat=float(lat) if isinstance(lat, (int, float)) else None,
        lon=float(lon) if isinstance(lon, (int, float)) else None,
        zip=str(raw["tzip"]) if raw.get("tzip") not in (None, "") else None,
        city_state=str(raw["cs"]) if raw.get("cs") else None,
        load_id=str(load_id) if load_id is not None else None,
        raw=raw,
    )


def _parse_header(html: str) -> list[Column]:
    match = _HEADER_ROW_RE.search(html)
    if match is None:
        return []
    columns: list[Column] = []
    index = 0
    for attrs, inner in _TH_RE.findall(match.group(1)):
        if 'id="row_number_head"' in attrs:
            continue
        name_match = _HEADER_NAME_RE.search(inner)
        label = " ".join(_clean_text(name_match.group(1) if name_match else inner))
        key_match = _HEADER_KEY_RE.search(attrs)
        if key_match:
            key = key_match.group(1)
        elif "det_header_extra" in attrs:
            key = "extra"
        else:
            # Columns CargoTel adds on the fly carry no dv_header_ key: the radius view
            # prepends "Origin Dist", "Dest Dist" and "Total". Key them off the label.
            key = _slug(label) or f"col{index}"
        columns.append(Column(index=index, key=key, label=label))
        index += 1
    return columns


def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", label).strip("_").lower()


def _iter_cells(html: str) -> Iterator[tuple[int, int, int, str, str]]:
    """Yield ``(row, col, position, open_tag, inner_html)`` for every DetCell.

    Walks to the matching ``</td>`` counting nested ``<td>`` so a cell holding a table
    stays one cell. CargoTel does not nest today, but the markup is old and loose.
    """

    for match in _CELL_OPEN_RE.finditer(html):
        depth = 0
        pos = match.end()
        end = None
        for token in _TD_TOKEN_RE.finditer(html, pos):
            if token.group(0).lower().startswith("</"):
                if depth == 0:
                    end = token.start()
                    break
                depth -= 1
            else:
                depth += 1
        if end is None:
            end = len(html)
        yield int(match.group(1)), int(match.group(2)), match.start(), match.group(0), html[pos:end]


def _sections(html: str) -> tuple[list[int], list[str]]:
    positions: list[int] = []
    texts: list[str] = []
    for match in _SECTION_RE.finditer(html):
        positions.append(match.start())
        texts.append(" ".join(_clean_text(match.group(1))))
    return positions, texts


def parse_grid(html: str, status: int | None = None) -> Grid:
    """Parse a ``Search.mcgi`` result page into a :class:`Grid`.

    Pure function of the HTML (plus the optional HTTP status for sign-in detection), so
    it can be unit-tested against saved pages.
    """

    warnings: list[str] = []
    title = None
    for pattern in _TITLE_RE:
        m = pattern.search(html)
        if m:
            title = html_lib.unescape(m.group(1)).strip() or None
            break

    signin = is_signin_page(html, status)
    no_results = "No Results Found" in html
    columns = _parse_header(html)
    by_index = {c.index: c for c in columns}

    footer = _FOOTER_RE.search(html)
    row_from = int(footer.group(1)) if footer else None
    row_to = int(footer.group(2)) if footer else None
    total = int(footer.group(3)) if footer else None
    returned = _RETURNED_RE.search(html)
    rows_returned = int(returned.group(1)) if returned else None

    section_pos, section_txt = _sections(html)

    rows_raw: dict[int, list[tuple[int, int, str, str]]] = {}
    for row, col, pos, open_tag, inner in _iter_cells(html):
        rows_raw.setdefault(row, []).append((col, pos, open_tag, inner))

    if rows_raw and not columns:
        warnings.append(
            "no det_header_row found; columns taken from cell classes, which are shifted "
            "by one on map-enabled searches"
        )

    rows: list[GridRow] = []
    shifted = aligned = 0
    for row_index in sorted(rows_raw):
        cells: dict[str, Cell] = {}
        load_hash: dict[str, str] = {}
        origin = dest = None
        load_id: str | None = None
        link_class: str | None = None
        first_pos: int | None = None
        for col, pos, open_tag, inner in sorted(rows_raw[row_index]):
            if first_pos is None:
                first_pos = pos
            class_match = _CELL_CLASS_KEY_RE.search(open_tag)
            class_key = class_match.group(1) if class_match else None
            column = by_index.get(col)
            if column is not None:
                key = column.key
                if class_key is not None and key != "extra":
                    if class_key == key:
                        aligned += 1
                    else:
                        shifted += 1
            else:
                key = class_key or f"col{col}"

            lines = _clean_text(inner)
            cell = Cell(
                key=key,
                html=inner,
                text=" | ".join(lines),
                lines=lines,
                load_links=list(dict.fromkeys(_LOAD_LINK_RE.findall(inner))),
                class_key=class_key,
            )
            cells[key] = cell

            mapid = _MAPID_RE.search(inner)
            if mapid and load_id is None:
                load_id, link_class = mapid.group(1), mapid.group(2)
            if "CgtObjectFactory" in inner:
                hash_match = _HASH_RE.search(inner)
                if hash_match:
                    load_hash = {k: _js_unescape(v) for k, v in _HASH_PAIR_RE.findall(hash_match.group(2))}
                    load_hash.setdefault("id", hash_match.group(1))
            if "MapPoints[" in inner:
                mp = _MAPPOINTS_RE.search(inner)
                if mp:
                    for side, body in _MAP_SIDE_RE.findall(mp.group(2)):
                        point = _parse_map_point(body)
                        if side == "origin":
                            origin = point
                        else:
                            dest = point

        if load_id is None:
            hidden = _HIDDEN_LOAD_ID_RE.search("".join(c.html for c in cells.values()))
            if hidden:
                load_id = hidden.group(1)
        if load_id is None and load_hash.get("id"):
            load_id = load_hash["id"]
        if load_id is None and "load_id" in cells and cells["load_id"].load_links:
            load_id = cells["load_id"].load_links[0]

        section = None
        if section_pos and first_pos is not None:
            k = bisect.bisect_right(section_pos, first_pos) - 1
            if k >= 0:
                section = section_txt[k]

        rows.append(
            GridRow(
                index=row_index,
                load_id=load_id,
                link_class=link_class,
                section=section,
                cells=cells,
                load=load_hash,
                origin=origin,
                dest=dest,
            )
        )

    class_shift = shifted > aligned
    if class_shift:
        warnings.append(
            f"data-cell classes are shifted from their content ({shifted} shifted vs "
            f"{aligned} aligned); cells were mapped by header position"
        )
    if signin:
        warnings.append("this is the CargoTel sign-in page; the session cookie is stale")
    if rows and total is not None and len(rows) != (row_to or 0) - (row_from or 1) + 1:
        warnings.append(
            f"parsed {len(rows)} rows but the footer says rows {row_from}-{row_to}"
        )

    return Grid(
        title=title,
        columns=columns,
        rows=rows,
        row_from=row_from,
        row_to=row_to,
        total_rows=total,
        rows_returned=rows_returned,
        no_results=no_results,
        signin_page=signin,
        class_shift=class_shift,
        warnings=warnings,
        radius_center=_parse_radius_center(html),
    )


# --------------------------------------------------------------------------- #
# Menu                                                                         #
# --------------------------------------------------------------------------- #

_MENU_TOKEN_RE = re.compile(
    r'\{|\}|"text"\s*:\s*"((?:[^"\\]|\\.)*)"(?:\s*,\s*"url"\s*:\s*(null|"(?:[^"\\]|\\.)*"))?'
    r'(?:\s*,\s*"descr"\s*:\s*"((?:[^"\\]|\\.)*)")?'
)
_SAVED_RE = re.compile(r"expand_saved=(\d+)&(?:amp;)?expand_saved_type=(\w+)")


def parse_menu(menu_js: str) -> list[MenuItem]:
    """Flatten ``menu_js.mcgi`` into leaf items with their menu path.

    The file is JSON-ish JavaScript. Rather than trusting it to be valid JSON, this walks
    braces and ``"text"`` tokens: an item's path is the ``text`` of every enclosing item.
    """

    items: list[MenuItem] = []
    depth = 0
    stack: dict[int, str] = {}
    for match in _MENU_TOKEN_RE.finditer(menu_js):
        token = match.group(0)
        if token == "{":
            depth += 1
            continue
        if token == "}":
            for d in [d for d in stack if d >= depth]:
                del stack[d]
            depth -= 1
            continue
        text = _js_unescape(match.group(1)).strip()
        url = match.group(2)
        descr = _js_unescape(match.group(3) or "")
        stack[depth] = text
        if url and url != "null":
            url = _js_unescape(url.strip('"'))
            saved = _SAVED_RE.search(url)
            path = [stack[d] for d in sorted(stack) if d < depth]
            items.append(
                MenuItem(
                    path=path,
                    text=text,
                    url=url,
                    saved_id=saved.group(1) if saved else None,
                    saved_type=saved.group(2) if saved else None,
                    descr=descr,
                )
            )
    return items


# --------------------------------------------------------------------------- #
# Fetching                                                                     #
# --------------------------------------------------------------------------- #


def build_search_params(spec: SearchParams) -> dict[str, str]:
    """The GET parameters for one ``Search.mcgi`` request.

    Radius needs four fields together: the checkbox that enables the radius block, its
    ``fs_`` companion, the origin (or destination), and the miles. The fields are numbered
    by radius row, and **only row 1 is applied**: the form also renders a row 2, and the
    same fields sent as row 2 are echoed back but the search then runs as if 300 miles had
    been asked (observed 2026-10-12: 10, 75, 150 and 300 on row 2 all returned the same 138
    rows; on row 1, 10 returned 0 rows and 300 returned 138, matching the true distances).

    ``Menu__newDate`` is the date *field* and ``Menu__newDateMne`` the window.
    """

    params: dict[str, str] = {
        "expand_saved": str(spec.saved_id),
        "expand_saved_type": spec.saved_type,
    }
    if spec.radius_origin or spec.radius_dest:
        params["Menu__radius__radiuses"] = "1"
        params["Menu__radius__fs_radiuses"] = "1"
        if spec.radius_origin:
            miles = spec.radius_miles or 150
            if miles not in RADIUS_MILES:
                raise ValueError(f"radius miles must be one of {RADIUS_MILES}, got {miles}")
            params["Menu__radius__1__orig"] = spec.radius_origin
            params["Menu__radius__1__oradius"] = str(miles)
        if spec.radius_dest:
            miles = spec.radius_dest_miles or 150
            if miles not in RADIUS_MILES:
                raise ValueError(f"radius miles must be one of {RADIUS_MILES}, got {miles}")
            params["Menu__radius__1__dest"] = spec.radius_dest
            params["Menu__radius__1__dradius"] = str(miles)
    if spec.date_field:
        params["Menu__newDate"] = DATE_FIELDS.get(spec.date_field, spec.date_field)
        if spec.date_window:
            params["Menu__newDateMne"] = spec.date_window
    if spec.per_page:
        params["Menu__perPage"] = str(spec.per_page)
    params.update(spec.extra)
    return params


def _s3_cookie() -> str:
    """The ``cgt-browser-session`` value, read the same way ``pull_load_detail.py`` does."""

    import os

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from pull_load_detail import fetch_json_from_s3  # noqa: WPS433 (local import by design)

    data = fetch_json_from_s3()
    if not data:
        raise RuntimeError("CargoTel session cookie could not be read from S3")
    return data[0]["value"]


class CargoTelSession:
    """Fetches CargoTel pages with the shared session cookie, politely.

    * one cookie read per session, refreshed once when a sign-in page comes back;
    * at least ``min_interval`` seconds between requests (CargoTel pages are 3 to 5 MB
      and the server is shared with every dispatcher);
    * ``requests`` is imported lazily so the parser stays importable without it.
    """

    def __init__(
        self,
        cookie_source: Callable[[], str] | None = None,
        min_interval: float = 1.0,
        timeout: float = 90.0,
    ) -> None:
        self._cookie_source = cookie_source or _s3_cookie
        self._cookie: str | None = None
        self._min_interval = min_interval
        self._timeout = timeout
        self._last_request = 0.0

    def cookie(self, refresh: bool = False) -> str:
        if self._cookie is None or refresh:
            self._cookie = self._cookie_source()
        return self._cookie

    def get(self, url: str, params: dict[str, str] | None = None) -> tuple[int, str]:
        import requests

        wait = self._min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        headers = {**_BROWSER_HEADERS, "cookie": f"cgt-browser-session={self.cookie()}"}
        response = requests.get(url, headers=headers, params=params, timeout=self._timeout)
        self._last_request = time.monotonic()
        return response.status_code, response.text

    def fetch_search_html(self, spec: SearchParams) -> tuple[int, str, dict[str, str]]:
        """GET one saved search. Retries once with a fresh cookie on a sign-in page."""

        params = build_search_params(spec)
        status, html = self.get(SEARCH_URL, params)
        if is_signin_page(html, status):
            self.cookie(refresh=True)
            status, html = self.get(SEARCH_URL, params)
        return status, html, params

    def fetch_search(self, spec: SearchParams) -> Grid:
        status, html, _ = self.fetch_search_html(spec)
        if status != 200:
            raise RuntimeError(f"CargoTel Search.mcgi returned HTTP {status} for saved search {spec.saved_id}")
        grid = parse_grid(html, status)
        if grid.signin_page:
            raise RuntimeError(
                "CargoTel answered with the sign-in page twice; the S3 session cookie is stale"
            )
        return grid

    def fetch_menu(self) -> list[MenuItem]:
        status, text = self.get(MENU_URL)
        if status != 200 or is_signin_page(text, status):
            raise RuntimeError(f"CargoTel menu_js.mcgi returned HTTP {status} (sign-in page: {is_signin_page(text, status)})")
        return parse_menu(text)


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


def _write_csv(path: str, records: list[dict[str, Any]]) -> None:
    keys: list[str] = []
    for record in records:
        for key in record:
            if key not in keys:
                keys.append(key)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(records)


def _write_json(path: str, grid: Grid) -> None:
    payload = {
        "title": grid.title,
        "columns": [{"index": c.index, "key": c.key, "label": c.label} for c in grid.columns],
        "row_from": grid.row_from,
        "row_to": grid.row_to,
        "total_rows": grid.total_rows,
        "rows_returned": grid.rows_returned,
        "class_shift": grid.class_shift,
        "warnings": grid.warnings,
        "rows": grid.records(),
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Pull and parse a CargoTel Search.mcgi saved search.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  cargotel_search.py 1788 --radius 48326 --miles 150 --per-page All --out loads.csv\n"
            '  cargotel_search.py 1587 --date-field CarrierAdd --window "Last 30 days" --per-page 1000 --out hist.csv\n'
            "  cargotel_search.py --from-html saved_1788.html --json loads.json\n"
            '  cargotel_search.py --menu "General Dispatch"'
        ),
    )
    parser.add_argument("saved_id", nargs="?", help="saved search id (from --menu)")
    parser.add_argument("--saved-type", default="priv", help="expand_saved_type (default priv)")
    parser.add_argument("--from-html", metavar="FILE", help="parse a saved page instead of fetching")
    parser.add_argument("--menu", nargs="?", const="", metavar="FILTER", help="list saved searches whose menu path contains FILTER")
    parser.add_argument("--radius", metavar="ORIGIN", help="origin zip or 'City, ST' for the radius filter")
    parser.add_argument("--miles", type=int, choices=RADIUS_MILES, default=150, help="origin radius in miles (default 150)")
    parser.add_argument("--dest-radius", metavar="DEST", help="destination zip or 'City, ST' for a destination radius")
    parser.add_argument("--dest-miles", type=int, choices=RADIUS_MILES, default=150)
    parser.add_argument("--date-field", help=f"Menu__newDate code or one of {', '.join(DATE_FIELDS)}")
    parser.add_argument("--window", help='Menu__newDateMne window, e.g. "Last 30 days", "Last 12 Months"')
    parser.add_argument("--per-page", help=f"Menu__perPage: one of {', '.join(PER_PAGE_CHOICES)}")
    parser.add_argument("--param", action="append", default=[], metavar="KEY=VALUE", help="extra GET parameter (repeatable)")
    parser.add_argument("--local-miles", type=float, metavar="MILES", help="after parsing, keep only rows within MILES (great-circle) of the radius center or --near")
    parser.add_argument("--near", metavar="LAT,LON", help="center for --local-miles when the page has no radius center")
    parser.add_argument("--side", choices=("origin", "dest", "either"), default="origin", help="which end --local-miles checks (default origin)")
    parser.add_argument("--out", metavar="CSV", help="write flat rows to CSV")
    parser.add_argument("--json", metavar="FILE", help="write the parsed grid to JSON")
    parser.add_argument("--save-html", metavar="FILE", help="archive the fetched HTML")
    parser.add_argument("--show", type=int, default=3, metavar="N", help="print the first N rows (default 3)")
    args = parser.parse_args(argv)

    if args.menu is not None:
        session = CargoTelSession()
        items = session.fetch_menu()
        needle = args.menu.lower()
        shown = 0
        for item in items:
            path = " > ".join(item.path + [item.text])
            if needle and needle not in path.lower():
                continue
            shown += 1
            print(f"{item.saved_id or '-':>6} {item.saved_type or '':8s} {path}")
        print(f"{shown} saved searches", file=sys.stderr)
        return 0

    if args.from_html:
        with open(args.from_html, encoding="utf-8", errors="ignore") as handle:
            html = handle.read()
        grid = parse_grid(html)
    else:
        if not args.saved_id:
            parser.error("saved_id is required unless --from-html or --menu is given")
        if args.per_page and args.per_page not in PER_PAGE_CHOICES:
            parser.error(f"--per-page must be one of {', '.join(PER_PAGE_CHOICES)}")
        extra: dict[str, str] = {}
        for item in args.param:
            if "=" not in item:
                parser.error(f"--param expects KEY=VALUE, got {item!r}")
            key, value = item.split("=", 1)
            extra[key] = value
        spec = SearchParams(
            saved_id=args.saved_id,
            saved_type=args.saved_type,
            radius_origin=args.radius,
            radius_miles=args.miles if args.radius else None,
            radius_dest=args.dest_radius,
            radius_dest_miles=args.dest_miles if args.dest_radius else None,
            date_field=args.date_field,
            date_window=args.window,
            per_page=args.per_page,
            extra=extra,
        )
        session = CargoTelSession()
        status, html, params = session.fetch_search_html(spec)
        print(f"GET Search.mcgi {params} -> HTTP {status}, {len(html)} chars", file=sys.stderr)
        if args.save_html:
            with open(args.save_html, "w", encoding="utf-8") as handle:
                handle.write(html)
        grid = parse_grid(html, status)

    if args.local_miles is not None:
        lat = lon = None
        if args.near:
            try:
                lat, lon = (float(part) for part in args.near.split(","))
            except ValueError:
                parser.error("--near expects LAT,LON")
        try:
            kept = grid.within(args.local_miles, lat, lon, args.side)
        except ValueError as exc:
            parser.error(str(exc))
        print(f"local filter: {len(kept)} of {len(grid.rows)} rows within {args.local_miles:g} mi ({args.side})", file=sys.stderr)
        grid.rows = kept

    print(grid.summary())
    for warning in grid.warnings:
        print(f"warning: {warning}", file=sys.stderr)
    if grid.columns:
        print("columns: " + ", ".join(f"{c.key} ({c.label})" for c in grid.columns))
    for row in grid.rows[: max(args.show, 0)]:
        shown = {k: v for k, v in row.flat(grid.columns).items() if v not in (None, "")}
        print(json.dumps(shown, default=str))

    if args.out:
        _write_csv(args.out, grid.records())
        print(f"wrote {len(grid.rows)} rows to {args.out}", file=sys.stderr)
    if args.json:
        _write_json(args.json, grid)
        print(f"wrote {args.json}", file=sys.stderr)

    if grid.signin_page:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
