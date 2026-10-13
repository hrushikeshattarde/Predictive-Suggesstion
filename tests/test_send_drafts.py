"""Unit tests for send_drafts.py (``python -m unittest``). No network and no SES: drafts
are built in a temp folder the way dispatch_watcher writes them, and the transport is a
fake that records what it was asked to send."""

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cargotel_search import parse_grid  # noqa: E402
from send_drafts import (  # noqa: E402
    FileTransport,
    Offers,
    SentLog,
    SesTransport,
    apply_offer_cap,
    compose,
    is_blocked,
    load_do_not_email,
    load_reps,
    read_drafts,
    rebuild,
    recheck_picks,
    rep_email_for,
    send_all,
)
from tests.test_suggest_reloads import grid_page, standard_rows  # noqa: E402


def draft_json(load_id, carrier, driver, rep, carrier_email, picks, dest_zip="60477", sales_rep="MITCHELL, STUART", sales_rep2=None):
    return {
        "dispatched": {
            "load_id": load_id, "customer": "Stellantis",
            "origin": {"name": "Port", "city": "Jacksonville", "state": "FL", "zip": "32206", "when": None, "date": None},
            "destination": {"name": "Dealer", "city": "Orland Hills", "state": "IL", "zip": dest_zip, "when": "12-Oct-2026 Mon 11 AM EST", "date": "2026-10-12"},
            "carrier": {"client_id": "80542", "name": carrier, "contact_name": "Eugen Padureanu", "email": carrier_email, "phone": None, "city": "Lombard", "state": "IL"},
            "dispatch_rep_id": "39093", "dispatch_rep": rep,
            "sales_rep_id": "27877" if sales_rep else None, "sales_rep": sales_rep, "sales_rep2_id": None, "sales_rep2": sales_rep2,
            "vehicles": [{"vin": "V1", "description": "2027 FIAT TOPOLINO"}],
            "esigned": True,
        },
        "search": {"title": "General Load Build w/  Map", "rows": 10, "radius_center": None, "built_loads": 2, "single_orders": 5},
        "picks": picks,
        "email": {"to": carrier_email, "cc": "<rep>", "subject": "x", "body": "x"},
        "event": {"carrier": carrier, "driver": driver, "load_id": load_id, "delivered_at": "10/12/2026 11:40", "destination": "Dealer | Orland Hills, IL", "origin": "", "units": 1, "loads": [load_id]},
    }


def pick(load_id, kind="load", units=3, members=None):
    return {
        "kind": kind, "load_id": load_id, "units": units, "origin": "Elwood, Il 60421", "origin_zip": "60421",
        "deadhead_miles": 21.2, "destinations": [["Rhinelander, WI 54501", units]], "dest_states": ["WI"],
        "road_miles": 366, "models": [["2500", units]], "price": 469.96 * units, "age_days": 3,
        "member_ids": members or [], "direction": "other", "score": 85.2,
    }


class FakeTransport:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = fail_for

    def send(self, message, from_addr):
        if message.load_id in self.fail_for:
            raise RuntimeError("ses down")
        self.sent.append((from_addr, message))
        return f"msg-{len(self.sent)}"


class RepTests(unittest.TestCase):
    def test_lookup_matches_both_name_forms(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "reps.csv")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("name,email\nBUJAGA, AMOSSI,amossi@example.com\nKyle Link,kyle@example.com\nbad line\nnoemail,notanaddress\n")
            reps = load_reps(path)
        self.assertEqual(len(reps), 2)
        self.assertEqual(rep_email_for("BUJAGA, AMOSSI", reps), "amossi@example.com")
        self.assertEqual(rep_email_for("Amossi Bujaga", reps), "amossi@example.com")
        self.assertEqual(rep_email_for("LINK, KYLE", reps), "kyle@example.com")
        self.assertIsNone(rep_email_for("CROMWELL, JUSTICE", reps))
        self.assertIsNone(rep_email_for(None, reps))

    def test_do_not_email(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "dne.txt")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("# stops\nbad@example.com\nAcme  Trucking LLC   # replied stop\n\n")
            blocked = load_do_not_email(path)
        self.assertEqual(blocked, {"bad@example.com", "acme trucking llc"})
        self.assertTrue(is_blocked("BAD@example.com", "Other", blocked))
        self.assertTrue(is_blocked("ok@example.com", "Acme Trucking LLC", blocked))
        self.assertFalse(is_blocked("ok@example.com", "Fine Carrier", blocked))


