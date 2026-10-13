"""AWS Lambda entry point: one pass of the dispatch watcher, then the sender, with the
working files kept in S3 instead of the local folder.

The existing modules are untouched; they still read and write plain files. This handler
mirrors a few S3 prefixes into a scratch folder, calls ``dispatch_watcher.main`` and
``send_drafts.main`` with paths inside it, then copies whatever changed back up. S3's
``LastModified`` becomes each downloaded file's mtime, because the sender drops drafts
older than ``--draft-max-age-hours`` by mtime.

Bucket layout (``RELOAD_BUCKET``)::

    config/reps.csv, config/do_not_email.txt        edited by the team, read every pass
    state/watcher_state.json, offers.json,
          sent_log.jsonl                            the duplicate guards
    drafts/<load>_<carrier>.json|.txt               the watcher's output, the sender's input
    outbox/...                                      dry-run emails while sending is off
    pages/...                                       archived CargoTel pages (RELOAD_SAVE_PAGES=1)

Environment variables (``RELOAD_BUCKET`` is required, the rest optional)::

    RELOAD_BUCKET          the bucket above
    RELOAD_PAUSED          "1" skips the pass (kill switch besides disabling the schedule)
    RELOAD_SEND            "1" sends through SES; otherwise emails go to outbox/
    RELOAD_MODE            auto (default) or pilot
    RELOAD_LLM             "1" has Claude on Bedrock word each email; template otherwise
    RELOAD_RECHECK         "0" skips the re-check that loads are still open (default on)
    RELOAD_MAX_SENDS       emails per pass (default 20)
    RELOAD_MAX_EVENTS      drafts per pass (watcher default 20)
    RELOAD_TRIGGER         accepted (default) or delivered
    RELOAD_MILES           search radius (default 150)
    RELOAD_REQUIRE_ESIGN   "1" also insists on the E-Signed Carrier Agreement
    RELOAD_SAVE_PAGES      "1" archives fetched pages under pages/
    RELOAD_WORKDIR         scratch folder (default /tmp/reload)
    MAIL_FROM              verified SES sender (default the vehicles@ group)
    SES_CONFIGURATION_SET  SES configuration set for bounce/complaint events
    BEDROCK_MODEL_ID       read by bedrock_email
    TZ                     America/New_York, so the age filters match CargoTel's clock
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

import boto3

import dispatch_watcher
import send_drafts

DEFAULT_WORKDIR = "/tmp/reload"
DEFAULT_FROM = "Circle Logistics <vehicles@circledelivers.com>"
DEFAULT_MAX_SENDS = "20"
DEFAULT_MILES = "150"
#: The sender ignores drafts older than 12 h, so a day's worth is all a pass needs.
DRAFT_LOOKBACK_HOURS = 24

CONFIG_PREFIX = "config/"
STATE_PREFIX = "state/"
DRAFTS_PREFIX = "drafts/"
OUTBOX_PREFIX = "outbox/"
PAGES_PREFIX = "pages/"
#: Local sub-folder -> S3 prefix. All of them are uploaded after a pass; only the first
#: three are downloaded before it.
FOLDERS = (("config", CONFIG_PREFIX), ("state", STATE_PREFIX), ("drafts", DRAFTS_PREFIX),
           ("outbox", OUTBOX_PREFIX), ("pages", PAGES_PREFIX))

_client: Any = None


def s3_client() -> Any:
    global _client
    if _client is None:
        _client = boto3.client("s3")
    return _client


def flag(env: Mapping[str, str], name: str, default: str = "0") -> bool:
    return env.get(name, default).strip().lower() in ("1", "true", "yes", "on")


def sync_down(bucket: str, prefix: str, local_dir: str, *, newer_than: datetime | None = None, client: Any = None) -> int:
    """Copy ``s3://bucket/prefix*`` into ``local_dir``, keeping LastModified as the mtime."""

    client = client or s3_client()
    os.makedirs(local_dir, exist_ok=True)
    count = 0
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):]
            if not rel or rel.endswith("/"):
                continue
            modified = obj["LastModified"]
            if newer_than is not None and modified < newer_than:
                continue
            path = os.path.join(local_dir, *rel.split("/"))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            client.download_file(bucket, obj["Key"], path)
            stamp = modified.timestamp()
            os.utime(path, (stamp, stamp))
            count += 1
    return count


