# Tracking-update alerts

Checks the GPS pings of every load on Operations Reports > Kesh: Tracking Update Screen,
writes the last ping into the dispatch comments of the loads whose driver is pinging, and
emails the dispatch rep, the sales rep and the sales rep's manager about the loads whose
driver is not. Stellantis loads go to the dispatch rep and `stellantisob@circledelivers.com`.
From `vehicles@circledelivers.com`. Built on the CargoTel scraping and the rep list of the
reload suggester one folder up, and on the email plumbing of `../due_date_alerts`.

| File | What it does |
| --- | --- |
| `tracking_update_alerts.py` | The whole job: pull the screen, read each load's GPS history, comment the pinging loads, email the rest, remember what was done. CLI + importable. |
| `tracking_update_lambda.py` | AWS Lambda entry point: one run per invocation, state in S3 (`tracking-alerts/` prefix of the reload suggester's bucket, sharing its `config/reps.csv`). |
| `deploy.ps1`, `deploy/lambda-policy.json` | Builds the zip and creates or updates the function `circle-tracking-update-alerts`, its roles and the EventBridge schedule. |
| `tests/` | Unit tests on a synthetic screen and synthetic GPS pages. `python -m unittest` from this folder. |

## Where the data comes from

The menu entry Operations Reports > **Kesh: Tracking Update Screen** is saved search
**2077** (created 2026-11-05). Its filter is status Dispatched, carrier added in the last 90
days, bundle parents and individual orders, two customers excluded, and **Pickup ETA
before <date>**, the date being the one the screenshot shows ("or from Nov/7/2026", meaning
the loads with a pickup ETA earlier than that day). That date is supposed to move with the
calendar: today + 2 days (Nov 8 on Nov 6, Nov 9 on Nov 7, ...).

CargoTel will not take that filter from a URL: sending `Menu__DateMne_PickupETA=Before`
with any dates crashes the page (`Can't locate object method "hour" via package "Before"`,
the same bug the duplicate check hit with "Last 90 days"). Sending **only the date parts**
(`Menu__month/day/year_DateStart_PickupETA`) makes CargoTel drop the Pickup ETA filter
altogether and return the rest of the saved filter (175 Dispatched rows on 2026-11-06, 93
with the saved date). The job does that and applies the date rule itself: pickup ETA
before `today + --days-ahead` at midnight. Verified 2026-11-06: filtering the unfiltered
pull by "before Nov 7" gives exactly the 93 rows of the saved screen; "before Nov 8" gives
126 (130 by 09:08, as loads were added).

The grid columns the job reads (the saved view, plus the extras it asks for with
`Menu__ViewFields`):

| Column | Key | What is in it |
| --- | --- | --- |
| Dispatch Rep | `dispatch_rep` | `KALEB HAMMAN` |
| Order ID | `load_id` | the load (bundle parents and single orders; members are skipped) |
| Pickup ETA | `pickiup_eta_datetime` | `11/06/2026 20:00` (sic, CargoTel's spelling) |
| GPS City/St Link | `GPSLink` | `/backoffice/gpshistory.mcgi?load_id=<id>` |
| Current Location | `currentlocation` | CargoTel's own idea, shown in the email |
| Dispatch Comment (Add) | `DispatchCmt_IDAdd` | a textarea `Control__DispatchCmt_ID__<id>__comment` inside the page's `Menu_alt` form |
| Last Dispatch Comment | `DispatchCmt_IDLastCmt` | `2026-11-05 15:14 EST / JUSTICE CROMWELL / 2026-11-05 10:19:29-05 Burnsville, MN`: the last comment's time, author and text |
| Sales Rep 1 / 2 | `sales_rep1`, `sales_rep2` | added on the GET |
| Original Consignor, Customer ID#, Customer | `original_consignor`, `cons_id`, `consignor` | added on the GET. `consignor` reads **N/A** on every EDI load (all Stellantis loads, Kingbee, Enterprise, USPS ...); `original_consignor` carries the real customer and `cons_id` its id. Stellantis is customer id **62943** ("Stellantis (fca)"); 37 of the 93 rows on 2026-11-06. |

The GPS page (`gpshistory.mcgi?load_id=<id>`) is a plain table, oldest ping first: user
name, timestamp with UTC offset (`2026-11-06 06:12:25-05`), city/state (`Oak Lawn,IL`),
cell coverage. Its default window is the last 48 hours, and that is the window the job
asks for too (`--gps-days` 2, sent through the form's date selects,
`gpshistory_form__month_start_date` ..., which work as GET parameters), so a load reads
"no GPS pings" exactly when the link shows an empty table. A 14-day window was tried
first and turned up pings from before the pickup on loads whose link showed nothing
(350364: last ping Oct 29, pickup Oct 30, dispatched Oct 28), which the team read as wrong.

## How it works

```
Kesh: Tracking Update Screen (2077), Pickup ETA filter dropped, rep + customer columns added
   │  status Dispatched (--status), pickup ETA before today + 2 days (--days-ahead),
   │  minus the excluded customers (--exclude-customer: Merchants Automotive)
   ▼
GPS history per load (48 h, as the  active   last ping within 4 h (--stale-hours)   -> last ping written as a dispatch comment
link shows it)                       stale    last ping older than 4 h               -> last ping written as a comment (once) AND email
                                     never    no ping in the window                  -> email
                                     unknown  page unreadable                        -> logged only
   ▼
one email per group                  Stellantis (customer 62943): per dispatch rep, To dispatch rep + stellantisob@
                                     others: per (dispatch rep, sales rep), To both, Cc the sales rep's manager
                                     (reps.csv) + secondary sales rep; same recipients -> one email;
                                     nobody addressable -> To the escalation list, reps named per load
   ▼
alert_state.json, comment_state.json a load repeats after 4 h (--repeat-hours) while still not pinging, or at once
                                     when its state or owners change; a ping is written as a comment once
```

The comment is the ping as CargoTel prints it plus the place, `2026-11-06 06:12:25-05
Oak Lawn, IL`, the format Justice already types by hand (seen on load 352242). It is
written for every load that has a ping, whether the driver is still pinging or went
quiet more than 4 hours ago (asked for on 2026-11-06: a quiet driver's last known position
belongs on the load too, and the load is still in the alert). It is skipped when the load's
last dispatch comment already contains that ping's timestamp, by anyone, or when
`comment_state.json` says the job wrote it, so a quiet driver's unchanged last ping is
written once, not every hour.

Each email has a plain-text and an HTML part with one section per state ("No GPS pings",
"GPS stopped more than 4 h ago"), its instruction, and a table per section: load, customer,
lane, carrier and driver with the driver's cell, pickup ETA, last GPS ping (time, place,
how long ago, link to the GPS history), CargoTel's current location, and the last dispatch
comment.

## Running it

```bash
# the loads and their GPS state, nothing composed (reads ~130 GPS pages, about 3 minutes)
python tracking_update_alerts.py --list

# dry run: emails to outbox/, the comments that would be added to outbox/<stamp>_comments.txt; no state touched
python tracking_update_alerts.py

# comments into CargoTel, emails still to the outbox
python tracking_update_alerts.py --post-comments

# live
python tracking_update_alerts.py --send --post-comments

# show someone the emails first: every email goes to you, marked as a test copy, nothing recorded
python tracking_update_alerts.py --send --redirect-to you@circledelivers.com

# a test on chosen loads only: one comment, or one group's email as a test copy
python tracking_update_alerts.py --post-comments --only-load 352242
python tracking_update_alerts.py --send --redirect-to justice.cromwell@circledelivers.com --only-load 350364,350271,354448,349850

# offline, against archived pages (--save-html DIR archives them: DIR/grid_*.html and DIR/gps/gps_<load>.html)
python tracking_update_alerts.py --from-html pages/grid_20261106-0930.html --gps-dir pages/gps --now 2026-11-06T09:30:00-05:00
```

Needs the AWS SSO session (`aws sso login`) for the CargoTel cookie in S3 and, with
`--send`, SES rights on the vehicles@ identity. `--reps` defaults to `../reps.csv`; that
file now takes a fifth column, **manager** (an email address), which is who gets Cc'd on
the sales rep's alerts. A sales rep without one is noted in the email (or Cc'd to
`--default-manager`).

