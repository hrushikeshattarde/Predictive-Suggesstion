"""AWS Lambda entry point for the sales rep sync: one run per invocation, with the working
files in S3.

Shares the reload suggester's bucket but keeps its own prefix::

    sales-rep-sync/state/changes.jsonl     one line per load changed (or failed) by the bot
    sales-rep-sync/reports/                the JSON report of every run with SRS_APPLY=1
    sales-rep-sync/outbox/                 dry-run reports while applying is off
    sales-rep-sync/pages/                  archived pages and POST responses (SRS_SAVE_PAGES=1)

Environment variables::

    SRS_BUCKET               the bucket (default: RELOAD_BUCKET, else circle-reload-suggestions)
    SRS_PREFIX               prefix for the files above (default sales-rep-sync/)
    SRS_PAUSED               "1" skips the run
    SRS_APPLY                "1" presses the bulk widget in CargoTel; otherwise a dry run
    SRS_OVERWRITE            "1" also changes loads that carry a different rep (default: fill empty only)
    SRS_MAX_CHANGES          loads changed per run at most (default 200; -1 = no cap)
    SRS_SAVE_PAGES           "1" archives the fetched pages and every POST response
    SRS_WORKDIR              scratch folder (default /tmp/sales-rep-sync)
    TZ                       America/New_York, so "now" matches CargoTel's clock

Event keys (for a manual invoke)::

    {"dry_run": true}                   plan only, whatever SRS_APPLY says
    {"only_load": "354657,354704"}      only these order ids
"""

from __future__ import annotations

import os
import shutil
import sys
import time
from datetime import datetime
from typing import Any, Mapping

HERE = os.path.dirname(os.path.abspath(__file__))
PARENT = os.path.dirname(HERE)
if PARENT not in sys.path:
    sys.path.insert(0, PARENT)

from lambda_handler import apply_tz, flag, s3_client, sync_down, sync_up  # noqa: E402

import sales_rep_sync  # noqa: E402

DEFAULT_WORKDIR = "/tmp/sales-rep-sync"
DEFAULT_BUCKET = "circle-reload-suggestions"
DEFAULT_PREFIX = "sales-rep-sync/"
FOLDERS = ("state", "reports", "outbox", "pages")


def sync_args(work: str, env: Mapping[str, str], now: datetime, event: Mapping[str, Any] | None = None) -> list[str]:
    event = event or {}
    args = [
        "--outbox", os.path.join(work, "outbox"),
        "--reports", os.path.join(work, "reports"),
        "--changes-log", os.path.join(work, "state", "changes.jsonl"),
        "--max-changes", env.get("SRS_MAX_CHANGES", str(sales_rep_sync.DEFAULT_MAX_CHANGES)),
    ]
    if flag(env, "SRS_APPLY") and not event.get("dry_run"):
        args.append("--apply")
    if flag(env, "SRS_OVERWRITE"):
        args.append("--overwrite")
    if flag(env, "SRS_SAVE_PAGES"):
        args += ["--save-pages", os.path.join(work, "pages")]
    if event.get("only_load"):
        args += ["--only-load", str(event["only_load"])]
    return args


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} lambda: {message}", flush=True)


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    env = os.environ
    if flag(env, "SRS_PAUSED"):
        log("paused (SRS_PAUSED=1); nothing done")
        return {"paused": True}
    bucket = env.get("SRS_BUCKET") or env.get("RELOAD_BUCKET") or DEFAULT_BUCKET
    prefix = env.get("SRS_PREFIX", DEFAULT_PREFIX)
    apply_tz(env)
    work = env.get("SRS_WORKDIR", DEFAULT_WORKDIR)
    started = time.time()
    now = datetime.now()

    shutil.rmtree(work, ignore_errors=True)
    for folder in FOLDERS:
        os.makedirs(os.path.join(work, folder), exist_ok=True)
    client = s3_client()
    down = {"state": sync_down(bucket, prefix + "state/", os.path.join(work, "state"), client=client)}
    log(f"from s3://{bucket}/{prefix}: " + ", ".join(f"{k}={v}" for k, v in down.items()))

    args = sync_args(work, env, now, event)
    result: dict[str, Any] = {"bucket": bucket, "prefix": prefix, "down": down, "apply": "--apply" in args}
    try:
        result["rc"] = sales_rep_sync.main(args)
    finally:
        up = {folder: sync_up(bucket, prefix + folder + "/", os.path.join(work, folder), changed_since=started - 1, client=client)
              for folder in FOLDERS}
        result["up"] = up
        result["seconds"] = round(time.time() - started)
        log(f"to s3://{bucket}/{prefix}: " + ", ".join(f"{k}={v}" for k, v in up.items()) + f" ({result['seconds']}s)")
    return result