def sync_up(bucket: str, prefix: str, local_dir: str, *, changed_since: float | None = None, client: Any = None) -> int:
    """Upload the files under ``local_dir``: all of them, or only those modified at or
    after ``changed_since`` (a Unix timestamp). Nothing is ever deleted in S3."""

    client = client or s3_client()
    if not os.path.isdir(local_dir):
        return 0
    count = 0
    for root, _dirs, files in os.walk(local_dir):
        for name in sorted(files):
            path = os.path.join(root, name)
            if changed_since is not None and os.path.getmtime(path) < changed_since:
                continue
            rel = os.path.relpath(path, local_dir).replace(os.sep, "/")
            client.upload_file(path, bucket, prefix + rel)
            count += 1
    return count


def watcher_args(work: str, env: Mapping[str, str]) -> list[str]:
    args = [
        "--state", os.path.join(work, "state", "watcher_state.json"),
        "--out-dir", os.path.join(work, "drafts"),
        "--trigger", env.get("RELOAD_TRIGGER", "accepted"),
        "--miles", env.get("RELOAD_MILES", DEFAULT_MILES),
    ]
    if env.get("RELOAD_MAX_EVENTS"):
        args += ["--max-events", env["RELOAD_MAX_EVENTS"]]
    if flag(env, "RELOAD_REQUIRE_ESIGN"):
        args.append("--require-esign")
    if flag(env, "RELOAD_SAVE_PAGES"):
        args += ["--save-html", os.path.join(work, "pages")]
    return args


def sender_args(work: str, env: Mapping[str, str]) -> list[str]:
    args = [
        "--drafts", os.path.join(work, "drafts"),
        "--reps", os.path.join(work, "config", "reps.csv"),
        "--do-not-email", os.path.join(work, "config", "do_not_email.txt"),
        "--sent-log", os.path.join(work, "state", "sent_log.jsonl"),
        "--offers", os.path.join(work, "state", "offers.json"),
        "--outbox", os.path.join(work, "outbox"),
        "--from", env.get("MAIL_FROM") or DEFAULT_FROM,
        "--mode", env.get("RELOAD_MODE", "auto"),
        "--max-sends", env.get("RELOAD_MAX_SENDS", DEFAULT_MAX_SENDS),
        "--miles", env.get("RELOAD_MILES", DEFAULT_MILES),
    ]
    if flag(env, "RELOAD_SEND"):
        args.append("--send")
    if flag(env, "RELOAD_LLM"):
        args.append("--llm")
    if flag(env, "RELOAD_RECHECK", "1"):
        args.append("--recheck")
    if env.get("SES_CONFIGURATION_SET"):
        args += ["--configuration-set", env["SES_CONFIGURATION_SET"]]
    return args


def apply_tz(env: Mapping[str, str]) -> None:
    """Honour TZ on Linux so ``datetime.now()`` in the modules is Eastern like CargoTel."""

    if env.get("TZ") and hasattr(time, "tzset"):
        time.tzset()


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} lambda: {message}", flush=True)


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    env = os.environ
    if flag(env, "RELOAD_PAUSED"):
        log("paused (RELOAD_PAUSED=1); nothing done")
        return {"paused": True}
    bucket = env["RELOAD_BUCKET"]
    apply_tz(env)
    work = env.get("RELOAD_WORKDIR", DEFAULT_WORKDIR)
    started = time.time()

    shutil.rmtree(work, ignore_errors=True)
    for folder, _prefix in FOLDERS:
        os.makedirs(os.path.join(work, folder), exist_ok=True)
    client = s3_client()
    lookback = datetime.now(timezone.utc) - timedelta(hours=DRAFT_LOOKBACK_HOURS)
    down = {
        "config": sync_down(bucket, CONFIG_PREFIX, os.path.join(work, "config"), client=client),
        "state": sync_down(bucket, STATE_PREFIX, os.path.join(work, "state"), client=client),
        "drafts": sync_down(bucket, DRAFTS_PREFIX, os.path.join(work, "drafts"), newer_than=lookback, client=client),
    }
    log(f"from s3://{bucket}: " + ", ".join(f"{k}={v}" for k, v in down.items()))

    result: dict[str, Any] = {"bucket": bucket, "down": down, "send": flag(env, "RELOAD_SEND")}
    try:
        result["watcher_rc"] = dispatch_watcher.main(watcher_args(work, env))
        result["sender_rc"] = send_drafts.main(sender_args(work, env))
    finally:
        # One second of slack for filesystem timestamp granularity.
        up = {folder: sync_up(bucket, prefix, os.path.join(work, folder), changed_since=started - 1, client=client)
              for folder, prefix in FOLDERS}
        result["up"] = up
        result["seconds"] = round(time.time() - started)
        log(f"to s3://{bucket}: " + ", ".join(f"{k}={v}" for k, v in up.items()) + f" ({result['seconds']}s)")
    return result
