# Pickup-ETA alerts

Emails the dispatch rep and the sales rep of every Spot Loads - Tracking load that needs a
pickup update: no pickup ETA, a pickup ETA that has passed while the load is still
Dispatched, or no scheduled pickup date. From `vehicles@circledelivers.com`, Cc Justice
Cromwell and Stuart Mitchell. Built on the CargoTel scraping and the rep list of the reload
suggester one folder up, and on the email plumbing of `../due_date_alerts`.

| File | What it does |
| --- | --- |
| `pickup_eta_alerts.py` | The whole job: pull the tracking search, apply the rules, read each flagged load's sales rep from its load page, group by (dispatch rep, sales rep), send one email per pair (or write it to `outbox/`), remember what was sent. CLI + importable. |
| `pickup_eta_lambda.py` | AWS Lambda entry point: one run per invocation, state in S3 (`pickup-alerts/` prefix of the reload suggester's bucket, sharing its `config/reps.csv`). |
| `deploy.ps1`, `deploy/lambda-policy.json` | Builds the zip (the shared modules, `due_date_alerts.py` and these two) and creates or updates the function `circle-pickup-eta-alerts`, its roles and the EventBridge schedule. |
| `tests/` | Unit tests on a synthetic tracking page with the live ETA widget markup. `python -m unittest` from this folder. |

## Where the data comes from

The menu path the request names, Load Tracking > Spot Load Tracking > Spot Loads -
Tracking, is saved search **1823**. Its filter is status Dispatched (`O`) or In-Route
(`G`), carrier added in the last 90 days, bundle parents and individual orders; on
2026-10-13 it had 104 rows (65 Dispatched, 39 In-Route), every one with a carrier and a
dispatch rep. The grid columns that matter:

| Column | Key | What is in it |
| --- | --- | --- |
| Dispatch Rep | `dispatch_rep` | `KYLE LINK` (first last, capitals) |
| Dispatched Status Date | `statuspendingpickup` | when the current status was set; the load's age for the grace period |
| Pickup ETA (Edit) | `pickup_eta_only` | an inline editor: five `<select>` boxes (month / day / year / hour / minute) whose `SELECTED` options are the stored ETA. Nothing selected = no ETA. Verified against the Pickup ETA field on two load pages (343963: `10/13/2026 11:00` in both; 343786: empty in both). |
| PickUp Scheduled Date | `sched_pickup` | `10/10/2026 14:26`; CargoTel fills it at dispatch unless a user typed one, so it is rarely empty (never, on 2026-10-13) |
| Carrier Name, Carrier Phone, Driver Name, Drivers Cell Phone | | shown in the email so the rep can call |
| Priority Comment | `PriorityComment` | free text such as "Pickup later today" or "Picked up"; shown as a note |

The saved view has no sales rep column, but CargoTel honours `Menu__ViewFields` on a GET
(verified 2026-10-13): the job asks for `sales_rep1`, `sales_rep2` and `consignor` only,
CargoTel puts those first and appends the view's own columns, and every row comes back
with its reps (`KYLE LINK` / `Link, Kyle`). Do not ask for the full column list: the
saved columns then appear twice and the duplicate Pickup ETA (Edit) cell is empty, which
reads as "no widget" on every row (the run refuses to continue in that case). Only if the
rep columns are missing does the job fall back to reading each flagged load's
`loadmaint.mcgi` page (3-4 s each, cached 12 h in `rep_cache.json`).

## How it works

```
Spot Loads - Tracking (saved search 1823, all rows)
   │  statuses Dispatched / In-Route (--status)
   ▼
rules                                     no pickup ETA                       (always)
                                          pickup ETA passed                   (--skip-stale-check turns off): still Dispatched
                                                                              (--stale-statuses) 2 h (--stale-eta-hours) after the
                                                                              ETA; a date-only ETA counts as the whole day
                                          no scheduled pickup date            (--skip-sched-check turns off)
   │  only loads at least --min-age-hours (1) past their status date,
   │  and not same-day loads (pickup ETA or scheduled pickup today; --include-same-day)
   ▼
sales rep per load                        Sales Rep 1 / 2 columns of the grid (fallback: loadmaint.mcgi, cached 12 h);
   │                                      a load with none gets the rep its customer has on its other loads
   │                                      (this grid first, then customer_reps.json), marked "inferred";
   │                                      dispatch rep straight from the grid
   ▼
one email per (dispatch rep, sales rep)   To both (reps.csv), Cc Justice + Stuart (+ secondary sales rep);
   │                                      pairs with the same recipients are merged into one email;
   │                                      nobody addressable -> To Justice + Stuart, reps named per load
   ▼
alert_state.json                          a load repeats after 24 h, or at once if what it needs or who owns it changed;
                                          it drops out the moment the ETA is entered or the status moves on
```

Each email has a plain-text and an HTML part, with one section per rule ("No pickup
ETA", "Pickup ETA passed, status not updated", "No scheduled pickup date"), each with its
one-line instruction, and a table per section: load, customer, status and how long it has
been in it, lane, carrier and driver with phones, scheduled pickup, pickup ETA (in red
with "passed 3 d ago" when that is the problem), due date, units, the action needed and
the priority comment. A load that breaks two rules sits under the first section and its
action line lists both.

## Running it

```bash
# what is flagged right now, nothing composed (first run reads every flagged load's page)
python pickup_eta_alerts.py --list

# dry run: one file per email in outbox/, the alert state is NOT touched
python pickup_eta_alerts.py

# live: send through SES, record in alert_state.json and sent_log.jsonl
python pickup_eta_alerts.py --send

# show someone the real thing first: every email goes to you, marked as a test copy, nothing recorded
python pickup_eta_alerts.py --send --redirect-to you@circledelivers.com

# offline, against an archived page
python pickup_eta_alerts.py --from-html pages/tracking_20261013-1125.html --reps-json reps_by_load.json
```

Needs the AWS SSO session (`aws sso login`) for the CargoTel cookie in S3 and, with
`--send`, SES rights on the vehicles@ identity. `--reps` defaults to `../reps.csv`.

Switches worth knowing: `--min-age-hours` (1), `--repeat-hours` (24; `0` = every run),
`--status` (`Dispatched,In-Route`), `--skip-sched-check`, `--skip-stale-check`,
`--stale-eta-hours` (2), `--stale-statuses` (`Dispatched`), `--include-same-day`,
`--no-infer-sales-rep`, `--cc` (comma-separated; default Justice and Stuart), `--only-rep
NAME` (either role; `unassigned` for loads with nobody), `--rep-cache-hours` (12),
`--max-lookups` (150), `--save-html DIR`, `--now ISO`, `--json FILE`.

## First runs (2026-10-13)

With the no-ETA rule alone, 10 of the 104 loads were flagged, all Kyle Link's as both
dispatch and sales rep (orders 342198, 342979, 343785-343789, 343794-343796; dispatched 4
to 7 days earlier). None lacked a scheduled pickup date. That was one email, To Kyle, Cc
Justice and Stuart; Justice approved it and the job went live.

Adding the passed-ETA rule the same afternoon (110 loads by then) flagged 20 more:
Dispatched loads whose pickup ETA was one to several days gone, spread over Kyle, Seth
Edmondson, Mitchell Meyers and Victoria Storm, which makes 6 emails per run instead of 1.
In-Route loads were left out of that rule on purpose (see below); 39 of them also had a
past pickup ETA.

## Decisions baked in

- **Both reps get To**, the managers Cc, exactly as asked. When the same person is the
  dispatch and the sales rep (every load on the first run) they get it once.
- **One email per (dispatch rep, sales rep) pair**, never one per load, so everyone on an
  email is responsible for every load in it. Pairs that resolve to the same To and Cc
  (for example a rep's own loads plus their loads with no sales rep) are merged, so a rep
  gets one email per run.
- **Same-day loads are left alone.** A load whose pickup ETA or scheduled pickup date is
  the run day is not alerted that day (a timed ETA earlier today is not "passed" for this
  purpose either); if it is still open tomorrow it is. `--include-same-day` turns this off.
- **A missing sales rep is taken from the customer's other loads** (the rep most of that
  customer's loads on the tracking page carry, else the rep seen most often on earlier
  runs, kept in `customer_reps.json`), so the load joins that rep's email instead of
  going to the dispatch rep alone. The email says so per load and asks for the rep to be
  set on the load; the inferred rep is never written back into the history. A customer
  with no other loads anywhere still ends up with no sales rep. `--no-infer-sales-rep`
  turns this off.
- **"Pickup ETA passed" only applies to Dispatched loads.** CargoTel keeps a load
  In-Route from the moment the driver sets off until it is Delivered, so a past pickup ETA
  on an In-Route load is normal and the rep has nothing to update. `--stale-statuses
  Dispatched,In-Route` widens it if the team wants those too.
- **A date-only ETA means the whole day.** CargoTel stores "10/13/2026" as 00:00; the load
  is only past its pickup once that day is over, plus the 2-hour grace. A timed ETA gets
  the same 2 hours (`--stale-eta-hours`) before a late driver becomes an alert.
- **"Not scheduled" is read as an empty PickUp Scheduled Date.** CargoTel fills that field
  at dispatch, so the rule rarely fires; it is kept because the request named it, and
  `--skip-sched-check` turns it off. If "not scheduled" means something else (for
  example a scheduled pickup date that has passed with the load still Dispatched), that
  is a small change in `select_loads()`.
- **A one-hour grace period** after the status date, so a load dispatched minutes ago is
  not nagged while the rep is still entering the ETA. `--min-age-hours 0` disables it.
- **In-Route loads are still checked for a missing ETA**, because the saved search returns
  them and the ETA field is editable on them. `--status Dispatched` leaves them out
  entirely.
- **Nothing is dropped silently.** A load whose dispatch and sales rep both lack an
  address in `reps.csv` (or that has no sales rep and a dispatch rep not in the list)
  goes to Justice and Stuart as To, with both reps named per load. A load whose page could
  not be read is alerted with no sales rep rather than skipped.
- **The run refuses to proceed** if the saved view loses the Pickup ETA (Edit) or
  PickUp Scheduled Date column, instead of flagging every load.
- **Dry runs leave the state alone**; `--redirect-to` test copies are never recorded.
- **Stuart Mitchell's address** is `smitchell@circledelivers.com` (his TransportPro user
  record); Justice Cromwell's is the one in `reps.csv`. Override both with `--cc` (or
  `PICKUP_CC` on Lambda).

