"""AWS Lambda entry point for the due-date alerts: one run per invocation (scheduled every
morning at 8:00 Eastern), with the working files in S3.

Shares the reload suggester's bucket and its ``config/reps.csv`` (one rep list to keep in
sync) but keeps its own prefix for everything else::

    config/reps.csv                         shared with the reload suggester (down only)
    due-alerts/state/alert_state.json       what was alerted and when
    due-alerts/state/rep_cache.json         sales reps read from load pages
    due-alerts/state/sent_log.jsonl         one line per email
    due-alerts/outbox/                      dry-run emails while sending is off
    due-alerts/pages/                       archived Spot Load Map pages (ALERT_SAVE_PAGES=1)

Environment variables::

    ALERT_BUCKET           the bucket (default: RELOAD_BUCKET, else circle-reload-suggestions)
    ALERT_PREFIX           prefix for the files above (default due-alerts/)
    ALERT_PAUSED           "1" skips the run
    ALERT_SEND             "1" sends through SES; otherwise emails go to outbox/
    ALERT_HOURS            alert window (default 48)
    ALERT_REPEAT_HOURS     alert the same load again after this long (default 24)
    ALERT_CC               comma-separated Cc list (default: Justice Cromwell, Stuart Mitchell)
    ALERT_NO_OVERDUE       "1" leaves out loads already past due
    ALERT_SAVE_PAGES       "1" archives the fetched page
    ALERT_WORKDIR          scratch folder (default /tmp/due-alerts)
    MAIL_FROM              verified SES sender (default the vehicles@ group)
    SES_CONFIGURATION_SET  SES configuration set for bounce/complaint events
    TZ                     America/New_York, so "now" matches CargoTel's clock
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

import due_date_alerts  # noqa: E402

DEFAULT_WORKDIR = "/tmp/due-alerts"
DEFAULT_BUCKET = "circle-reload-suggestions"
DEFAULT_PREFIX = "due-alerts/"
CONFIG_PREFIX = "config/"


def alert_args(work: str, env: Mapping[str, str]) -> list[str]:
    args = [
        "--reps", os.path.join(work, "config", "reps.csv"),
        "--state", os.path.join(work, "state", "alert_state.json"),
        "--rep-cache", os.path.join(work, "state", "rep_cache.json"),
        "--sent-log", os.path.join(work, "state", "sent_log.jsonl"),
        "--outbox", os.path.join(work, "outbox"),
        "--from", env.get("MAIL_FROM") or due_date_alerts.DEFAULT_FROM,
        "--hours", env.get("ALERT_HOURS", str(int(due_date_alerts.DEFAULT_HOURS))),
        "--repeat-hours", env.get("ALERT_REPEAT_HOURS", str(int(due_date_alerts.DEFAULT_REPEAT_HOURS))),
    ]
    if env.get("ALERT_CC"):
        args += ["--cc", env["ALERT_CC"]]
    if flag(env, "ALERT_SEND"):
        args.append("--send")
    if flag(env, "ALERT_NO_OVERDUE"):
        args.append("--no-overdue")
    if flag(env, "ALERT_SAVE_PAGES"):
        args += ["--save-html", os.path.join(work, "pages")]
    if env.get("SES_CONFIGURATION_SET"):
        args += ["--configuration-set", env["SES_CONFIGURATION_SET"]]
    return args


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} lambda: {message}", flush=True)


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    env = os.environ
    if flag(env, "ALERT_PAUSED"):
        log("paused (ALERT_PAUSED=1); nothing done")
        return {"paused": True}
    bucket = env.get("ALERT_BUCKET") or env.get("RELOAD_BUCKET") or DEFAULT_BUCKET
    prefix = env.get("ALERT_PREFIX", DEFAULT_PREFIX)
    apply_tz(env)
    work = env.get("ALERT_WORKDIR", DEFAULT_WORKDIR)
    started = time.time()

    shutil.rmtree(work, ignore_errors=True)
    for folder in ("config", "state", "outbox", "pages"):
        os.makedirs(os.path.join(work, folder), exist_ok=True)
    client = s3_client()
    down = {
        "config": sync_down(bucket, CONFIG_PREFIX, os.path.join(work, "config"), client=client),
        "state": sync_down(bucket, prefix + "state/", os.path.join(work, "state"), client=client),
    }
    log(f"from s3://{bucket}: " + ", ".join(f"{k}={v}" for k, v in down.items()))

    result: dict[str, Any] = {"bucket": bucket, "prefix": prefix, "down": down, "send": flag(env, "ALERT_SEND")}
    try:
        result["rc"] = due_date_alerts.main(alert_args(work, env))
    finally:
        up = {folder: sync_up(bucket, prefix + folder + "/", os.path.join(work, folder), changed_since=started - 1, client=client)
              for folder in ("state", "outbox", "pages")}
        result["up"] = up
        result["seconds"] = round(time.time() - started)
        log(f"to s3://{bucket}/{prefix}: " + ", ".join(f"{k}={v}" for k, v in up.items()) + f" ({result['seconds']}s)")
    return result
