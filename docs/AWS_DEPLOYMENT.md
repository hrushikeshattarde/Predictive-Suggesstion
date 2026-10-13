# Running the reload suggester on AWS

How the dispatch watcher and the sender run unattended on AWS, what they need, how to
deploy them, and how to roll sending out safely. Companion to the top-level
[README](../README.md), which explains what each script does.

The shape follows the Payment Bot deployment (schedule, Lambda, S3, no always-on server),
with fewer moving parts: there is no inbox to poll, no Slack approval step, and no Gmail.
Sending goes through SES, so the Google service account is not needed on AWS at all.

## 1. What runs where

```
EventBridge Scheduler  cron(0/15 6-21 ? * MON-SAT *)  America/New_York
        │
        ▼
Lambda  circle-reload-suggester   (Python 3.12, lambda_handler.handler, 1024 MB, 15 min, concurrency 1)
        │  1. mirror config/ state/ drafts/ from S3 into /tmp/reload
        │  2. dispatch_watcher.main()                      -> drafts/
        │  3. send_drafts.main(--recheck [--send] [--llm]) -> SES or outbox/
        │  4. upload whatever changed back to S3
        │
        ├── reads   s3://circle-bot-cookies/rubicon/cargotel.json   (the login bot keeps it fresh)
        ├── r/w     s3://circle-reload-suggestions/{config,state,drafts,outbox,pages}/
        ├── calls   Bedrock inference profile us.anthropic.claude-sonnet-5-5 (with RELOAD_LLM=1)
        └── sends   SES as Circle Logistics <vehicles@circledelivers.com>, Reply-To vehicles@
```

Account 988836287275, region us-east-1: that is where the SES identity and the Bedrock
profile already live. One invocation is one pass. The watcher's own `--loop` is never used.

The scripts keep working on plain files. `lambda_handler.py` only moves those files
between S3 and the Lambda scratch folder:

| Local file today | On AWS (`s3://circle-reload-suggestions/`) | Direction |
| --- | --- | --- |
| `reps.csv`, `do_not_email.txt` | `config/` | down only; the team edits these in S3 |
| `watcher_state.json`, `offers.json`, `sent_log.jsonl` | `state/` | down, then up |
| `drafts/` | `drafts/` (only the last 24 h are fetched) | down, then up |
| `outbox/` | `outbox/` | up only; dry-run emails while sending is off |
| `pages/` | `pages/` | up only, with `RELOAD_SAVE_PAGES=1` |
| `.env` | Lambda environment variables | |

Two details the handler takes care of, because the sender relies on them: a downloaded
file keeps its S3 `LastModified` as its mtime (the sender drops drafts older than 12 hours
by mtime), and `TZ=America/New_York` is set so the age filters in the watcher and sender
compare against CargoTel's Eastern timestamps rather than UTC.

## 2. Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `RELOAD_BUCKET` | (required) | the work bucket |
| `RELOAD_PAUSED` | `0` | `1` makes every pass a no-op: kill switch without touching the schedule |
| `RELOAD_SEND` | `0` | `1` sends through SES; otherwise emails are written to `outbox/` |
| `RELOAD_MODE` | `auto` | `auto` emails the carrier and Cc's the sales rep; `pilot` emails the sales rep to forward |
| `RELOAD_LLM` | `0` | `1` has Claude on Bedrock word each email (template on any failure) |
| `RELOAD_RECHECK` | `1` | drop loads that left the Load Build search since the draft |
| `RELOAD_MAX_SENDS` | `20` | emails per pass |
| `RELOAD_MAX_EVENTS` | watcher default (20) | drafts per pass |
| `RELOAD_TRIGGER` | `accepted` | or `delivered` |
| `RELOAD_MILES` | `150` | search radius |
| `RELOAD_REQUIRE_ESIGN` | `0` | also insist on the E-Signed Carrier Agreement |
| `RELOAD_SAVE_PAGES` | `0` | archive fetched CargoTel pages under `pages/` |
| `MAIL_FROM` | `Circle Logistics <vehicles@circledelivers.com>` | verified SES sender |
| `SES_CONFIGURATION_SET` | (none) | configuration set for bounce and complaint events |
| `BEDROCK_MODEL_ID` | `us.anthropic.claude-sonnet-5-5` | read by `bedrock_email.py` |
| `TZ` | `America/New_York` | set by the deploy script |

`deploy.ps1` sets all of these from its parameters; change them there, not in the console,
so the next deploy does not undo a console edit.

## 3. Permissions

The function role (`circle-reload-suggester-role`) gets exactly, from
[`deploy/lambda-policy.json`](../deploy/lambda-policy.json):

- `s3:GetObject` on the cookie object in `circle-bot-cookies`
- `s3:ListBucket`, `s3:GetObject`, `s3:PutObject` on the work bucket
- `bedrock:InvokeModel` on the inference profile and the underlying Claude Sonnet 5.5
  foundation model in every region the profile routes to
