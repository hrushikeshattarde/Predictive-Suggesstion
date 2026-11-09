"""AWS Lambda entry point for the tracking-update alerts: one run per invocation, with
the working files in S3.

Shares the reload suggester's bucket and its ``config/reps.csv`` (one rep list to keep in
sync; its ``manager`` column feeds this job) but keeps its own prefix::

    config/reps.csv                              shared with the other jobs (down only)
    tracking-alerts/state/alert_state.json       which loads were emailed and when
    tracking-alerts/state/comment_state.json     which ping was written on each load
    tracking-alerts/state/sent_log.jsonl         one line per email
    tracking-alerts/outbox/                      dry-run emails and planned comments
    tracking-alerts/pages/                       archived grid and GPS pages (TRACK_SAVE_PAGES=1)

Environment variables::

    TRACK_BUCKET            the bucket (default: RELOAD_BUCKET, else circle-reload-suggestions)
    TRACK_PREFIX            prefix for the files above (default tracking-alerts/)
    TRACK_PAUSED            "1" skips the run
    TRACK_SEND              "1" sends through SES; otherwise emails go to outbox/
    TRACK_POST_COMMENTS     "1" adds the dispatch comments in CargoTel; otherwise they go to outbox/
    TRACK_DAYS_AHEAD        pickup ETA before today + this many days (default 2)
    TRACK_STALE_HOURS       a last ping older than this is "not pinging" (default 4)
    TRACK_GPS_DAYS          GPS history window in days (default 2, what the GPS link itself shows)
    TRACK_REPEAT_HOURS      alert the same load again after this long (default 4)
    TRACK_STATUS            comma-separated statuses (default Dispatched)
    TRACK_EXCLUDE_CUSTOMERS customers left out of the run, by id or part of the name (default Merchants Automotive; "-" for none)
    TRACK_STELLANTIS_TO     comma-separated addresses for Stellantis alerts (default stellantisob@)
    TRACK_ESCALATE_TO       comma-separated addresses when neither rep can be emailed
    TRACK_DEFAULT_MANAGER   comma-separated Cc for sales reps with no manager in reps.csv
    TRACK_REPORT_TO         comma-separated addresses for the daily GPS report (default Justice Cromwell; "-" for nobody)
    TRACK_REPORT_HOURS      Eastern hour(s) whose run sends the report: "10,16" (default), "8", "all", "-" (never)
    TRACK_REPORT_QUIET_HOURS  the report lists drivers quiet for longer than this (default 11)
    TRACK_REPORT_STATUS     statuses scanned for the report only, no comments or alerts (default In-Route; "-" for none)
    TRACK_SAVE_PAGES        "1" archives the fetched pages
    TRACK_WORKDIR           scratch folder (default /tmp/tracking-alerts)
    MAIL_FROM               verified SES sender (default the vehicles@ group)
    SES_CONFIGURATION_SET   SES configuration set for bounce/complaint events
    TZ                      America/New_York, so "now" matches CargoTel's clock

An event of ``{"report": true}`` (``aws lambda invoke --payload``) sends the report from
that run whatever the hour; everything else in the event is ignored.
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

import tracking_update_alerts  # noqa: E402

DEFAULT_WORKDIR = "/tmp/tracking-alerts"
DEFAULT_BUCKET = "circle-reload-suggestions"
DEFAULT_PREFIX = "tracking-alerts/"
CONFIG_PREFIX = "config/"


def alert_args(work: str, env: Mapping[str, str], event: Mapping[str, Any] | None = None) -> list[str]:
    args = [
        "--reps", os.path.join(work, "config", "reps.csv"),
        "--state", os.path.join(work, "state", "alert_state.json"),
        "--comment-state", os.path.join(work, "state", "comment_state.json"),
        "--sent-log", os.path.join(work, "state", "sent_log.jsonl"),
        "--outbox", os.path.join(work, "outbox"),
        "--from", env.get("MAIL_FROM") or tracking_update_alerts.DEFAULT_FROM,
        "--days-ahead", env.get("TRACK_DAYS_AHEAD", str(tracking_update_alerts.DEFAULT_DAYS_AHEAD)),
        "--stale-hours", env.get("TRACK_STALE_HOURS", str(int(tracking_update_alerts.DEFAULT_STALE_HOURS))),
        "--gps-days", env.get("TRACK_GPS_DAYS", str(tracking_update_alerts.DEFAULT_GPS_DAYS)),
        "--repeat-hours", env.get("TRACK_REPEAT_HOURS", str(int(tracking_update_alerts.DEFAULT_REPEAT_HOURS))),
    ]
    for option, name in (("--status", "TRACK_STATUS"), ("--stellantis-to", "TRACK_STELLANTIS_TO"),
                         ("--escalate-to", "TRACK_ESCALATE_TO"), ("--default-manager", "TRACK_DEFAULT_MANAGER"),
                         ("--configuration-set", "SES_CONFIGURATION_SET")):
        if env.get(name):
            args += [option, env[name]]
    # "-" switches a default off (an empty Lambda variable reads as unset).
    for option, name in (("--exclude-customer", "TRACK_EXCLUDE_CUSTOMERS"), ("--report-to", "TRACK_REPORT_TO"),
                         ("--report-hours", "TRACK_REPORT_HOURS"), ("--report-status", "TRACK_REPORT_STATUS")):
        if env.get(name):
            args += [option, "" if env[name].strip() == "-" else env[name]]
    if env.get("TRACK_REPORT_QUIET_HOURS"):
        args += ["--report-quiet-hours", env["TRACK_REPORT_QUIET_HOURS"]]
    if isinstance(event, Mapping) and str(event.get("report", "")).strip().lower() in ("1", "true", "yes", "on"):
        args.append("--report")
    if flag(env, "TRACK_SEND"):
        args.append("--send")
    if flag(env, "TRACK_POST_COMMENTS"):
        args.append("--post-comments")
    if flag(env, "TRACK_SAVE_PAGES"):
        args += ["--save-html", os.path.join(work, "pages")]
    return args


def log(message: str) -> None:
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} lambda: {message}", flush=True)


def handler(event: dict[str, Any] | None = None, context: Any = None) -> dict[str, Any]:
    env = os.environ
    if flag(env, "TRACK_PAUSED"):
        log("paused (TRACK_PAUSED=1); nothing done")
        return {"paused": True}
    bucket = env.get("TRACK_BUCKET") or env.get("RELOAD_BUCKET") or DEFAULT_BUCKET
    prefix = env.get("TRACK_PREFIX", DEFAULT_PREFIX)
    apply_tz(env)
    work = env.get("TRACK_WORKDIR", DEFAULT_WORKDIR)
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

    result: dict[str, Any] = {"bucket": bucket, "prefix": prefix, "down": down, "send": flag(env, "TRACK_SEND"),
                              "post_comments": flag(env, "TRACK_POST_COMMENTS")}
    def checkpoint() -> None:
        # The emails are out and the alert state is saved: copy it up now, so a timeout in the
        # comment phase (the slow part; the Lambda is killed and ``finally`` never runs) cannot
        # lose it and repeat the alerts next hour.
        n = sync_up(bucket, prefix + "state/", os.path.join(work, "state"), changed_since=started - 1, client=client)
        log(f"checkpoint: state={n} to s3://{bucket}/{prefix}state/")

    try:
        result["rc"] = tracking_update_alerts.main(alert_args(work, env, event), checkpoint=checkpoint)
    finally:
        up = {folder: sync_up(bucket, prefix + folder + "/", os.path.join(work, folder), changed_since=started - 1, client=client)
              for folder in ("state", "outbox", "pages")}
        result["up"] = up
        result["seconds"] = round(time.time() - started)
        log(f"to s3://{bucket}/{prefix}: " + ", ".join(f"{k}={v}" for k, v in up.items()) + f" ({result['seconds']}s)")
    return result
