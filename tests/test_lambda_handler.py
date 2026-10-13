"""Unit tests for lambda_handler.py (``python -m unittest``). No AWS: S3 is a small
in-memory fake, and the watcher and sender mains are replaced by fakes that write files."""

import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lambda_handler  # noqa: E402


class FakeS3:
    """Just enough of a boto3 S3 client: list_objects_v2 paging, download, upload."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, datetime]] = {}
        self.uploads: list[str] = []

    def put(self, key: str, text: str = "", modified: datetime | None = None) -> None:
        self.objects[key] = (text.encode("utf-8"), modified or datetime.now(timezone.utc))

    def get_paginator(self, name: str):
        assert name == "list_objects_v2"
        return self

    def paginate(self, Bucket: str, Prefix: str):
        contents = [{"Key": key, "LastModified": modified, "Size": len(body)}
                    for key, (body, modified) in sorted(self.objects.items()) if key.startswith(Prefix)]
        # Two pages, to exercise the paging loop.
        yield {"Contents": contents[:1]}
        yield {"Contents": contents[1:]}

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:
        with open(Filename, "wb") as handle:
            handle.write(self.objects[Key][0])

    def upload_file(self, Filename: str, Bucket: str, Key: str) -> None:
        with open(Filename, "rb") as handle:
            self.objects[Key] = (handle.read(), datetime.now(timezone.utc))
        self.uploads.append(Key)


def arg_after(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


class SyncTests(unittest.TestCase):
    def test_sync_down_keeps_mtime_and_skips_old_files_and_folder_markers(self):
        s3 = FakeS3()
        now = datetime.now(timezone.utc)
        s3.put("drafts/")  # folder marker
        s3.put("drafts/344329_stepauto.json", "{}", now - timedelta(hours=2))
        s3.put("drafts/344329_stepauto.txt", "hi", now - timedelta(hours=2))
        s3.put("drafts/old.json", "{}", now - timedelta(days=3))
        s3.put("state/offers.json", "{}", now)  # other prefix
        with tempfile.TemporaryDirectory() as tmp:
            count = lambda_handler.sync_down("b", "drafts/", tmp, newer_than=now - timedelta(hours=24), client=s3)
            self.assertEqual(count, 2)
            self.assertEqual(sorted(os.listdir(tmp)), ["344329_stepauto.json", "344329_stepauto.txt"])
            expected = (now - timedelta(hours=2)).timestamp()
            self.assertAlmostEqual(os.path.getmtime(os.path.join(tmp, "344329_stepauto.json")), expected, delta=1.5)

    def test_sync_up_uploads_only_changed_files_with_forward_slash_keys(self):
        s3 = FakeS3()
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sub"))
            fresh = os.path.join(tmp, "sub", "new.txt")
            stale = os.path.join(tmp, "old.txt")
            for path in (fresh, stale):
                with open(path, "w", encoding="utf-8") as handle:
                    handle.write("x")
            day_ago = time.time() - 86400
            os.utime(stale, (day_ago, day_ago))
            count = lambda_handler.sync_up("b", "outbox/", tmp, changed_since=time.time() - 3600, client=s3)
            self.assertEqual(count, 1)
            self.assertEqual(s3.uploads, ["outbox/sub/new.txt"])
            # Without a cut-off everything goes up.
            self.assertEqual(lambda_handler.sync_up("b", "outbox/", tmp, client=s3), 2)

    def test_sync_up_of_missing_folder_is_zero(self):
        self.assertEqual(lambda_handler.sync_up("b", "pages/", "/nonexistent/folder", client=FakeS3()), 0)


class ArgTests(unittest.TestCase):
    def test_defaults_are_dry_run_with_recheck(self):
        args = lambda_handler.sender_args("/w", {})
        self.assertNotIn("--send", args)
        self.assertNotIn("--llm", args)
        self.assertIn("--recheck", args)
        self.assertEqual(arg_after(args, "--mode"), "auto")
        self.assertEqual(arg_after(args, "--max-sends"), "20")
        self.assertEqual(arg_after(args, "--from"), lambda_handler.DEFAULT_FROM)
        self.assertEqual(arg_after(args, "--reps"), os.path.join("/w", "config", "reps.csv"))
        self.assertEqual(arg_after(args, "--sent-log"), os.path.join("/w", "state", "sent_log.jsonl"))
        watcher = lambda_handler.watcher_args("/w", {})
        self.assertEqual(arg_after(watcher, "--trigger"), "accepted")
        self.assertEqual(arg_after(watcher, "--out-dir"), os.path.join("/w", "drafts"))
        self.assertNotIn("--save-html", watcher)

    def test_env_switches(self):
        env = {"RELOAD_SEND": "1", "RELOAD_LLM": "true", "RELOAD_RECHECK": "0", "RELOAD_MODE": "pilot",
               "RELOAD_MAX_SENDS": "5", "SES_CONFIGURATION_SET": "circle-reload", "MAIL_FROM": "a@b.c",
               "RELOAD_SAVE_PAGES": "1", "RELOAD_TRIGGER": "delivered", "RELOAD_MAX_EVENTS": "3", "RELOAD_REQUIRE_ESIGN": "yes"}
        args = lambda_handler.sender_args("/w", env)
        self.assertIn("--send", args)
        self.assertIn("--llm", args)
        self.assertNotIn("--recheck", args)
        self.assertEqual(arg_after(args, "--mode"), "pilot")
        self.assertEqual(arg_after(args, "--max-sends"), "5")
        self.assertEqual(arg_after(args, "--configuration-set"), "circle-reload")
        self.assertEqual(arg_after(args, "--from"), "a@b.c")
        watcher = lambda_handler.watcher_args("/w", env)
        self.assertEqual(arg_after(watcher, "--trigger"), "delivered")
        self.assertEqual(arg_after(watcher, "--max-events"), "3")
        self.assertIn("--require-esign", watcher)
        self.assertEqual(arg_after(watcher, "--save-html"), os.path.join("/w", "pages"))


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = os.path.join(self.tmp.name, "work")
        self.s3 = FakeS3()
        hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
        self.s3.put("config/reps.csv", "LINK, KYLE,kyle@example.com,Sales Rep\n", hour_ago)
        self.s3.put("state/watcher_state.json", '{"events": {}}', hour_ago)
        self.s3.put("drafts/344329_stepauto.json", "{}", hour_ago)
        self.env = {"RELOAD_BUCKET": "work-bucket", "RELOAD_WORKDIR": self.work}
        self.env_patch = mock.patch.dict(os.environ, self.env, clear=False)
        self.env_patch.start()
        self.client_patch = mock.patch.object(lambda_handler, "s3_client", lambda: self.s3)
        self.client_patch.start()

    def tearDown(self):
        self.client_patch.stop()
        self.env_patch.stop()
        self.tmp.cleanup()

    def test_pass_runs_watcher_then_sender_and_uploads_what_changed(self):
        calls = []

        def fake_watcher(argv):
            calls.append("watcher")
            state = arg_after(argv, "--state")
            self.assertTrue(os.path.exists(state), "state was not downloaded first")
            self.assertTrue(os.path.exists(os.path.join(arg_after(argv, "--out-dir"), "344329_stepauto.json")))
            with open(state, "w", encoding="utf-8") as handle:
                handle.write('{"events": {"x": 1}}')
            with open(os.path.join(arg_after(argv, "--out-dir"), "344330_acme.json"), "w", encoding="utf-8") as handle:
                handle.write("{}")
            return 0

        def fake_sender(argv):
            calls.append("sender")
            self.assertNotIn("--send", argv)
            self.assertTrue(os.path.exists(arg_after(argv, "--reps")), "reps.csv was not downloaded")
            with open(arg_after(argv, "--sent-log"), "a", encoding="utf-8") as handle:
                handle.write("{}\n")
            os.makedirs(arg_after(argv, "--outbox"), exist_ok=True)
            with open(os.path.join(arg_after(argv, "--outbox"), "344330.eml"), "w", encoding="utf-8") as handle:
                handle.write("mail")
            return 0

        with mock.patch.object(lambda_handler.dispatch_watcher, "main", fake_watcher), \
                mock.patch.object(lambda_handler.send_drafts, "main", fake_sender):
            result = lambda_handler.handler({}, None)

        self.assertEqual(calls, ["watcher", "sender"])
        self.assertEqual(result["watcher_rc"], 0)
        self.assertEqual(result["sender_rc"], 0)
        self.assertFalse(result["send"])
        self.assertEqual(result["down"], {"config": 1, "state": 1, "drafts": 1})
        self.assertEqual(result["up"], {"config": 0, "state": 2, "drafts": 1, "outbox": 1, "pages": 0})
        self.assertEqual(sorted(self.s3.uploads), ["drafts/344330_acme.json", "outbox/344330.eml",
                                                   "state/sent_log.jsonl", "state/watcher_state.json"])
        self.assertEqual(self.s3.objects["state/watcher_state.json"][0], b'{"events": {"x": 1}}')

    def test_state_is_uploaded_even_when_the_watcher_fails(self):
        def failing_watcher(argv):
            with open(arg_after(argv, "--state"), "w", encoding="utf-8") as handle:
                handle.write('{"events": {"partial": 1}}')
            raise RuntimeError("CargoTel down")

        with mock.patch.object(lambda_handler.dispatch_watcher, "main", failing_watcher), \
                mock.patch.object(lambda_handler.send_drafts, "main", lambda argv: self.fail("sender must not run")):
            with self.assertRaises(RuntimeError):
                lambda_handler.handler({}, None)
        self.assertEqual(self.s3.uploads, ["state/watcher_state.json"])

    def test_paused_does_nothing(self):
        with mock.patch.dict(os.environ, {"RELOAD_PAUSED": "1"}), \
                mock.patch.object(lambda_handler, "s3_client", lambda: self.fail("no S3 while paused")):
            self.assertEqual(lambda_handler.handler({}, None), {"paused": True})


if __name__ == "__main__":
    unittest.main()
