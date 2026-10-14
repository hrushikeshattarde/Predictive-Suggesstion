# Duplicate order check

Every Monday at 08:00 Eastern, emails Justice Cromwell the VINs that appear on more than
one order number with the same origin and the same destination, from CargoTel's
**Orders > Duplicate check** saved search (id 1863). Sender: the vehicles@ group.

What the saved search already filters (as of 2026-10-13): customer Stellantis (fca);
statuses Active, In-Route, Dispatched, On Hold, Delivered; "Delivered Date Or Not
Delivered" = **Last 90 days**; rows grouped by VIN. The job adds the VIN, Load ID,
created / actual pickup / delivered dates, carrier, sales rep and zips as extra grid
columns, narrows the rows to the **last 7 days** the way the CargoTel dropdown would
(keep an order that is not delivered yet, or was delivered within the window), and then
groups the orders offline.

## Files

| file | what |
|---|---|
| `duplicate_check.py` | fetch, filter, group, CSV, email (CLI; dry run to `outbox/` unless `--send`) |
| `duplicate_lambda.py` | the Lambda handler: S3 in, run, S3 out |
| `deploy.ps1` | builds the zip, creates/updates the function, roles and the Monday schedule |
| `deploy/lambda-policy.json` | the function's IAM policy (cookie read, own prefix, SES as vehicles@) |
| `tests/test_duplicate_check.py` | offline tests: `python duplicate_check/tests/test_duplicate_check.py` |

## Run locally

```bash
python duplicate_check/duplicate_check.py
```

Fetches the search (~65 MB, about 100 s; the S3 session cookie must be valid), writes
`duplicate_check/duplicate_orders_<today>.csv` and the email to `duplicate_check/outbox/`.
`--send` sends through SES, `--days 0 --no-email` gives the whole 90-day list as CSV only,
`--save-json FILE` archives the grid and `--from-json FILE` re-analyses it offline
(`--now 2026-10-19T08:00` to pin the clock).

## AWS

| item | value |
|---|---|
| function | `circle-duplicate-check` (python3.12, 1024 MB, 15 min, reserved concurrency 1) |
| schedule | EventBridge Scheduler `circle-duplicate-check`, `cron(0 8 ? * MON *)` America/New_York |
| bucket | `s3://circle-reload-suggestions/duplicate-check/` (`state/sent_log.jsonl`, `reports/`, `outbox/`, `pages/`) |
| roles | `circle-duplicate-check-role`, `circle-duplicate-check-scheduler-role` |
| env | `DUP_SEND`, `DUP_DAYS`, `DUP_TO`, `DUP_CC`, `DUP_SKIP_EMPTY`, `DUP_SAVE_GRID`, `DUP_PAUSED`, `MAIL_FROM`, `TZ` |

```powershell
.\duplicate_check\deploy.ps1            # stage one: nothing sent, email lands in outbox/
.\duplicate_check\deploy.ps1 -Send      # live
.\duplicate_check\deploy.ps1 -CodeOnly  # code change only
.\duplicate_check\deploy.ps1 -Paused    # kill switch
```

Deployed live on 2026-10-14 with `-Send -SkipEmpty`: an email goes out only on Mondays
when at least one VIN was found. A quiet Monday is therefore either "nothing found" or a
failed run; tell them apart with the CloudWatch log or `s3://.../duplicate-check/reports/`
(a CSV is written on every run). Without `-SkipEmpty` an "all clear" email is sent instead.

## Match levels

| `match`         | meaning                                                                 |
|-----------------|-------------------------------------------------------------------------|
| `same facility` | origin and destination strings identical (facility name and city)       |
| `same city`     | only the "City, ST" parts match; facility names differ. Second table, for review. |

VINs whose other orders are on a different lane (out-and-back test-fleet moves) are not
reported.

## Gotchas

* Do **not** send `Menu__DateMne_DeliveredOr=...` (or `Menu__DeliveredOr`) on the GET.
  CargoTel's filter parser dies (`Can't locate object method "hour" via package
  "Last 90 days"`) and the half-written gzip body shows up in `requests` as
  `ContentDecodingError`. The 90-day window lives in the saved search and the 7-day one is
  applied locally; the job reads the window CargoTel echoes back and stops if it is not
  the expected one (`--expect-window`).
* Grid column keys differ from the date-filter keys: `LONG_Created`, `act_pickup`,
  `deliver`, `carriername`, `bundleparent`, `Vins17`, `sales_rep1`, `originzip`,
  `destinationzip` (all are `Menu__ViewFields_AVL_*` options on the page).
* With one run a week, a pair whose first order was delivered more than 7 days before the
  second one is created is only caught if the second is still undelivered at the next
  Monday run; `--days 0` (or `DUP_DAYS=0`) widens it to the full 90 days.
