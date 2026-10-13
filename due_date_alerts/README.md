# Due-date alerts

Emails each sales rep the loads on the Spot Load Map that are due within 48 hours, from
`vehicles@circledelivers.com`, Cc Justice Cromwell and Stuart Mitchell. Built on the
CargoTel scraping and the rep list of the reload suggester one folder up.

| File | What it does |
| --- | --- |
| `due_date_alerts.py` | The whole job: pull the Spot Load Map, keep the loads due within the window, read each one's sales rep from its load page, group by rep, send one email per rep (or write it to `outbox/`), remember what was sent. CLI + importable. |
| `alerts_lambda.py` | AWS Lambda entry point: one run per invocation, state in S3 (`due-alerts/` prefix of the reload suggester's bucket, sharing its `config/reps.csv`). |
| `deploy.ps1`, `deploy/lambda-policy.json` | Builds the zip (the shared modules plus these two) and creates or updates the function `circle-due-date-alerts`, its roles and the EventBridge schedule. |
| `tests/` | Unit tests on a synthetic Spot Load Map page. `python -m unittest` from this folder. |

## How it works

```
Spot Load Map (saved search 1786, all rows)
   │  Due Date column: "10/16/2026 11:33 *Calculated" or "10/16/2026 00:00"
   ▼
loads with due date <= now + 48 h          (past-due loads included; --no-overdue drops them)
   │
   ▼
sales rep per load                         loadmaint.mcgi -> sales_rep / sales_rep2 / ass_user
   │                                       cached 12 h in rep_cache.json (a page takes 3-4 s)
   ▼
one email per primary sales rep            To the rep (reps.csv), Cc Justice + Stuart (+ secondary rep)
   │                                       no rep on the load, or rep not in reps.csv -> To Justice + Stuart
   ▼
alert_state.json                           a load repeats after 24 h, or at once if its due date or rep changed
```

The Spot Load Map has no sales rep column, which is why every due load's page is read.
The dispatch rep (`ass_user`) is never emailed; it is only shown on the loads that have no
sales rep, so the managers know who to hand them to.

Each email has a plain-text and an HTML part. Loads are split into **Past due** and
**Due within 48 hours**, earliest first, with customer, lane, pickup, due date (marked
"calculated" when CargoTel derived it), time left, units, price and a link to the load.

## Running it

```bash
# what is due right now, nothing composed (first run reads every due load's page)
python due_date_alerts.py --list

# dry run: one file per email in outbox/, the alert state is NOT touched
python due_date_alerts.py

# live: send through SES, record in alert_state.json and sent_log.jsonl
python due_date_alerts.py --send

# offline, against an archived page
python due_date_alerts.py --from-html pages/spot_20261013-1100.html --reps-json reps_by_load.json
```

Needs the AWS SSO session (`aws sso login`) for the CargoTel cookie in S3 and, with
`--send`, SES rights on the vehicles@ identity. `--reps` defaults to `../reps.csv`.

Switches worth knowing: `--hours` (48), `--repeat-hours` (24; `0` = every run),
`--no-overdue`, `--cc` (comma-separated; default Justice and Stuart), `--rep-cache-hours`
(12), `--max-lookups` (150 load pages per run), `--save-html DIR`, `--now ISO` (pretend
it is another time), `--json FILE` (run summary, or the due loads with `--list`).

## Decisions baked in

- **Past-due loads are included** by default and shown first under their own heading. A
  load that is 6 days late is more urgent than one due tomorrow, and nobody else is
  looking at it. `--no-overdue` turns this off.
- **One email per rep per run**, never one per load. The same load is repeated to the same
  rep only after the repeat window (24 h locally, 20 h on AWS where the run is daily at
  8:00), but immediately if its due date moved or its sales rep changed.
- **Nothing is dropped silently.** A load with no sales rep, or whose rep has no email in
  `reps.csv`, goes to the Cc list (Justice + Stuart) as To, with the sales and dispatch
  rep named per load. A load whose page could not be read is treated the same way.
- **The secondary sales rep is Cc'd** when the load has one and `reps.csv` knows the address.
- **Dry runs leave the state alone**, so flipping to `--send` later needs no cleanup
  (unlike the reload suggester's first day).
- **Stuart Mitchell's address** is `smitchell@circledelivers.com`, the MAIN email on his
  TransportPro user record (user 1349). Justice Cromwell's is the one in `reps.csv`.
  Override both with `--cc` (or `ALERT_CC` on Lambda).

## Open questions (seen on 2026-10-13)

- **Calculated due dates move.** Between two pulls 17 minutes apart, seven loads'
  `*Calculated` due dates moved one to seven days earlier (for example 342271 went from
  10/16 to 10/09) and then stayed put; typed due dates did not change. Opening a load page
  did not change anything on its own. A moved due date re-triggers the alert, which is
  the right thing if the move is real; if these values flap, raise `--repeat-hours` or
  ask the CargoTel admin how the calculation works.
- **Most Spot Load Map loads have no sales rep.** On the first run 3 of the 10 due loads
  had no `sales_rep` on the load page (only a dispatch rep), so they went to Justice and
  Stuart. If the team would rather have the dispatch rep alerted in that case, that is a
  small change in `address()`.
- **Stale loads repeat every day** (one load was 13 days past due). That is by design,
  but `--no-overdue` or a cap on how many days past due to include are both easy to add.

## On AWS

```powershell
.\due_date_alerts\deploy.ps1            # stage one: runs on the schedule, emails to s3://.../due-alerts/outbox/
.\due_date_alerts\deploy.ps1 -Send      # live
.\due_date_alerts\deploy.ps1 -CodeOnly  # code change only
.\due_date_alerts\deploy.ps1 -Paused    # kill switch
```

Function `circle-due-date-alerts` (Python 3.12, 512 MB, 15 min, concurrency 1), schedule
`cron(0 8 * * ? *)` America/New_York (every morning at 8:00 Eastern, repeat window 20 h so a
load alerted later in the day still shows up next morning), bucket `circle-reload-suggestions`:

| Key | What |
| --- | --- |
| `config/reps.csv` | shared with the reload suggester; one list to keep in sync |
| `due-alerts/state/alert_state.json` | what was alerted and when |
| `due-alerts/state/rep_cache.json` | sales reps read from load pages |
| `due-alerts/state/sent_log.jsonl` | one line per email |
| `due-alerts/outbox/` | dry-run emails while `ALERT_SEND=0` |
| `due-alerts/pages/` | archived pages with `ALERT_SAVE_PAGES=1` |

Environment variables: `ALERT_BUCKET`, `ALERT_PREFIX`, `ALERT_PAUSED`, `ALERT_SEND`,
`ALERT_HOURS`, `ALERT_REPEAT_HOURS`, `ALERT_CC`, `ALERT_NO_OVERDUE`, `ALERT_SAVE_PAGES`,
`MAIL_FROM`, `SES_CONFIGURATION_SET`, `TZ`. The role may read the cookie object and
`config/`, read and write `due-alerts/`, and send as vehicles@. No Bedrock.

Rollout: deploy without `-Send`, read a day of `due-alerts/outbox/`, check the grouping
and the addresses, then `-Send`.