## On AWS

```powershell
.\pickup_eta_alerts\deploy.ps1            # stage one: runs on the schedule, emails to s3://.../pickup-alerts/outbox/
.\pickup_eta_alerts\deploy.ps1 -Send      # live
.\pickup_eta_alerts\deploy.ps1 -CodeOnly  # code change only
.\pickup_eta_alerts\deploy.ps1 -Paused    # kill switch
```

Function `circle-pickup-eta-alerts` (Python 3.12, 512 MB, 15 min, concurrency 1), schedule
`cron(0 11 * * ? *)` America/New_York (every morning at 11:00 Eastern, seven days a week;
a load is nagged once a day until the ETA is entered). The repeat window on AWS is 20 h
rather than 24 so a once-a-day run never skips a day when it starts a few seconds earlier
than the previous one. Bucket `circle-reload-suggestions`:

| Key | What |
| --- | --- |
| `config/reps.csv` | shared with the other two jobs; one list to keep in sync |
| `pickup-alerts/state/alert_state.json` | what was alerted and when |
| `pickup-alerts/state/rep_cache.json` | sales reps read from load pages (fallback path) |
| `pickup-alerts/state/customer_reps.json` | sales reps seen per customer, for loads that have none |
| `pickup-alerts/state/sent_log.jsonl` | one line per email |
| `pickup-alerts/outbox/` | dry-run emails while `PICKUP_SEND=0` |
| `pickup-alerts/pages/` | archived pages with `PICKUP_SAVE_PAGES=1` |

