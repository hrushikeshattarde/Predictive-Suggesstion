"""AWS Lambda entry point for the weekly duplicate-order check: one run per invocation,
with the working files in S3.

Shares the reload suggester's bucket but keeps its own prefix::

    duplicate-check/state/sent_log.jsonl      one line per email
    duplicate-check/reports/                  the CSV of every run
    duplicate-check/outbox/                   dry-run emails while sending is off
    duplicate-check/pages/                    archived grids as JSON (DUP_SAVE_GRID=1)

Environment variables::

    DUP_BUCKET               the bucket (default: RELOAD_BUCKET, else circle-reload-suggestions)
    DUP_PREFIX               prefix for the files above (default duplicate-check/)
    DUP_PAUSED               "1" skips the run
    DUP_SEND                 "1" sends through SES; otherwise emails go to outbox/
    DUP_DAYS                 the local window in days (default 7; 0 = the saved search's 90 days)
    DUP_EXPECT_WINDOW        the window the saved search must apply (default "Last 90 days")
    DUP_TO                   comma-separated recipients (default Justice Cromwell)
    DUP_CC                   comma-separated Cc list (default none)
    DUP_SKIP_EMPTY           "1" sends nothing when no duplicate was found
    DUP_SAVE_GRID            "1" archives the fetched grid as JSON
    DUP_WORKDIR              scratch folder (default /tmp/duplicate-check)
    MAIL_FROM                verified SES sender (default the vehicles@ group)
    SES_CONFIGURATION_SET    SES configuration set for bounce/complaint events
    TZ                       America/New_York, so "now" matches CargoTel's clock
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

import duplicate_check  # noqa: E402

DEFAULT_WORKDIR = "/tmp/duplicate-check"
DEFAULT_BUCKET = "circle-reload-suggestions"
DEFAULT_PREFIX = "duplicate-check/"


def check_args(work: str, env: Mapping[str, str], now: datetime) -> list[str]:
    args = [
        "--days", env.get("DUP_DAYS", str(duplicate_check.DEFAULT_DAYS)),
        "--expect-window", env.get("DUP_EXPECT_WINDOW", duplicate_check.DEFAULT_WINDOW),
        "--out", os.path.join(work, "reports", f"duplicate_orders_{now:%Y-%m-%d}.csv"),
        "--sent-log", os.path.join(work, "state", "sent_log.jsonl"),
        "--outbox", os.path.join(work, "outbox"),
        "--from", env.get("MAIL_FROM") or duplicate_check.DEFAULT_FROM,
        "--to", env.get("DUP_TO") or ",".join(duplicate_check.DEFAULT_TO),
        "--cc", env.get("DUP_CC") or ",".join(duplicate_check.DEFAULT_CC),
    ]
    if flag(env, "DUP_SEND"):
        args.append("--send")
    if flag(env, "DUP_SKIP_EMPTY"):
        args.append("--skip-empty")
    if flag(env, "DUP_SAVE_GRID"):
        args += ["--save-json", os.path.join(work, "pages", f"grid_{now:%Y-%m-%d_%H%M}.json")]
    if env.get("SES_CONFIGURATION_SET"):
        args += ["--configuration-set", env["SES_CONFIGURATION_SET"]]
    return args


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} lambda: {message}", flush=True)


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    env = os.environ
    if flag(env, "DUP_PAUSED"):
        log("paused (DUP_PAUSED=1); nothing done")
        return {"paused": True}
    bucket = env.get("DUP_BUCKET") or env.get("RELOAD_BUCKET") or DEFAULT_BUCKET
    prefix = env.get("DUP_PREFIX", DEFAULT_PREFIX)
    apply_tz(env)
    work = env.get("DUP_WORKDIR", DEFAULT_WORKDIR)
    started = time.time()
    now = datetime.now()

    shutil.rmtree(work, ignore_errors=True)
    for folder in ("state", "reports", "outbox", "pages"):
        os.makedirs(os.path.join(work, folder), exist_ok=True)
    client = s3_client()
    down = {"state": sync_down(bucket, prefix + "state/", os.path.join(work, "state"), client=client)}
    log(f"from s3://{bucket}/{prefix}: " + ", ".join(f"{k}={v}" for k, v in down.items()))

    result: dict[str, Any] = {"bucket": bucket, "prefix": prefix, "down": down, "send": flag(env, "DUP_SEND")}
    try:
        result["rc"] = duplicate_check.main(check_args(work, env, now))
    finally:
        up = {folder: sync_up(bucket, prefix + folder + "/", os.path.join(work, folder), changed_since=started - 1, client=client)
              for folder in ("state", "reports", "outbox", "pages")}
        result["up"] = up
        result["seconds"] = round(time.time() - started)
        log(f"to s3://{bucket}/{prefix}: " + ", ".join(f"{k}={v}" for k, v in up.items()) + f" ({result['seconds']}s)")
    return result
