# Sales rep sync

Gives every unassigned load on **Bot Pipelines > Sales Rep Edit** the **Default Primary
Rep** of its Bill To customer, as recorded on **Administrative Functions > Client
Management > Customer Admin > Customer List**, by pressing the screen's own "Primary Sales
Rep (Bulk)" widget.

Why: CargoTel keeps a default rep per customer but does not copy it onto new loads, so
loads arrive with no Sales Rep 1. Every job in this repo that emails or Cc's the sales rep
(reload suggestions, due-date alerts, pickup-ETA alerts, tracking-update alerts) holds
such loads. With the rep filled in within half an hour, they flow.

## What one run does

1. **Customer List** (`ClientSearch.mcgi?expand_saved=65`, `Menu__perPage=All` on the GET):
   client id, name, key and Default Primary Rep for every customer (109 on 2026-11-09, 65
   with a rep). Reps are shown as `LAST, FIRST`.
2. **Sales Rep Edit** (`Search.mcgi?expand_saved=2080`, saved on 2026-11-09): Sales Rep =
   Unassigned, carrier added in the last 90 days, pickup ETA set, Ford (74771) and
   Mercedes (82705) excluded, grouped by customer. The job adds the `bill_id`,
   `billtoname`, `cons_id`, `consignor` and `original_consignor` columns on the GET
   (`Menu__ViewFields`); CargoTel puts them first and keeps the saved columns, including
   the bulk checkbox and Sales Rep 1.
3. **Plan**: for each load, the Bill To id is looked up on the Customer List (falling back
   to the Customer id when the Bill To is not listed), the customer's default rep is
   normalised (`CROMWELL, JUSTICE` -> `JUSTICE CROMWELL`) and matched against the labels of
   the screen's "Primary Sales Rep (Bulk)" dropdown, which carries CargoTel's user ids.
   A load is skipped, and listed with the reason, when
   - its Bill To is not on the Customer List (e.g. 62942, a second Circle Logistics account),
   - the customer has no Default Primary Rep (e.g. United States Postal Service),
   - the default rep is not in the dropdown,
   - the row has no bulk checkbox,
   - Sales Rep 1 already holds the rep ("already set"), or
   - Sales Rep 1 holds a *different* rep. By default only empty reps are filled; pass
     `--overwrite` to align those too (only relevant if someone widens the screen's filter
     beyond Unassigned).
4. **Apply** (`--apply`): one POST per rep. The whole `Menu_alt` form of the fetched page
   is sent exactly as a browser would (every hidden field, the per-row `STATESEED` /
   `STATEVER` tokens, the ETA widgets with their current values), with that rep's loads
   ticked (`sales_rep_bulk//<order id>`), the rep chosen in
   `Control__sales_rep_bulk__sales_rep` and `Menu__is_submitted=Submit`. The second and
   later POSTs are built from the page the previous POST rendered, like a browser. The
   Unassign entry (`[NONE]`) is never chosen.
5. **Check**: the re-rendered screen that comes back from each POST is parsed. A load that
   has left the unassigned-only screen, or shows the rep in Sales Rep 1, is *verified*;
   one still there with no (or another) rep is *failed* and retried next run. A response
   that is not the screen (sign-in page, CargoTel error, no form) fails that rep's loads
   and stops the run, since the page state is then unknown.

Without `--apply` nothing is written: the plan is printed and saved as JSON in `outbox/`.
With `--apply` the report goes to `reports/` and every changed or failed load is appended
to `changes.jsonl`.

Dry run on 2026-11-09 14:03 ET: 59 loads on the screen, 57 planned (Justice Cromwell 36
Stellantis + Circle Logistics loads, Kyle Link 15, Victoria Storm 5 Kingbee, Mitchell
Meyers 1), 2 skipped (62942 not on the list; USPS has no default rep). Live test 14:31 ET on
load 354657 alone, then the first live pass on AWS at 15:02 ET: 57 loads, five POSTs
(Isiah Greenfield 1 USPS, Justice Cromwell 37, Kyle Link 13, Mitchell Meyers 1, Victoria
Storm 5), all 57 assigned in 81 s; the screen was empty afterwards. That first pass reported
Victoria's five as failed because the empty "No Results Found" response has no form; the
check was fixed the same afternoon and the five records corrected in the S3 change log.

## Running it

```bash
python sales_rep_sync/sales_rep_sync.py --list                          # the plan, nothing saved
python sales_rep_sync/sales_rep_sync.py                                 # dry run, report to outbox/
python sales_rep_sync/sales_rep_sync.py --apply --only-load 354657       # one load, for real
python sales_rep_sync/sales_rep_sync.py --apply --only-rep "Kyle Link"  # one rep's loads
python sales_rep_sync/sales_rep_sync.py --apply --save-pages sales_rep_sync/pages
python sales_rep_sync/sales_rep_sync.py --from-html screen.html --customers-html list.html --list
```

