"""Offline tests for the duplicate-order check: the window filter, the grouping and the
email. Run with ``python -m pytest duplicate_check/tests`` (or ``python -m unittest``)."""

from __future__ import annotations

import datetime as dt
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import duplicate_check as dc  # noqa: E402

NOW = dt.datetime(2026, 10, 19, 8, 0)
CHELSEA = "Cpg Cc - Cpg | Chelsea, MI"
AURORA = "Sgs Environmental Testing Cntr | Aurora, CO"


def order(load_id: str, vin: str, origin: str, dest: str, *, delivered: str = "", status: str = "Delivered",
          created: str = "10/12/2026 09:00", carrier: str = "Kc Auto Transport Inc", ref: str = "2500653") -> dict:
    return {
        "load_id": load_id, "Vins17": vin, "section": f"Vins: {vin}", "origin": origin, "destination": dest,
        "deliver": delivered, "status": status, "LONG_Created": created, "act_pickup": "", "carriername": carrier,
        "cons_ref": ref, "bundleparent": "9" + load_id, "originzip": "48118", "destinationzip": "80011", "sales_rep1": "JUSTICE CROMWELL",
    }


class WindowTests(unittest.TestCase):
    def test_not_delivered_is_always_kept(self):
        self.assertTrue(dc.in_window(order("1", "V", CHELSEA, AURORA, status="Dispatched"), NOW, 7))

    def test_delivered_inside_and_outside_the_window(self):
        self.assertTrue(dc.in_window(order("1", "V", CHELSEA, AURORA, delivered="10/14/2026 11:00"), NOW, 7))
        self.assertFalse(dc.in_window(order("1", "V", CHELSEA, AURORA, delivered="10/11/2026 11:00"), NOW, 7))

    def test_zero_days_keeps_everything(self):
        self.assertTrue(dc.in_window(order("1", "V", CHELSEA, AURORA, delivered="01/01/2026 00:00"), NOW, 0))

    def test_date_only_values_parse(self):
        self.assertEqual(dc.parse_date("10/14/2026"), dt.datetime(2026, 10, 14))
        self.assertIsNone(dc.parse_date("-"))


class GroupingTests(unittest.TestCase):
    def test_same_lane_twice_is_a_duplicate(self):
        rows = dc.find_duplicates([order("1", "VIN1", CHELSEA, AURORA), order("2", "VIN1", CHELSEA, AURORA, ref="2516580")])
        self.assertEqual([r["order_id"] for r in rows], ["1", "2"])
        self.assertEqual({r["match"] for r in rows}, {"same facility"})
        self.assertEqual(rows[0]["load_id"], "91")

    def test_out_and_back_is_not_a_duplicate(self):
        rows = dc.find_duplicates([order("1", "VIN1", CHELSEA, AURORA), order("2", "VIN1", AURORA, CHELSEA)])
        self.assertEqual(rows, [])

    def test_same_city_different_facility_is_flagged_separately(self):
        a = order("1", "VIN1", "Vehicle Control Services | Auburn Hills, MI", "Arizona Proving Grounds | Yucca, AZ")
        b = order("2", "VIN1", "Ctc Cc - Ctc | Auburn Hills, MI", "Apg Cc | Yucca, AZ")
        rows = dc.find_duplicates([a, b])
        self.assertEqual({r["match"] for r in rows}, {"same city"})
        self.assertEqual(len(rows), 2)

    def test_one_order_per_vin_is_ignored_and_same_row_twice_counts_once(self):
        rows = dc.find_duplicates([order("1", "VIN1", CHELSEA, AURORA), order("1", "VIN1", CHELSEA, AURORA)])
        self.assertEqual(rows, [])

    def test_vin_falls_back_to_the_section_header(self):
        a, b = order("1", "VIN1", CHELSEA, AURORA), order("2", "VIN1", CHELSEA, AURORA)
        a["Vins17"] = b["Vins17"] = ""
        self.assertEqual(len(dc.find_duplicates([a, b])), 2)

    def test_lane_match_ignores_case_and_spacing(self):
        rows = dc.find_duplicates([order("1", "VIN1", CHELSEA, AURORA), order("2", "VIN1", CHELSEA.upper(), "  " + AURORA + " ")])
        self.assertEqual(len(rows), 2)


