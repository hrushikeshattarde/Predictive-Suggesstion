"""Unit tests for macropoint_webhook.py (``python -m unittest`` from the macropoint_webhook
folder). The calls are shaped like AWS Lambda function-URL events (payload 2.0) carrying
MacroPoint's documented Location Updates query string; S3 is a dict."""

import base64
import io
import json
import os
import sys
import unittest
from datetime import datetime, timezone

FOLDER = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if FOLDER not in sys.path:
    sys.path.insert(0, FOLDER)

import macropoint_webhook as mw  # noqa: E402

TOKEN = "t0ken-abc123"
ENV = {"MP_PATH_TOKEN": TOKEN, "MP_BUCKET": "b", "MP_PREFIX": "macropoint/"}
NOW = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)
MP_IP = "52.141.169.195"  # inside 52.141.169.192/28


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.puts = 0

    def get_object(self, Bucket, Key):
        if (Bucket, Key) not in self.objects:
            raise KeyError("NoSuchKey")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, Bucket, Key, Body, ContentType=None):
        self.objects[(Bucket, Key)] = Body
        self.puts += 1

    def record(self, load_id):
        return json.loads(self.objects[("b", f"macropoint/latest/{load_id}.json")])


def query(load_id="354350", when="2026-11-12T14:17:00Z", city="Forsyth", state="GA", lat="33.0343", lng="-83.9380",
          locator="4079707252", source="Ping"):
    return (f"MPOrderID=987654&ID={load_id}&Latitude={lat}&Longitude={lng}&Uncertainty=0&Street1=I-75&Street2=&Neighborhood="
            f"&City={city}&State={state}&Postal=31029&Country=USA&LocationDateTimeUTC={when}"
            f"&ApproxLocationDateTimeInLocalTime=2026-11-12T09:17:00&DataSource={source}&MPTrackingRequestID=&locator={locator}")


def event(qs, path=f"/{TOKEN}", ip=MP_IP, method="GET", headers=None, body=None):
    return {"version": "2.0", "rawPath": path, "rawQueryString": qs, "headers": headers or {},
            "requestContext": {"http": {"method": method, "sourceIp": ip, "path": path}},
            "body": body, "isBase64Encoded": False}


class ParseTests(unittest.TestCase):
    def test_parse_when(self):
        self.assertEqual(mw.parse_when("2026-11-12T14:17:00Z"), datetime(2026, 11, 12, 14, 17, tzinfo=timezone.utc))
        self.assertEqual(mw.parse_when("2026-11-12T09:17:00-05:00"), datetime(2026, 11, 12, 14, 17, tzinfo=timezone.utc))
        self.assertEqual(mw.parse_when("2026-11-12T14:17:00.1234567Z"), datetime(2026, 11, 12, 14, 17, 0, 123456, tzinfo=timezone.utc))
        self.assertEqual(mw.parse_when("2026-11-12 14:17:00"), datetime(2026, 11, 12, 14, 17, tzinfo=timezone.utc))
        self.assertIsNone(mw.parse_when(""))
        self.assertIsNone(mw.parse_when("yesterday"))

    def test_ping_fields(self):
        ping = mw.ping_from_params(mw.request_params(event(query())), NOW)
        self.assertEqual(ping["load_id"], "354350")
        self.assertEqual(ping["when_utc"], "2026-11-12T14:17:00+00:00")
        self.assertEqual((ping["city"], ping["state"], ping["postal"]), ("Forsyth", "GA", "31029"))
        self.assertEqual((ping["lat"], ping["lng"]), (33.0343, -83.938))
        self.assertEqual((ping["data_source"], ping["locator"], ping["mp_order_id"]), ("Ping", "4079707252", "987654"))

    def test_post_bodies_are_read_too(self):
        params = mw.request_params(event("", method="POST", body=json.dumps({"ID": "354350", "City": "Macon", "State": "GA"})))
        self.assertEqual((params["id"], params["city"]), ("354350", "Macon"))
        params = mw.request_params(event("", method="POST", body="ID=354350&City=Macon&State=GA"))
        self.assertEqual(params["state"], "GA")
        encoded = base64.b64encode(b"ID=354351&City=Perry").decode()
        e = event("", method="POST", body=encoded)
        e["isBase64Encoded"] = True
        self.assertEqual(mw.request_params(e)["id"], "354351")


