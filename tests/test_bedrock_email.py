"""Unit tests for bedrock_email.py that need no AWS: the fact check, the cache-point
request shape, the compacted facts, and the write_email fallback contract."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bedrock_email  # noqa: E402
from bedrock_email import SYSTEM_PROMPT, _compact, _system_blocks, check, usage_line  # noqa: E402

FACTS = {
    "load_id": "344141",
    "circle_rep": {"name": "Justice Cromwell", "role": "sales rep", "title": "Sales Rep", "email": None},
    "suggested_loads": [{"number": "344196"}, {"number": "344250"}],
}

GOOD = """Hi there,

Load 344141 delivers to Orland Hills, IL. Two loads are open at Elwood:
1. Load 344196: 3 vehicles to Cedar Rapids, IA, 220 road miles.
2. Load 344250: 1 vehicle to S Saint Paul, MN, 417 road miles.
Rates are available on request. Reply or call Justice Cromwell.

Justice Cromwell
Sales Rep, Circle Logistics"""


class CheckTests(unittest.TestCase):
    def test_good_email_passes(self):
        self.assertEqual(check(GOOD, FACTS), [])

    def test_missing_load_and_unknown_number(self):
        body = GOOD.replace("2. Load 344250: 1 vehicle to S Saint Paul, MN, 417 road miles.", "2. Load 999999: 1 vehicle to S Saint Paul, MN.")
        problems = check(body, FACTS)
        self.assertIn("suggested load 344250 is missing from the email", problems)
        self.assertIn("unknown six-digit number 999999 in the email", problems)

    def test_rates_phone_signature_and_rep(self):
        self.assertIn("a dollar amount appears in the email", check(GOOD + "\nPays $450.", FACTS))
        self.assertIn("a phone number appears in the email", check(GOOD + "\n(555) 010-0001", FACTS))
        self.assertIn("the signature is missing (the email may be cut off)", check(GOOD.replace("Sales Rep, Circle Logistics", ""), FACTS))
        self.assertIn("the sales rep Justice Cromwell is not named in the email", check(GOOD.replace("Justice Cromwell", "someone"), FACTS))

    def test_only_the_sales_rep_phone_is_allowed(self):
        facts = dict(FACTS, circle_rep=dict(FACTS["circle_rep"], phone="260-208-4500 ext. 1616"))
        body = GOOD.replace("Reply or call Justice Cromwell.", "Reply or call Justice Cromwell at 260-208-4500 ext. 1616.") + "\njustice@example.com\n260-208-4500 ext. 1616"
        self.assertEqual(check(body, facts), [])
        self.assertEqual(check(GOOD + "\n(260) 208-4500", facts), [])  # same number, other punctuation
        problems = check(body + "\nOr try (555) 010-0001.", facts)
        self.assertTrue(any(p.startswith("a phone number other than the sales rep's appears") for p in problems), problems)


class CachingShapeTests(unittest.TestCase):
    def test_system_blocks_end_with_a_cache_point(self):
        blocks = _system_blocks("1h")
        self.assertEqual(blocks[0], {"text": SYSTEM_PROMPT})
        self.assertEqual(blocks[1], {"cachePoint": {"type": "default", "ttl": "1h"}})
        self.assertEqual(_system_blocks(None)[1], {"cachePoint": {"type": "default"}})
        self.assertEqual(_system_blocks("")[1], {"cachePoint": {"type": "default"}})

    def test_system_prompt_is_static_and_above_the_minimum(self):
        # No dates or per-load text may ever appear in the cached prefix; byte-identical calls are what hit the cache.
        for marker in ("2026-", "344141", "{"):
            self.assertNotIn(marker, SYSTEM_PROMPT)
        # Bedrock needs at least 512 tokens of prefix for a checkpoint on Sonnet 5.5; ~4 characters per token,
        # so demand a comfortable margin in characters.
        self.assertGreater(len(SYSTEM_PROMPT), 512 * 4)

    def test_compact_drops_empty_values(self):
        self.assertEqual(_compact({"a": None, "b": "", "c": [], "d": {"e": None, "f": 1}, "g": [None, {"h": ""}]}), {"d": {"f": 1}, "g": [None, {}]})

    def test_usage_line(self):
        self.assertEqual(usage_line({"inputTokens": 900, "cacheReadInputTokens": 1100, "cacheWriteInputTokens": 0, "outputTokens": 400}),
                         "900 uncached in, 1100 cache read, 0 cache write, 400 out")
        self.assertEqual(usage_line({}), "0 uncached in, 0 cache read, 0 cache write, 0 out")


class WriteEmailTests(unittest.TestCase):
    def test_write_email_returns_none_when_check_fails(self):
        from suggest_reloads import DispatchedLoad, Stop, Suggestion

        load = DispatchedLoad(load_id="344141", destination=Stop(city="Orland Hills", state="IL", zip="60477"), sales_rep="CROMWELL, JUSTICE")
        picks = [Suggestion(kind="load", load_id="344196", units=3, origin="Elwood, Il 60421", origin_zip="60421", deadhead_miles=21.2,
                            destinations=[("Cedar Rapids, IA 52402", 3)], dest_states=["IA"], road_miles=220, models=[("2500", 3)],
                            price=None, age_days=3, member_ids=[])]
        original = bedrock_email.generate
        try:
            bedrock_email.generate = lambda facts, **kw: ("Subject", "Hi there, nothing useful here.", {"outputTokens": 5})
            self.assertIsNone(bedrock_email.write_email(load, picks, signature={}, delivered_at=None, model_id="m", profile=None, region=None))
            bedrock_email.generate = lambda facts, **kw: ("Subject", GOOD.replace("344250", "344196"), {"outputTokens": 50})
            result = bedrock_email.write_email(load, picks, signature={}, delivered_at=None, model_id="m", profile=None, region=None)
            self.assertEqual(result[0], "Subject")
            self.assertEqual(result[2], {"outputTokens": 50})
        finally:
            bedrock_email.generate = original


if __name__ == "__main__":
    unittest.main()
