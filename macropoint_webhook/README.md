# MacroPoint location receiver

Takes MacroPoint's "Location Updates" callback and keeps the newest ping of each CargoTel
load in S3, so the hourly tracking-update job (`../tracking_update_alerts/`) can write it
into the load's Location/ETA in CargoTel. Asked for on 2026-11-12: the driver-app pings
CargoTel collects are unreliable, and the vehicles team already tracks loads in MacroPoint.

## Where the data comes from

- **MacroPoint** (Descartes), portal `https://visibility.macropoint.com`, account "Circle
  Logistics (Main)". The vehicles team (vehicles@circledelivers.com) creates a tracking
  request per load with the **CargoTel load number as the MacroPoint Load ID**, tracking the
  driver's phone every 15 minutes for 4 to 7 days (checked on 351455, 354350 and 357141).
- **The callback.** Under Manage Company > Company Callback Urls, MacroPoint can push each
  location it receives to a URL of ours. Order Status Changes, Trip Event Updates, Form
  Submits, Location Temperature Updates and Alerting already go to TransportPro
  (`https://cli.transportpro.net/cron.macropointUpdate.php`); **Location Updates was empty**
  on 2026-11-12, so it can point here without touching TransportPro's setup.
- **The format** (Descartes MacroPoint docs, "Load Visibility Data Retrieval > Location
  Updates"): an HTTP GET with `MPOrderID`, `ID` (our Load ID), `Latitude`, `Longitude`,
  `Uncertainty`, `Street1`, `Street2`, `Neighborhood`, `City`, `State`, `Postal`, `Country`,
  `LocationDateTimeUTC`, `ApproxLocationDateTimeInLocalTime`, `DataSource` (Ping, Telematics,
  TMS, LTL, Agent), `MPTrackingRequestID` and `locator` (the tracked phone or truck).
  MacroPoint calls from 52.141.169.150, 52.141.169.192/28, 52.152.27.96/28 and 52.237.140.174.

## How it works

```
MacroPoint ──GET ?ID=354350&City=Forsyth&State=GA&LocationDateTimeUTC=...──▶ function URL /<secret>
   │  no secret (or wrong Basic auth)          -> 403
   │  ID not six digits (TransportPro loads)   -> 200 "ignored", nothing kept
   ▼
s3://circle-reload-suggestions/macropoint/latest/<load>.json
   {"load_id", "latest": {ping}, "history": [20 older pings]}   newest by LocationDateTimeUTC;
                                                                late or repeated pings never replace it
   ▼
hourly tracking-update run copies latest/ (last 24 h) and writes the place into Location/ETA
```

The account sends every ping of every active load (about 4,700 loads, mostly TransportPro's),
so the function is called often; it answers the others in milliseconds without storing
anything. Calls from outside MacroPoint's addresses are logged as `unexpected_source`, not
refused, in case Descartes adds addresses.

## On AWS (deployed 2026-11-12 09:56 ET, MacroPoint not pointed at it yet)

```powershell
.\macropoint_webhook\deploy.ps1                 # create or update, keeps the secret
.\macropoint_webhook\deploy.ps1 -CodeOnly       # code change only
.\macropoint_webhook\deploy.ps1 -RotateToken    # new secret: MacroPoint's URL must then be changed too
```

Function `circle-macropoint-webhook` (Python 3.12, 128 MB, 10 s), role
`circle-macropoint-webhook-role` (read/write `macropoint/*` in the bucket, logs only), a
public function URL (auth NONE, guarded by the secret path segment `MP_PATH_TOKEN`).
Environment: `MP_PATH_TOKEN`, `MP_BUCKET`, `MP_PREFIX` (`macropoint/`), `MP_ID_PATTERN`
(`^\d{6}$`), `MP_HISTORY` (20); optional `MP_BASIC_AUTH` (`user:password`) for MacroPoint's
Basic authentication instead of the secret path.

The URL to enter in MacroPoint is written by the deploy script to
`macropoint_webhook/callback_url.txt`, which is git-ignored because it carries the secret.

Verified 2026-11-12 with three calls: a test ping for dummy load 999999 was stored (record
removed afterwards), the same call without the secret got 403, a seven-digit ID got
"ignored" and nothing was stored.

## Turning it on (needs the manager's OK)

1. In MacroPoint: Manage Company > Company Callback Urls > Location Updates > edit.
2. Authentication Type: No Auth. Callback URL: the line in `callback_url.txt`.
3. Save. Pings start arriving within 15 minutes for every load being tracked; watch them with
   `aws logs tail /aws/lambda/circle-macropoint-webhook --since 30m --profile paybot-admin --region us-east-1`.

To stop: clear the Location Updates callback in MacroPoint (nothing else changes), or set
`TRACK_POST_LOCATIONS=0` on the tracking job to keep collecting without writing to CargoTel.

## Tests

`python -m unittest` from this folder: 9 tests on function-URL-shaped events with
MacroPoint's documented query string and a dict standing in for S3.
