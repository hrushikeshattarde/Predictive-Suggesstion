# Predictive Suggestion Automation

Tooling for the CargoTel side of the predictive suggestion automation (report:
https://claude.ai/code/artifact/2d853d83-e36c-48fc-8f35-3cc9336a0f89).

| File | What it does |
| --- | --- |
| `pull_load_detail.py` | Dumps `loadmaint.mcgi` for one or more load ids (the original scraping tool). |
| `cargotel_search.py` | Phase 0: fetches a `Search.mcgi` saved search with optional radius, date and page-size filters, and parses the grid (rows, per-row load hash, `MapPoints` coordinates). CLI + importable. |
| `suggest_reloads.py` | Phase 1 dry run: for a dispatched load id, reads its destination zip, carrier and dispatch rep from `loadmaint.mcgi`, runs the 150-mile Load Build search around that zip, groups bundle members into built loads, picks 5 to 10 (loads first, then orders) and prints the draft email. Never sends. |
| `dispatch_watcher.py` | Polls the Assigned Carrier Report. Default trigger `accepted`: a new dispatch whose offer the carrier has accepted (it is on the report and not on the Waiting Carrier Acceptance search), one event per truck (carrier + driver), suggester run around that load's delivery. `--trigger delivered` instead fires when a truck has dropped all its loads. Drafts go to `drafts/`, state to `watcher_state.json`. No Slack, no sending. |
| `send_drafts.py` | Sends the watcher's drafts from vehicles@ via SES, directly to the carrier's dispatcher, Cc the load's sales rep (and secondary sales rep), Reply-To the vehicles@ group; the sales rep signs. No forwarding step (`--mode pilot` exists as a switch). Duplicate guards: one email per carrier per 24 h (carrier identified by email), a load is never offered to the same carrier twice in 72 h, a load goes to at most 3 carriers in 24 h, drafts older than 12 h are dropped, do-not-email list, optional re-check that loads are still open, sent log. Dry run to `outbox/` unless `--send`. |
| `bedrock_email.py` | One load end to end with Claude Sonnet 5.5 on Bedrock writing the email from the facts, a deterministic fact check, and SES sending. Test addressing (TEST_TO / TEST_CC, "[TEST]" prefix) unless `--production`, which addresses the carrier and Cc's the sales rep(s). |
| `gmail_send.py` | Gmail API sender via the service account (impersonates a mailbox); kept for reading replies later. |
| `reps.example.csv` | Template for `reps.csv`: rep name as CargoTel shows it, email, optional title and phone. The email is signed by the load's sales rep (name, title, email, phone) and its closing line says to call the rep at that phone; a load with no sales rep assigned is held, never sent. |
| `lambda_handler.py` | AWS Lambda entry point: one watcher pass then one sender pass per invocation, with `config/`, `state/`, `drafts/`, `outbox/` and `pages/` mirrored to an S3 bucket. Switches are environment variables (`RELOAD_SEND`, `RELOAD_LLM`, `RELOAD_PAUSED`, ...). |
| `deploy/` | `deploy.ps1` builds the zip and creates or updates the bucket, IAM roles, Lambda function and EventBridge schedule; the JSON files are the policy templates it fills in. |
| `docs/AWS_DEPLOYMENT.md` | How it runs on AWS, the environment variables, the rollout stages, alarms, and what to check before going live. |
| `requirements.txt` | Pinned runtime dependencies, bundled into the Lambda zip. |
| `tests/` | Unit tests on synthetic pages that mirror the live markup. `python -m unittest`. |
| `due_date_alerts/` | Separate job: emails each sales rep the Spot Load Map loads due within 48 hours (Cc Justice Cromwell and Stuart Mitchell). Own README, tests, Lambda and deploy script; reuses the modules above. |
| `pickup_eta_alerts/` | Separate job: emails the dispatch rep and the sales rep the Spot Loads - Tracking loads that need a pickup update: no pickup ETA, a pickup ETA that passed while still Dispatched, or no scheduled pickup date (Cc Justice Cromwell and Stuart Mitchell). Same-day loads are skipped and a missing sales rep is taken from the customer's other loads. Own README, tests, Lambda and deploy script; reuses the modules above and the email plumbing of `due_date_alerts/`. |

Both scripts read the `cgt-browser-session` cookie from
`s3://circle-bot-cookies/rubicon/cargotel.json` through the default boto3 credential chain
(`aws sso login` first when running locally).

## Quick start

```bash
# which saved searches exist under a menu path
python cargotel_search.py --menu "General Dispatch"

# open loads within 150 miles of a zip, all rows, to CSV (+ archive the HTML)
python cargotel_search.py 1788 --radius 48326 --miles 150 --per-page All --out loads.csv --save-html loads.html

# 30 days of carrier assignments (history for ranking)
python cargotel_search.py 1587 --date-field CarrierAdd --window "Last 30 days" --per-page 1000 --out history.csv

# parse an archived page offline
python cargotel_search.py --from-html loads.html --json loads.json

# draft the reload email for a dispatched load (prints To / Cc / Subject / body; sends nothing)
python suggest_reloads.py 344141 --out draft.txt --json picks.json --save-html pages/

# same, around a zip without a load page
python suggest_reloads.py --zip 60477

# dispatch watcher: seed once, then run one pass per schedule (or --loop --interval 900)
python dispatch_watcher.py --seed
python dispatch_watcher.py
python dispatch_watcher.py --dry-run            # list what would be drafted

# sender, pilot mode: dry run writes to outbox/; --send goes through SES
python send_drafts.py --reps reps.csv
python send_drafts.py --reps reps.csv --send --from reloads@circledelivers.com --recheck
```

A production pass is `dispatch_watcher.py` followed by `send_drafts.py --reps reps.csv
--send --recheck`. The sender never re-sends a draft (sent log keyed on the watcher's
event), sends at most one email per carrier per 24 hours, never offers a load to the same
carrier twice within 72 hours, drops any load already offered to 3 other carriers in the
last 24 hours (`offers.json`), skips drafts older than 12 hours, skips carriers on
`do_not_email.txt` (one email or carrier name per line), and with `--recheck` drops loads
that have left the Load Build search since the draft. A load with no sales rep, or a sales
rep with no address in `reps.csv`, is held. The From address must be a verified SES
identity; `--configuration-set` routes bounce and complaint events.

The watcher groups the report's rows by carrier and driver (the driver is never emailed;
the name only tells a carrier's trucks apart). With `--trigger accepted` (default) it
fires on new Dispatched or In-Route rows within `--max-age-hours` (default 24), minus the
loads still on the Waiting Carrier Acceptance search (1543), one event per truck on its
newest dispatch; `--require-esign` additionally insists on an "E-Signed Carrier
Agreement" attachment on the load page. With `--trigger delivered` it fires when a truck
has a Delivered load with a date and nothing still active, on the last delivered load.
Trucks with no driver name are handled per load; bundle member rows are skipped. The
event key is (load, carrier, driver), so the same truck getting a later load is a new
event. Date filters sent as GET parameters had no effect on this report, so all
filtering is local. Failures leave the event for the next pass, up to `--max-attempts`.

`suggest_reloads.py` ranks candidates by a miles-equivalent score (deadhead miles from the
delivery point, plus a penalty when the load heads neither toward the carrier's base state
nor back toward the dispatched load's origin state, minus small bonuses for older inventory
and for carrying a price). Built loads are offered first; single orders only top the list
up to `--min` (default 5) or stand alone when there are no built loads; `--max` caps at 10.
Prices stay out of the email unless `--show-pay` is given.

Saved search ids seen on 2026-10-12: Engineering Map 1876, General Load Build w/ Map 1788,
Assigned Carrier Report 1587 (all under Dispatch > General Dispatch). Radius choices are
10, 25, 50, 75, 150 and 300 miles; `--per-page` accepts 10 … 10000 or `All`.

## What the parser returns

`parse_grid(html)` gives a `Grid` with `columns` (header order, key + label), `rows`, the
footer counts (`row_from`, `row_to`, `total_rows`, `is_partial`) and `warnings`. Each
`GridRow` has `load_id`, `section`, `cells[key].text / .lines / .load_links`, the
`load` hash (`orig_zip`, `dest_zip`, `orig_state`, `dest_state`, `miles`, `price`,
`carrier_id`, `shipper_id`, …) and `origin` / `dest` `MapPoint`s with `lat`, `lon`, `zip`.
`grid.records()` flattens rows for CSV.

Radius searches are a true great-circle filter on the load origin (or destination with
`--dest-radius`), but only radius row 1 of the form is applied, which is what
`build_search_params` sends. The result page echoes the point the zip resolved to
(`grid.radius_center`, e.g. 42.66, -83.27 for 48326) and the parser adds `origin_miles` /
`dest_miles` to every record from that point, since CargoTel's own "Origin Dist" column is
left blank. `--local-miles N` re-checks the radius locally from the parsed coordinates.
Verified 2026-10-12 on search 1788 around 48326: 0 rows at 10 mi, 18 at 25, 28 at 150, 138 at 300.

Three CargoTel quirks the parser handles, so callers do not have to:

- On map-enabled searches the class name on each data cell is shifted one column from the
  cell's content. Cells are mapped by position against the header row, and
  `grid.class_shift` reports when the shift was seen.
- A stale cookie is answered with HTTP 403 and the sign-in page. `is_signin_page` detects
  it and the session retries once with a fresh cookie; `parse_grid` flags it in
  `signin_page` instead of returning an empty grid.
- The radius fields sent as row 2 (`Menu__radius__2__*`) are echoed back but ignored; the
  module always sends row 1.

## Running on AWS

`lambda_handler.py` runs one watcher pass and one sender pass per invocation, with the
working files in S3; `deploy/deploy.ps1` builds and deploys it, and
[docs/AWS_DEPLOYMENT.md](docs/AWS_DEPLOYMENT.md) has the picture, the rollout stages
(dry run to `outbox/`, optional pilot, live) and the pre-flight checks.

```powershell
.\deploy\deploy.ps1 -UploadConfig        # stage 1: on AWS, nothing sent
.\deploy\deploy.ps1 -Send -Llm           # stage 3: live
.\deploy\deploy.ps1 -CodeOnly            # code change only
```