class OfferAndRecheckTests(unittest.TestCase):
    def test_offer_cap_counts_other_carriers_in_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            offers = Offers(os.path.join(tmp, "offers.json"))
            now = datetime(2026, 10, 12, 12, 0)
            offers.add(["344255"], "Carrier A", now - timedelta(hours=2))
            offers.add(["344255"], "Carrier B", now - timedelta(hours=3))
            offers.add(["344255"], "Carrier C", now - timedelta(hours=30))  # outside the window
            offers.add(["344231"], "Carrier A", now - timedelta(hours=1))
            picks = [p for p in (rebuild_pick(pick("344255")), rebuild_pick(pick("344231")), rebuild_pick(pick("344246")))]
            kept, dropped = apply_offer_cap(picks, offers, "Carrier D", now, cap=2, window_hours=24)
            self.assertEqual([p.load_id for p in dropped], ["344255"])
            self.assertEqual([p.load_id for p in kept], ["344231", "344246"])
            # The same carrier's own earlier offer does not count against it.
            kept, dropped = apply_offer_cap(picks, offers, "Carrier A", now, cap=2, window_hours=24)
            self.assertEqual([p.load_id for p in dropped], [])
            offers.save()
            self.assertEqual(len(Offers(offers.path).data["344255"]), 3)

    def test_recheck_against_grid(self):
        grid = parse_grid(grid_page(standard_rows()))  # open: 700001 (members 500001-2), 700002, singles 500006..; 500010 assigned
        picks = [rebuild_pick(pick("700001", members=["500001", "500002"])), rebuild_pick(pick("500006", "order", 1)),
                 rebuild_pick(pick("500010", "order", 1)), rebuild_pick(pick("999999", members=["999998"]))]
        kept, dropped = recheck_picks(picks, grid)
        self.assertEqual([p.load_id for p in kept], ["700001", "500006"])
        self.assertEqual([p.load_id for p in dropped], ["500010", "999999"])


def rebuild_pick(data):
    from suggest_reloads import Suggestion
    return Suggestion(**data)


class SendAllTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.drafts_dir = os.path.join(self.tmp.name, "drafts")
        os.makedirs(self.drafts_dir)
        self.write("343514", "88 Transpoort LLC", "PADUREANU, EUGEN", "BUJAGA, AMOSSI", "dispatch@example.com", [pick("344255"), pick("344231"), pick("344246", "order", 1)])
        self.write("343600", "Blocked Carrier Inc", "X, Y", "BUJAGA, AMOSSI", "blocked@example.com", [pick("344255")])
        self.write("343700", "No Email Inc", "A, B", "BUJAGA, AMOSSI", None, [pick("344255")])
        # Sales rep not in reps.csv -> held
        self.write("343800", "Other Carrier LLC", "C, D", "BUJAGA, AMOSSI", "other@example.com", [pick("344255")], sales_rep="CROMWELL, JUSTICE")
        self.reps = {"stuart mitchell": "stuart@example.com", "amossi bujaga": "amossi@example.com"}
        self.blocked = {"blocked@example.com"}
        self.logs = []

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, load_id, carrier, driver, rep, email, picks, **kw):
        data = draft_json(load_id, carrier, driver, rep, email, picks, **kw)
        with open(os.path.join(self.drafts_dir, f"{load_id}_x.json"), "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def run_send(self, transport, mode="auto", **kw):
        return send_all(
            read_drafts(self.drafts_dir), reps=self.reps, transport=transport, from_addr="vehicles@example.com", mode=mode,
            sent_log=SentLog(os.path.join(self.tmp.name, "sent.jsonl")), offers=Offers(os.path.join(self.tmp.name, "offers.json")),
            blocked=self.blocked, now=datetime(2026, 10, 12, 12, 0), log=self.logs.append, reply_to="vehicles@example.com", **kw,
        )

    def test_pilot_mode_sends_to_sales_rep_with_carrier_shown(self):
        transport = FakeTransport()
        counts = self.run_send(transport, mode="pilot")
        self.assertEqual(counts["sent"], 1)
        self.assertEqual(counts["blocked"], 1)
        self.assertEqual(counts["no_carrier_email"], 1)
        self.assertEqual(counts["no_rep_email"], 1)  # sales rep Justice Cromwell is not in reps.csv
        from_addr, message = transport.sent[0]
        self.assertEqual(from_addr, "vehicles@example.com")
        self.assertEqual(message.to, ["stuart@example.com"])  # the sales rep, not the dispatch rep
        self.assertEqual(message.cc, [])
        self.assertEqual(message.reply_to, ["vehicles@example.com"])
        self.assertTrue(message.subject.startswith("[Pilot] Next loads near Orland Hills, IL after load 343514"))
        self.assertIn("forward it to the carrier at dispatch@example.com", message.body)
        self.assertIn("You dropped load 343514 (2027 FIAT TOPOLINO) in Orland Hills, IL 60477 on 10/12/2026.", message.body)
        self.assertIn("1. Load 344255: 3 units", message.body)
        self.assertIn("call Stuart Mitchell", message.body)
        # Second run: already sent, nothing goes out again.
        counts = self.run_send(transport, mode="pilot")
        self.assertEqual((counts["sent"], counts["already_sent"]), (0, 1))
        self.assertEqual(len(transport.sent), 1)

    def test_fallback_inbox_covers_missing_rep(self):
        transport = FakeTransport()
        counts = self.run_send(transport, mode="pilot", fallback_to="owner@example.com")
        self.assertEqual(counts["sent"], 2)
        to_lists = sorted(m.to[0] for _, m in transport.sent)
        self.assertEqual(to_lists, ["owner@example.com", "stuart@example.com"])

    def test_default_is_auto_mode_to_carrier_cc_sales_rep(self):
        transport = FakeTransport()
        counts = self.run_send(transport)  # no mode given
        self.assertEqual(counts["sent"], 1)
        _, message = transport.sent[0]
        self.assertEqual(message.mode, "auto")
        self.assertEqual(message.to, ["dispatch@example.com"])
        self.assertEqual(message.cc, ["stuart@example.com"])
        self.assertNotIn("PILOT", message.body)

    def test_auto_mode_addresses_carrier_and_copies_sales_rep(self):
        transport = FakeTransport()
        counts = self.run_send(transport, mode="auto")
        self.assertEqual(counts["sent"], 1)
        _, message = transport.sent[0]
        self.assertEqual(message.to, ["dispatch@example.com"])
        self.assertEqual(message.cc, ["stuart@example.com"])
        self.assertEqual(message.reply_to, ["vehicles@example.com"])  # replies go to the group
        self.assertFalse(message.subject.startswith("[Pilot]"))
        self.assertNotIn("PILOT", message.body)

    def test_secondary_sales_rep_is_copied_and_missing_sales_rep_is_held(self):
        self.write("343900", "Two Reps Inc", "E, F", "BUJAGA, AMOSSI", "two@example.com", [pick("344255")],
                   sales_rep="MITCHELL, STUART", sales_rep2="BUJAGA, AMOSSI")
        self.write("343950", "No Sales Rep Inc", "G, H", "BUJAGA, AMOSSI", "nosales@example.com", [pick("344255")], sales_rep=None)
        transport = FakeTransport()
        counts = self.run_send(transport, mode="auto")
        by_load = {m.load_id: m for _, m in transport.sent}
        self.assertEqual(by_load["343900"].cc, ["stuart@example.com", "amossi@example.com"])
        self.assertNotIn("343950", by_load)  # no sales rep: held, never redirected to the dispatch rep
        self.assertEqual(counts["no_sales_rep"], 1)
        self.assertTrue(any("343950, no sales rep assigned" in m for m in self.logs))

    def test_signature_uses_sales_rep_details(self):
        details = {"stuart mitchell": {"email": "stuart@example.com", "title": "Sales Manager"}}
        transport = FakeTransport()
        self.run_send(transport, mode="auto", rep_details=details)
        _, message = transport.sent[0]
        self.assertTrue(message.body.rstrip().endswith("Stuart Mitchell\nSales Manager, Circle Logistics\nstuart@example.com"))
        self.assertIn("call Stuart Mitchell to book", message.body)
        self.assertNotIn("Amossi Bujaga", message.body)

    def test_signature_and_closing_carry_the_rep_phone_when_known(self):
        details = {"stuart mitchell": {"email": "stuart@example.com", "title": "Sales Manager", "phone": "260-208-4500 ext. 1101"}}
        transport = FakeTransport()
        self.run_send(transport, mode="auto", rep_details=details)
        _, message = transport.sent[0]
        self.assertIn("call Stuart Mitchell at 260-208-4500 ext. 1101 to book", message.body)
        self.assertTrue(message.body.rstrip().endswith("Stuart Mitchell\nSales Manager, Circle Logistics\nstuart@example.com\n260-208-4500 ext. 1101"))

    def test_one_email_per_carrier_per_cooldown(self):
        # A second truck of the same carrier (same email, different name spelling) the same day is held.
        self.write("343990", "88 TRANSPOORT LLC", "OTHER, DRIVER", "BUJAGA, AMOSSI", "Dispatch@Example.com", [pick("344401", "order", 1)])
        transport = FakeTransport()
        counts = self.run_send(transport)
        self.assertEqual(counts["sent"], 1)
        self.assertEqual(counts["carrier_recent"], 1)
        self.assertEqual([m.load_id for _, m in transport.sent], ["343514"])
        self.assertTrue(any("one email per carrier per 24 h" in m for m in self.logs))
        # With no cooldown the held one goes out on the next run (the first is in the sent log),
        # and it does not repeat the loads the carrier already saw.
        transport = FakeTransport()
        counts = self.run_send(transport, carrier_cooldown_hours=0)
        self.assertEqual((counts["sent"], counts["already_sent"]), (1, 1))
        second = next(m for _, m in transport.sent if m.load_id == "343990")
        self.assertEqual(second.picks, ["344401"])

    def test_same_load_not_offered_twice_to_same_carrier(self):
        offers_path = os.path.join(self.tmp.name, "offers.json")
        offers = Offers(offers_path)
        now = datetime(2026, 10, 12, 12, 0)
        offers.add(["344255", "344231"], "dispatch@example.com", now - timedelta(hours=20))  # this carrier saw these yesterday
        offers.save()
        transport = FakeTransport()
        counts = self.run_send(transport)
        self.assertEqual(counts["sent"], 1)
        _, message = transport.sent[0]
        self.assertEqual(message.picks, ["344246"])
        self.assertNotIn("344255", message.body)
        with open(os.path.join(self.tmp.name, "sent.jsonl"), encoding="utf-8") as handle:
            entry = json.loads(handle.readline())
        self.assertEqual(entry["repeated"], ["344255", "344231"])
        self.assertEqual(entry["carrier_key"], "dispatch@example.com")
        # If every load was already offered to this carrier, nothing goes out.
        offers = Offers(offers_path)
        offers.add(["344246"], "dispatch@example.com", now - timedelta(hours=1))
        offers.save()
        os.remove(os.path.join(self.tmp.name, "sent.jsonl"))
        transport = FakeTransport()
        counts = self.run_send(transport)
        self.assertEqual((counts["sent"], counts["nothing_left"]), (0, 1))

    def test_stale_draft_is_not_sent(self):
        path = os.path.join(self.drafts_dir, "343514_x.json")
        # Relative to the fixed clock run_send uses, not the wall clock (the files are written today).
        old = (datetime(2026, 10, 12, 12, 0) - timedelta(hours=20)).timestamp()
        os.utime(path, (old, old))
        transport = FakeTransport()
        counts = self.run_send(transport)
        self.assertEqual(counts["stale"], 1)
        self.assertEqual(counts["sent"], 0)
        counts = self.run_send(transport, draft_max_age_hours=48)
        self.assertEqual(counts["sent"], 1)

    def test_llm_writer_is_used_and_falls_back(self):
        calls = []

        def writer(load, picks, *, signature, delivered_at):
            calls.append((load.load_id, [p.load_id for p in picks], signature, delivered_at))
            return "Model subject", "Model body for " + load.load_id, {"inputTokens": 900, "cacheReadInputTokens": 1100, "outputTokens": 400}

        transport = FakeTransport()
        counts = self.run_send(transport, writer=writer, rep_details={"stuart mitchell": {"email": "stuart@example.com", "title": "Sales Manager"}})
        self.assertEqual(counts["sent"], 1)
        _, message = transport.sent[0]
        self.assertEqual(message.subject, "Model subject")
        self.assertEqual(message.body, "Model body for 343514")
        self.assertEqual(calls[0][0], "343514")
        self.assertEqual(calls[0][2], {"email": "stuart@example.com", "title": "Sales Manager"})
        with open(os.path.join(self.tmp.name, "sent.jsonl"), encoding="utf-8") as handle:
            entry = json.loads(handle.readline())
        self.assertEqual(entry["source"], "model")
        self.assertEqual(entry["bedrock_usage"]["cacheReadInputTokens"], 1100)
        self.assertTrue(any("1100 cached" in m for m in self.logs))

        def failing_writer(load, picks, *, signature, delivered_at):
            raise RuntimeError("bedrock down")

        os.remove(os.path.join(self.tmp.name, "sent.jsonl"))
        os.remove(os.path.join(self.tmp.name, "offers.json"))  # else the 72 h repeat guard blocks the resend
        transport = FakeTransport()
        counts = self.run_send(transport, writer=failing_writer)
        self.assertEqual(counts["sent"], 1)
        _, message = transport.sent[0]
        self.assertIn("1. Load 344255:", message.body)  # template body
        self.assertTrue(any("model draft failed, template used" in m for m in self.logs))

        def rejecting_writer(load, picks, *, signature, delivered_at):
            return None  # failed the fact check

        os.remove(os.path.join(self.tmp.name, "sent.jsonl"))
        os.remove(os.path.join(self.tmp.name, "offers.json"))  # else the 72 h repeat guard blocks the resend
        transport = FakeTransport()
        self.run_send(transport, writer=rejecting_writer)
        _, message = transport.sent[0]
        self.assertIn("1. Load 344255:", message.body)

    def test_carrier_key(self):
        from send_drafts import carrier_key
        self.assertEqual(carrier_key(" Dispatch@Example.com ", "88 Transpoort LLC"), "dispatch@example.com")
        self.assertEqual(carrier_key(None, "88  TRANSPOORT LLC"), "88 transpoort llc")
        self.assertEqual(carrier_key("", None), "")

    def test_load_rep_details_with_title(self):
        from send_drafts import load_rep_details, rep_signature_for
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "reps.csv")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("name,email,title,phone\n# comment, with, commas\nMITCHELL, STUART,stuart@example.com,Sales Manager\nHANEY, ANGELICA,angelica@example.com\nLINK, KYLE,kyle@example.com,\n"
                             "STORM, VICTORIA,victoria@example.com,Senior Account Manager,260-208-4500 ext. 1212\n"
                             "CROMWELL, JUSTICE,justice@example.com,(260) 204-3067,Sales Rep\n")
            details = load_rep_details(path)
        self.assertEqual(details["stuart mitchell"], {"email": "stuart@example.com", "title": "Sales Manager", "phone": ""})
        self.assertEqual(details["angelica haney"], {"email": "angelica@example.com", "title": "", "phone": ""})
        self.assertEqual(details["kyle link"], {"email": "kyle@example.com", "title": "", "phone": ""})
        self.assertEqual(details["victoria storm"], {"email": "victoria@example.com", "title": "Senior Account Manager", "phone": "260-208-4500 ext. 1212"})
        self.assertEqual(details["justice cromwell"], {"email": "justice@example.com", "title": "Sales Rep", "phone": "(260) 204-3067"})  # phone and title in either order
        self.assertEqual(rep_signature_for("Victoria Storm", details), {"email": "victoria@example.com", "title": "Senior Account Manager", "phone": "260-208-4500 ext. 1212"})
        self.assertEqual(rep_signature_for("Stuart Mitchell", details), {"email": "stuart@example.com", "title": "Sales Manager"})
        self.assertEqual(rep_signature_for("ANGELICA HANEY", details), {"email": "angelica@example.com"})
        self.assertEqual(rep_signature_for(None, details), {})

    def test_offer_cap_and_sent_log_and_offers_file(self):
        offers_path = os.path.join(self.tmp.name, "offers.json")
        offers = Offers(offers_path)
        now = datetime(2026, 10, 12, 12, 0)
        for carrier in ("A", "B", "C"):
            offers.add(["344255"], carrier, now - timedelta(hours=1))
        offers.save()
        transport = FakeTransport()
        counts = self.run_send(transport)
        self.assertEqual(counts["sent"], 1)
        _, message = transport.sent[0]
        self.assertEqual(message.picks, ["344231", "344246"])  # 344255 capped at 3 other carriers
        self.assertNotIn("344255", message.body)
        with open(os.path.join(self.tmp.name, "sent.jsonl"), encoding="utf-8") as handle:
            entry = json.loads(handle.readline())
        self.assertEqual(entry["capped"], ["344255"])
        self.assertEqual(entry["message_id"], "msg-1")
        self.assertEqual(entry["mode"], "auto")
        saved = Offers(offers_path)
        self.assertEqual([e["carrier"] for e in saved.data["344231"]], ["dispatch@example.com"])  # carriers are keyed by email

    def test_recheck_drops_gone_loads_and_skips_empty(self):
        grid = parse_grid(grid_page(standard_rows()))  # none of 344255/344231/344246 are on it
        transport = FakeTransport()
        counts = self.run_send(transport, recheck=lambda zip_code: grid)
        self.assertEqual(counts["nothing_left"], 1)
        self.assertEqual(counts["sent"], 0)
        self.assertTrue(any("3 gone" in m for m in self.logs))

    def test_send_failure_is_logged_not_recorded(self):
        transport = FakeTransport(fail_for=("343514",))
        counts = self.run_send(transport)
        self.assertEqual(counts["error"], 1)
        self.assertEqual(counts["sent"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.tmp.name, "sent.jsonl")))

    def test_file_transport_writes_outbox(self):
        outbox = os.path.join(self.tmp.name, "outbox")
        counts = self.run_send(FileTransport(outbox))
        self.assertEqual(counts["sent"], 1)
        files = os.listdir(outbox)
        self.assertEqual(files, ["343514-88-transpoort-llc.txt"])
        with open(os.path.join(outbox, files[0]), encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue(text.startswith("From: vehicles@example.com\nTo: dispatch@example.com\nCc: stuart@example.com\n"))

    def test_ses_transport_request_shape(self):
        class FakeClient:
            def __init__(self):
                self.requests = []

            def send_email(self, **request):
                self.requests.append(request)
                return {"MessageId": "ses-123"}

        client = FakeClient()
        transport = SesTransport(client=client, configuration_set="reloads")
        counts = self.run_send(transport, mode="auto")
        self.assertEqual(counts["sent"], 1)
        request = client.requests[0]
        self.assertEqual(request["FromEmailAddress"], "vehicles@example.com")
        self.assertEqual(request["Destination"], {"ToAddresses": ["dispatch@example.com"], "CcAddresses": ["stuart@example.com"]})
        self.assertEqual(request["ReplyToAddresses"], ["vehicles@example.com"])
        self.assertEqual(request["ConfigurationSetName"], "reloads")
        self.assertEqual(request["Content"]["Simple"]["Subject"]["Data"], "Next loads near Orland Hills, IL after load 343514")

    def test_rebuild_round_trip(self):
        draft = read_drafts(self.drafts_dir)[0]
        load, picks = rebuild(draft)
        self.assertEqual(load.destination.zip, "60477")
        self.assertEqual(load.carrier.email, "dispatch@example.com")
        self.assertEqual(load.rep_display, "Amossi Bujaga")
        self.assertEqual(load.contact_rep_display, "Stuart Mitchell")
        self.assertTrue(load.esigned)
        self.assertEqual(picks[0].dest_summary(), "Rhinelander, WI 54501")
        self.assertEqual(draft.key, "343514|88 transpoort llc|padureanu, eugen")


if __name__ == "__main__":
    unittest.main()