Environment variables: `PICKUP_BUCKET`, `PICKUP_PREFIX`, `PICKUP_PAUSED`, `PICKUP_SEND`,
`PICKUP_REPEAT_HOURS`, `PICKUP_MIN_AGE_HOURS`, `PICKUP_STATUS`, `PICKUP_SKIP_SCHED_CHECK`,
`PICKUP_SKIP_STALE_CHECK`, `PICKUP_STALE_ETA_HOURS`, `PICKUP_STALE_STATUSES`,
`PICKUP_INCLUDE_SAME_DAY`, `PICKUP_NO_INFER_SALES_REP`, `PICKUP_CC`, `PICKUP_SAVE_PAGES`,
`MAIL_FROM`, `SES_CONFIGURATION_SET`, `TZ`. The role may read the cookie object and `config/`, read and
write `pickup-alerts/`, and send as vehicles@. No Bedrock.

Rollout as it happened (2026-10-13): one real send from this machine at 11:32 ET (Kyle
Link, 10 loads) which Justice checked, then the local `alert_state.json`, `sent_log.jsonl`
and `rep_cache.json` were copied to `s3://.../pickup-alerts/state/` and `deploy.ps1 -Send`
went live the same day. Keep that order for any future manual send: state to S3 first, or
the Lambda repeats it on its next run.