| Option | Meaning |
| --- | --- |
| `--apply` | press the widget; otherwise a dry run |
| `--overwrite` | also change loads whose Sales Rep 1 differs from the customer's default |
| `--only-load ID[,ID]`, `--only-rep NAME`, `--only-customer ID\|NAME` | narrow the changes (the rest are listed as skipped) |
| `--max-changes N` | cap per run, default 200 (`-1` = no cap); the rest wait for the next run |
| `--save-pages DIR` | archive both fetched pages and every POST response |
| `--from-html FILE --customers-html FILE` | work from saved pages (no network; `--apply` refused) |
| `--outbox`, `--reports`, `--changes-log` | where the dry-run reports, apply reports and change log go |

Exit code 1 when any load failed verification.

Tests: `python -m unittest` from this folder (synthetic pages shaped like the live
markup; no network; the poster is a fake).

## On AWS

`sales_rep_lambda.py` runs one pass per invocation with `state/` (the change log),
`reports/`, `outbox/` and `pages/` mirrored under `s3://circle-reload-suggestions/sales-rep-sync/`.
`deploy.ps1` builds the zip and creates or updates the function `circle-sales-rep-sync`,
its roles (cookie read, own prefix read/write; no SES) and the EventBridge schedule.

```powershell
.\sales_rep_sync\deploy.ps1            # stage one: on AWS, dry runs to the outbox prefix
.\sales_rep_sync\deploy.ps1 -Apply     # live
.\sales_rep_sync\deploy.ps1 -CodeOnly  # code change only
.\sales_rep_sync\deploy.ps1 -Paused    # kill switch
```

Schedule: `cron(20/30 6-21 * * ? *)` America/New_York, every 30 minutes at :20 and :50 from
06:20 to 21:50, seven days a week (the 30-minute cadence is the user's choice of 2026-11-10;
it ran every two hours at :30 before). The tracking-update alerts post to CargoTel from :00
and their Lambda stops at 15 minutes, so :20 clears every tracking run unless one times out
and Lambda retries it. A pass takes 1 to 90 seconds. Environment:
`SRS_APPLY`, `SRS_OVERWRITE`, `SRS_MAX_CHANGES`, `SRS_SAVE_PAGES`, `SRS_PAUSED`,
`SRS_BUCKET`, `SRS_PREFIX`, `TZ`. A manual invoke accepts `{"dry_run": true}` and
`{"only_load": "354657"}`.

## CargoTel facts this relies on

- The menu entry for the Customer List is a `ClientSearch.mcgi` saved view (id 65); it takes
  the same `Menu__*` GET parameters as `Search.mcgi` and the shared grid parser reads it
  (columns `Id`, `Client`, `Key`, `CreditTerms`, `Current`, `Phone`, `Addr1`, `City`,
  `State`, `Zip`, `Contact`, `defaultPrimaryrep`). Its filter is Status A/S/P, type
  Consignor, so a Bill To account outside that filter is "not on the Customer List".
- On the Sales Rep Edit screen `bill_id` equalled `cons_id` on every row seen; `billtoname`
  and `consignor` read `N/A` on EDI loads, `original_consignor` has the name.
- The bulk widget is one `<select name="Control__sales_rep_bulk__sales_rep">` at the foot
  of the `Menu_alt` form (options: AUBREY LAFAVE 37899, ISIAH GREENFIELD 27680, JUSTICE
  CROMWELL 26640, KYLE LINK 31033, MIKE REDBEAR 34145, MITCHELL MEYERS 26638, PODGE ORMSBY
  27801, SETH EDMONDSON 36153, STUART MITCHELL 26637, Unassign `[NONE]`, VICTORIA STORM
  36426) plus one checkbox per row. As with the dispatch-comment write in
  `tracking_update_alerts`, CargoTel honours the widget only when the whole form is posted.
- The Customer List reps are `LAST, FIRST`; the dropdown labels are `FIRST LAST`; grid
  columns such as Sales Rep 1 use `LAST, FIRST`. `norm_name` makes them comparable.
- A search with no rows renders as a bare page: the filter form, then
  `<div id='no_results_found'>No Results Found</div>`, with no grid, no `Menu_alt` form and
  no bulk dropdown. The POST that assigns the last loads on the screen comes back like
  this, so `screen_problem` accepts it and every posted load counts as having left the
  screen. A load that leaves the screen between the fetch and its rep's POST (someone else
  assigned it) is reported as skipped, not claimed as the bot's change.
- Each change shows on the load page as a Misc Comment "Pri. Sales Rep changed to Last,
  First." under the cookie's user (Alec Stopher), the same trace a manual edit leaves. The
  history also shows "Pri. Sales Rep unassigned" rows when a person cleared a rep; on
  2026-11-09, 6 of the 56 unassigned loads had such a row (the rest never had a rep).