class EmailTests(unittest.TestCase):
    def rows(self):
        return dc.find_duplicates([
            order("321645", "1C4PJXDR7VW550006", CHELSEA, AURORA, delivered="08/26/2026 15:00"),
            order("335140", "1C4PJXDR7VW550006", CHELSEA, AURORA, delivered="09/28/2026 00:00", carrier="Vb Auto Group Inc", ref="2516580"),
            order("1", "1C4UJ8GV3TS150269", "Vehicle Control Services | Auburn Hills, MI", "Arizona Proving Grounds | Yucca, AZ"),
            order("2", "1C4UJ8GV3TS150269", "Ctc Cc - Ctc | Auburn Hills, MI", "Apg Cc | Yucca, AZ"),
        ])

    def test_email_lists_both_tables_and_links_orders(self):
        email = dc.compose(self.rows(), to=["justice.cromwell@circledelivers.com"], cc=[], now=NOW, days=7,
                           total_orders=120, total_vins=118)
        self.assertEqual(email.to, ["justice.cromwell@circledelivers.com"])
        self.assertIn("2 VINs with more than one order on the same lane (last 7 days)", email.subject)
        self.assertTrue(email.text.startswith("Hi Justice,"))
        self.assertIn("SAME ORIGIN AND DESTINATION (FACILITY AND CITY) (1 VIN)", email.text)
        self.assertIn("SAME CITIES, DIFFERENT FACILITY NAMES - PLEASE REVIEW (1 VIN)", email.text)
        self.assertIn("order 321645 (load 9321645)", email.text)
        self.assertIn(dc.ORDER_URL + "335140", email.html)
        self.assertIn("1C4PJXDR7VW550006", email.html)
        self.assertIn("every Monday at 8:00 AM ET", email.text)
        self.assertEqual(email.load_ids, ["1", "2", "321645", "335140"])

    def test_empty_run_still_writes_an_all_clear(self):
        email = dc.compose([], to=["justice.cromwell@circledelivers.com"], cc=["x@circledelivers.com"], now=NOW, days=7,
                           total_orders=120, total_vins=118)
        self.assertIn("no duplicate orders found", email.subject)
        self.assertIn("shows no VIN on more than one order number", email.text)
        self.assertEqual(email.cc, ["x@circledelivers.com"])
        self.assertNotIn("<table", email.html)


class CliTests(unittest.TestCase):
    def test_offline_run_writes_csv_and_outbox(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            grid = os.path.join(tmp, "grid.json")
            with open(grid, "w", encoding="utf-8") as fh:
                json.dump({"window": "Last 90 days", "rows": [
                    order("1", "VIN1", CHELSEA, AURORA, delivered="10/15/2026 09:00"),
                    order("2", "VIN1", CHELSEA, AURORA, status="Dispatched"),
                    order("3", "VIN2", CHELSEA, AURORA, delivered="09/01/2026 09:00"),  # outside the 7 days
                    order("4", "VIN2", CHELSEA, AURORA, delivered="09/02/2026 09:00"),
                ]}, fh)
            csv_path = os.path.join(tmp, "out.csv")
            outbox = os.path.join(tmp, "outbox")
            rc = dc.main(["--from-json", grid, "--out", csv_path, "--outbox", outbox, "--now", "2026-10-19T08:00",
                          "--sent-log", os.path.join(tmp, "log.jsonl")])
            self.assertEqual(rc, 0)
            with open(csv_path, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            self.assertEqual(len(lines), 3)  # header + VIN1's two orders; VIN2 fell outside the window
            self.assertTrue(all("VIN1" in line for line in lines[1:]))
            files = os.listdir(outbox)
            self.assertEqual(len(files), 1)
            with open(os.path.join(outbox, files[0]), encoding="utf-8") as fh:
                body = fh.read()
            self.assertIn("To: justice.cromwell@circledelivers.com", body)
            self.assertIn("From: Circle Logistics <vehicles@circledelivers.com>", body)
            self.assertIn("1 VIN with more than one order", body)

    def test_days_zero_reports_the_whole_window_and_no_email(self):
        import json
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            grid = os.path.join(tmp, "grid.json")
            with open(grid, "w", encoding="utf-8") as fh:
                json.dump({"window": "Last 90 days", "rows": [
                    order("3", "VIN2", CHELSEA, AURORA, delivered="09/01/2026 09:00"),
                    order("4", "VIN2", CHELSEA, AURORA, delivered="09/02/2026 09:00"),
                ]}, fh)
            csv_path = os.path.join(tmp, "out.csv")
            outbox = os.path.join(tmp, "outbox")
            rc = dc.main(["--from-json", grid, "--out", csv_path, "--outbox", outbox, "--now", "2026-10-19T08:00",
                          "--days", "0", "--no-email", "--sent-log", os.path.join(tmp, "log.jsonl")])
            self.assertEqual(rc, 0)
            with open(csv_path, encoding="utf-8") as fh:
                self.assertEqual(len(fh.read().splitlines()), 3)
            self.assertFalse(os.path.exists(outbox))


if __name__ == "__main__":
    unittest.main()