- `ses:SendEmail` and `ses:SendRawEmail` on the vehicles@ identity, the domain identity
  and configuration sets
- CloudWatch Logs through the managed `AWSLambdaBasicExecutionRole`

The scheduler role (`circle-reload-suggester-scheduler-role`) can only invoke the function.
Nothing else in the account can be reached from the bot.

## 4. Deploy

From the repo root in PowerShell, with `aws sso login --profile paybot-admin` done and the
`.venv` created from `requirements.txt`:

```powershell
# first time: bucket, roles, function, schedule; push reps.csv and do_not_email.txt to config/
.\deploy\deploy.ps1 -UploadConfig

# code change only
.\deploy\deploy.ps1 -CodeOnly

# run one pass by hand and read the result
aws lambda invoke --function-name circle-reload-suggester --profile paybot-admin --region us-east-1 --payload '{}' --cli-binary-format raw-in-base64-out out.json; Get-Content out.json

# follow the log
aws logs tail /aws/lambda/circle-reload-suggester --since 30m --follow --profile paybot-admin --region us-east-1
```

The script builds the zip with Linux wheels (so a Windows build machine is fine), uses
Python's `zipfile` rather than `Compress-Archive` (Windows PowerShell 5.1 writes backslash
entry names that Lambda cannot import), bundles boto3 (the Bedrock prompt-cache TTL needs
a recent botocore), and sets reserved concurrency to 1 so two passes can never overlap.

`reps.csv` and `do_not_email.txt` are edited directly in `s3://circle-reload-suggestions/config/`
once deployed; every pass reads them fresh. Versioning is on, so an edit can be undone.

## 5. Rollout

Each stage is one `deploy.ps1` run; the switches only change environment variables.

| Stage | Command | What happens | Leave it for |
| --- | --- | --- | --- |
| 1. Dry run on AWS | `.\deploy\deploy.ps1 -UploadConfig` | full pipeline, emails written to `outbox/`, nothing sent | about three days; read the outbox |
| 2. Pilot (optional) | `.\deploy\deploy.ps1 -Send -Mode pilot -Llm` | the sales rep receives the email and forwards it | a week |
| 3. Live | `.\deploy\deploy.ps1 -Send -Llm` | carrier dispatcher gets it, sales rep Cc'd, 20 per pass | |

Stage 1 shakes out cookie access, IAM, Bedrock access and SES configuration before any
email leaves. Pausing at any stage: `.\deploy\deploy.ps1 -Paused`, or disable the schedule
in EventBridge Scheduler.

Dry-run passes still write to `state/sent_log.jsonl` and `state/offers.json` (the file
transport counts as a send for the duplicate guards). When moving from stage 1 to a sending
stage, delete those two objects first, or the 24 h carrier cooldown and the 72 h repeat
guard will hold real emails because of outbox-only ones:

```powershell
aws s3 rm s3://circle-reload-suggestions/state/sent_log.jsonl --profile paybot-admin
aws s3 rm s3://circle-reload-suggestions/state/offers.json --profile paybot-admin
```

Deployed 2026-10-13 in stage 1. The first pass on AWS took about three minutes for 9
drafts; the watcher draft cap of 20 per pass and the 15-minute cadence clear a backlog of
a day's dispatches within an hour.

## 6. Alarms and bounces

Set up once, by hand or later in the script:

- An SNS topic `circle-reload-alerts` with the on-call address subscribed.
- CloudWatch alarm on the function's `Errors` metric, threshold 1 over 15 minutes.
- CloudWatch alarm on `Invocations` being 0 for 1 hour during schedule hours (the
  schedule stopped, or the function is broken before it logs).
- An SES configuration set `circle-reload` whose event destination sends bounces and
  complaints to the same topic; deploy with `-ConfigurationSet circle-reload`.
- A metric filter on the log group for `sent:` lines gives a daily count for free.

## 7. Before stage 1

- **Cookie bucket.** Confirm `circle-bot-cookies` is in account 988836287275. If it lives
  elsewhere, its bucket policy must grant `s3:GetObject` on the cookie key to the function
  role.
- **SES sandbox.** The account must be out of the SES sandbox, or only verified addresses
  receive mail.
- **Domain reputation.** Ask the Workspace admin to confirm SPF includes `amazonses.com`
  and DKIM is set up for `circledelivers.com`, so carriers' mail servers do not junk it.
- **vehicles@ delivery.** Replies go to the group. Membership and delivery on that Google
  Group must be working before the bot relies on it.
- **Reps list.** Every active sales rep needs a row in `config/reps.csv` with a confirmed
  address; a load whose rep is missing is held, never sent.
- **Bedrock access.** The Sonnet 5.5 inference profile must be enabled for the account in
  us-east-1.

## 8. Cost

At roughly 100 emails a day:

| Item | Monthly |
| --- | --- |
| Lambda, about 60 runs a day, about a minute each at 1 GB | under $2 |
| Bedrock Sonnet, about 4k tokens per email | about $15 |
| S3, SES, CloudWatch | under $1 |
