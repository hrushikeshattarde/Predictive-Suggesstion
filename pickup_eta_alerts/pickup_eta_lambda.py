"""AWS Lambda entry point for the pickup-ETA alerts: one run per invocation, with the
working files in S3.

Shares the reload suggester's bucket and its ``config/reps.csv`` (one rep list to keep in
sync) but keeps its own prefix for everything else::

    config/reps.csv                           shared with the other jobs (down only)
    pickup-alerts/state/alert_state.json      what was alerted and when
    pickup-alerts/state/rep_cache.json        sales reps read from load pages (fallback path)
    pickup-alerts/state/customer_reps.json    sales reps seen per customer, for loads that have none
    pickup-alerts/state/sent_log.jsonl        one line per email
    pickup-alerts/outbox/                     dry-run emails while sending is off
    pickup-alerts/pages/                      archived tracking pages (PICKUP_SAVE_PAGES=1)

Environment variables::

    PICKUP_BUCKET            the bucket (default: RELOAD_BUCKET, else circle-reload-suggestions)
    PICKUP_PREFIX            prefix for the files above (default pickup-alerts/)
    PICKUP_PAUSED            "1" skips the run
    PICKUP_SEND              "1" sends through SES; otherwise emails go to outbox/
    PICKUP_REPEAT_HOURS      alert the same load again after this long (default 24)
    PICKUP_MIN_AGE_HOURS     leave a load alone this long after its status date (default 1)
    PICKUP_STATUS            comma-separated statuses to check (default Dispatched,In-Route)
    PICKUP_SKIP_SCHED_CHECK  "1" does not flag loads with no scheduled pickup date
    PICKUP_SKIP_STALE_CHECK  "1" does not flag loads whose pickup ETA has passed
    PICKUP_STALE_ETA_HOURS   hours after the pickup ETA before a load is flagged as past it (default 2)
    PICKUP_STALE_STATUSES    comma-separated statuses the passed-ETA rule applies to (default Dispatched)
    PICKUP_INCLUDE_SAME_DAY  "1" also alerts loads whose pickup ETA or scheduled pickup is today
    PICKUP_NO_INFER_SALES_REP "1" leaves a missing sales rep empty instead of taking the customer's usual one
    PICKUP_CC                comma-separated Cc list (default: Justice Cromwell, Stuart Mitchell)
    PICKUP_SAVE_PAGES        "1" archives the fetched page
    PICKUP_WORKDIR           scratch folder (default /tmp/pickup-alerts)
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

import pickup_eta_alerts  # noqa: E402

DEFAULT_WORKDIR = "/tmp/pickup-alerts"
DEFAULT_BUCKET = "circle-reload-suggestions"
DEFAULT_PREFIX = "pickup-alerts/"
CONFIG_PREFIX = "config/"


def alert_args(work: str, env: Mapping[str, str]) -> list[str]:
    args = [
        "--reps", os.path.join(work, "config", "reps.csv"),
        "--state", os.path.join(work, "state", "alert_state.json"),
        "--rep-cache", os.path.join(work, "state", "rep_cache.json"),
        "--customer-reps", os.path.join(work, "state", "customer_reps.json"),
        "--sent-log", os.path.join(work, "state", "sent_log.jsonl"),
        "--outbox", os.path.join(work, "outbox"),
        "--from", env.get("MAIL_FROM") or pickup_eta_alerts.DEFAULT_FROM,
        "--repeat-hours", env.get("PICKUP_REPEAT_HOURS", str(int(pickup_eta_alerts.DEFAULT_REPEAT_HOURS))),
        "--min-age-hours", env.get("PICKUP_MIN_AGE_HOURS", str(int(pickup_eta_alerts.DEFAULT_MIN_AGE_HOURS))),
        "--stale-eta-hours", env.get("PICKUP_STALE_ETA_HOURS", str(int(pickup_eta_alerts.DEFAULT_STALE_ETA_HOURS))),
    ]
    if env.get("PICKUP_STATUS"):
        args += ["--status", env["PICKUP_STATUS"]]
    if env.get("PICKUP_STALE_STATUSES"):
        args += ["--stale-statuses", env["PICKUP_STALE_STATUSES"]]
    if env.get("PICKUP_CC"):
        args += ["--cc", env["PICKUP_CC"]]
    if flag(env, "PICKUP_SEND"):
        args.append("--send")
    if flag(env, "PICKUP_SKIP_SCHED_CHECK"):
        args.append("--skip-sched-check")
    if flag(env, "PICKUP_SKIP_STALE_CHECK"):
        args.append("--skip-stale-check")
    if flag(env, "PICKUP_INCLUDE_SAME_DAY"):
        args.append("--include-same-day")
    if flag(env, "PICKUP_NO_INFER_SALES_REP"):
        args.append("--no-infer-sales-rep")
    if flag(env, "PICKUP_SAVE_PAGES"):
        args += ["--save-html", os.path.join(work, "pages")]
    if env.get("SES_CONFIGURATION_SET"):
        args += ["--configuration-set", env["SES_CONFIGURATION_SET"]]
    return args


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} lambda: {message}", flush=True)


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    env = os.environ
    if flag(env, "PICKUP_PAUSED"):
        log("paused (PICKUP_PAUSED=1); nothing done")
        return {"paused": True}
    bucket = env.get("PICKUP_BUCKET") or env.get("RELOAD_BUCKET") or DEFAULT_BUCKET
    prefix = env.get("PICKUP_PREFIX", DEFAULT_PREFIX)
    apply_tz(env)
    work = env.get("PICKUP_WORKDIR", DEFAULT_WORKDIR)
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

    result: dict[str, Any] = {"bucket": bucket, "prefix": prefix, "down": down, "send": flag(env, "PICKUP_SEND")}
    try:
        result["rc"] = pickup_eta_alerts.main(alert_args(work, env))
    finally:
        up = {folder: sync_up(bucket, prefix + folder + "/", os.path.join(work, folder), changed_since=started - 1, client=client)
              for folder in ("state", "outbox", "pages")}
        result["up"] = up
        result["seconds"] = round(time.time() - started)
        log(f"to s3://{bucket}/{prefix}: " + ", ".join(f"{k}={v}" for k, v in up.items()) + f" ({result['seconds']}s)")
    return result