Switches worth knowing: `--days-ahead` (2), `--stale-hours` (4), `--gps-days` (2),
`--exclude-customer` (Merchants Automotive, 81464),
`--repeat-hours` (4; `0` = every run), `--status` (`Dispatched`), `--stellantis-to`,
`--escalate-to` (default Justice Cromwell and Stuart Mitchell), `--default-manager`,
`--only-rep NAME` (`stellantis`, `unassigned` work too), `--only-load ID[,ID]` (the whole run
limited to those loads), `--max-gps` (400), `--save-html DIR`, `--now ISO`, `--json FILE`.

## What was verified (2026-11-06)

- The pull, the local date rule, the GPS pages with a widened window and the parsing of
  both were checked against the live portal (09:08 ET, 14-day window: 181 rows, 130 inside
  the date rule, 20 pinging, 56 quiet, 54 never pinged; 10:53 ET, after the window was
  brought down to the link's own 48 hours: 198 rows, 127 inside the rule, 26 pinging, 34
  quiet, 67 never pinged, 0 unreadable either time).
- **Adding a dispatch comment works** when the grid's `Menu_alt` form is posted whole,
  exactly as a browser does (every hidden field, every row's widgets with their current
  values, the comment boxes filled, the Submit button). A first attempt that sent only
  the filter fields plus one box came back as a normal page with nothing saved, so do not
  trim the form. Test at 09:18 ET: `--post-comments --only-load 352242` wrote
  `2026-11-06 09:16:36-05 Glenwood, IL` on load 352242; it shows as the load's Last
  Dispatch Comment (author ALEC STOPHER, the bot cookie's user) and in the load page's
  Comment History as a "Dispatch" comment. The screen was re-read and compared row by
  row with the page saved before the post: no other load's last comment, ETA widget
  values, pickup ETA, current location or status changed. After every post the job
  re-reads the screen and only records the comments that show up, logging `comment NOT
  found on <load>` otherwise.
- **One test email went out** at 09:18 ET through SES from vehicles@: the Kyle Link
  group (loads 350364, 349850, 350271, 354448: Kyle as dispatch and sales rep, with
  `--default-manager` standing in for the empty manager column) sent as a marked test
  copy to Justice Cromwell only (`--send --redirect-to`), SES id
  `010001a1b139f420-6f13228a-...`. Nothing was recorded in the state.
- Live on AWS since 11:01 ET the same day (see below).

## Decisions baked in

- **Merchants Automotive Group Holdings LLC (customer id 81464) is left out of the run**,
  comments and emails alike, at the user's request (2026-11-06). `--exclude-customer` /
  `TRACK_EXCLUDE_CUSTOMERS` take customer ids or parts of names; `""` (CLI) or `-` (Lambda)
  switch the exclusion off.
- **"Dispatcher" means the load's dispatch rep** (the Dispatch Rep column), as in the
  pickup-ETA job, not the carrier's dispatcher.
- **Stellantis alerts go to the dispatch rep and the Stellantis mailbox only**, as asked;
  the sales rep and manager are not on them.
- **The manager comes from reps.csv** (new `manager` column, an email). Nobody has one
  yet; until they do, the sales rep's email says "no manager set for X in reps.csv".
- **Dispatch reps missing from reps.csv** on 2026-11-06: Anthony Stavale, Daniel Merlano,
  Joseph Camilleri, Juan Bernal (not in TransportPro either) and Podge Ormsby
  (`pormsby@circledelivers.com` per his TransportPro user record, not added since the
  other jobs would start emailing him too). Their loads still go out: to the sales rep
  (+ manager), or for Stellantis to the mailbox alone, with the rep named in the email.
- **Repeat every 4 hours** while a load stays stale (the staleness threshold), not once a
  day: a driver who is not pinging is a same-day problem. Change with `--repeat-hours`.
- **A ping is written once.** The job compares the ping's timestamp with the load's last
  dispatch comment and with its own record, so running every hour does not fill the
  comments with the same position. That is what keeps the quiet drivers' comments to one
  per last ping while their alerts repeat every 4 hours.
- **A load whose GPS page cannot be read is never emailed or commented**; it is logged
  and counted as unreadable.
- **Loads with no pickup ETA are not on the screen** (the saved filter excludes them), so
  the job leaves them out too.
- **Dry runs leave the state alone**; `--redirect-to` test copies are never recorded.

## On AWS (live since 2026-11-06 11:01 ET)

```powershell
.\tracking_update_alerts\deploy.ps1                       # stage one: runs on the schedule, everything to s3://.../tracking-alerts/outbox/
.\tracking_update_alerts\deploy.ps1 -PostComments         # comments into CargoTel, emails still to the outbox
.\tracking_update_alerts\deploy.ps1 -Send -PostComments   # live
.\tracking_update_alerts\deploy.ps1 -CodeOnly             # code change only
.\tracking_update_alerts\deploy.ps1 -Paused               # kill switch
```

Function `circle-tracking-update-alerts` (Python 3.12, 512 MB, 15 min, concurrency 1,
`TRACK_SEND=1`, `TRACK_POST_COMMENTS=1`), schedule `cron(0 6-21 * * ? *)` America/New_York:
every hour on the hour from 06:00 to 21:00 Eastern, seven days a week ("hourly" was the
request; the night hours were left out because drivers park overnight, so those runs would
only mail reps about sleeping drivers; `-Schedule "cron(0 * * * ? *)"` makes it round the
clock). Deployed with `deploy.ps1 -Send -PostComments` at 11:00 ET on 2026-11-06 and run
once by hand straight away: 200 loads on the screen, 21 Merchants loads left out, 113
checked, 28 pinging (28 comments written, 0 failed), 33 quiet, 52 never pinged, 6 emails
sent (Kaleb, Jack and the no-address dispatch reps' Stellantis loads to stellantisob@; Kyle;
Victoria; 10 loads with nobody addressable to Justice and Stuart). A run takes about 7.5
minutes: 2 minutes of GPS pages and 5 minutes for CargoTel to take the comment form and
re-render the screen. Bucket `circle-reload-suggestions`:

| Key | What |
| --- | --- |
| `config/reps.csv` | shared with the other jobs; add the manager column there too |
| `tracking-alerts/state/alert_state.json` | which loads were emailed and when |
| `tracking-alerts/state/comment_state.json` | which ping was written on each load |
| `tracking-alerts/state/sent_log.jsonl` | one line per email |
| `tracking-alerts/outbox/` | dry-run emails and planned comments |
| `tracking-alerts/pages/` | archived pages with `TRACK_SAVE_PAGES=1` |

Environment variables: `TRACK_BUCKET`, `TRACK_PREFIX`, `TRACK_PAUSED`, `TRACK_SEND`,
`TRACK_POST_COMMENTS`, `TRACK_DAYS_AHEAD`, `TRACK_STALE_HOURS`, `TRACK_GPS_DAYS`,
`TRACK_REPEAT_HOURS`, `TRACK_STATUS`, `TRACK_EXCLUDE_CUSTOMERS`, `TRACK_STELLANTIS_TO`, `TRACK_ESCALATE_TO`,
`TRACK_DEFAULT_MANAGER`, `TRACK_SAVE_PAGES`, `MAIL_FROM`, `SES_CONFIGURATION_SET`, `TZ`.
The role may read the cookie object and `config/`, read and write `tracking-alerts/`, and
send as vehicles@.