class HandleTests(unittest.TestCase):
    def setUp(self):
        self.s3 = FakeS3()

    def call(self, e, env=ENV):
        return mw.handle(e, s3=self.s3, env=env, now=NOW)

    def test_stores_the_latest_ping_per_load(self):
        self.assertEqual(self.call(event(query()))["statusCode"], 200)
        rec = self.s3.record("354350")
        self.assertEqual(rec["latest"]["city"], "Forsyth")
        self.assertEqual(rec["history"], [])
        # a newer ping becomes latest, the old one moves to history
        self.call(event(query(when="2026-11-12T14:32:00Z", city="Macon", lat="32.84")))
        rec = self.s3.record("354350")
        self.assertEqual((rec["latest"]["city"], rec["history"][0]["city"]), ("Macon", "Forsyth"))
        # an older ping arriving late never replaces the newest
        self.call(event(query(when="2026-11-12T14:02:00Z", city="Locust Grove", lat="33.34")))
        rec = self.s3.record("354350")
        self.assertEqual(rec["latest"]["city"], "Macon")
        self.assertEqual([p["city"] for p in rec["history"]], ["Forsyth", "Locust Grove"])
        # the same ping twice is stored once
        puts = self.s3.puts
        self.call(event(query(when="2026-11-12T14:32:00Z", city="Macon", lat="32.84")))
        self.assertEqual(self.s3.puts, puts)

    def test_history_is_capped(self):
        for minute in range(30):
            self.call(event(query(when=f"2026-11-12T14:{minute:02d}:00Z", lat=str(33 + minute / 100))))
        rec = self.s3.record("354350")
        self.assertEqual(rec["latest"]["when_utc"], "2026-11-12T14:29:00+00:00")
        self.assertEqual(len(rec["history"]), mw.DEFAULT_HISTORY)

    def test_other_order_ids_are_dropped(self):
        for load_id in ("2650622", "Load-123", "1002-351455", ""):
            self.assertEqual(self.call(event(query(load_id=load_id)))["body"], "ignored")
        self.assertEqual(self.s3.objects, {})
        self.assertEqual(self.call(event(query(load_id="2650622")), env={**ENV, "MP_ID_PATTERN": r"^\d{6,7}$"})["body"], "OK")

    def test_auth(self):
        self.assertEqual(self.call(event(query(), path="/"))["statusCode"], 403)
        self.assertEqual(self.call(event(query(), path="/wrong-token"))["statusCode"], 403)
        self.assertEqual(self.call(event(query(), path=f"/macropoint/{TOKEN}"))["statusCode"], 200)
        self.assertEqual(self.call(event(query(), path="/"), env={})["statusCode"], 500)
        basic = {**ENV, "MP_PATH_TOKEN": "", "MP_BASIC_AUTH": "mp:s3cret"}
        good = {"Authorization": "Basic " + base64.b64encode(b"mp:s3cret").decode()}
        bad = {"authorization": "Basic " + base64.b64encode(b"mp:nope").decode()}
        self.assertEqual(self.call(event(query(), path="/", headers=good), env=basic)["statusCode"], 200)
        self.assertEqual(self.call(event(query(), path="/", headers=bad), env=basic)["statusCode"], 403)
        self.assertEqual(self.s3.puts, 1)  # every accepted call carried the same ping: stored once

    def test_source_address_is_logged_not_refused(self):
        self.assertTrue(mw.from_macropoint(event(query())))
        self.assertFalse(mw.from_macropoint(event(query(), ip="203.0.113.9")))
        self.assertEqual(self.call(event(query(), ip="203.0.113.9"))["statusCode"], 200)

    def test_storage_failure_is_a_500(self):
        class Broken(FakeS3):
            def get_object(self, Bucket, Key):
                raise RuntimeError("AccessDenied")

        self.s3 = Broken()
        self.assertEqual(self.call(event(query()))["statusCode"], 500)


if __name__ == "__main__":
    unittest.main()
