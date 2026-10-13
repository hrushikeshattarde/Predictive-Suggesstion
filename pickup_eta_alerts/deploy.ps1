<#
.SYNOPSIS
    Builds the Lambda zip and creates or updates the pickup-ETA alerts on AWS.

.DESCRIPTION
    Idempotent, modelled on ..\due_date_alerts\deploy.ps1. Shares the reload suggester's
    bucket (and its config/reps.csv) but has its own function, roles, prefix and schedule.
    Needs the AWS CLI v2 and the project's .venv (or a python on PATH).

.EXAMPLE
    # stage one: runs on AWS, nothing sent, emails land in s3://.../pickup-alerts/outbox/
    .\pickup_eta_alerts\deploy.ps1

.EXAMPLE
    # live: send through SES
    .\pickup_eta_alerts\deploy.ps1 -Send

.EXAMPLE
    # a code change only
    .\pickup_eta_alerts\deploy.ps1 -CodeOnly

.EXAMPLE
    # kill switch without touching the schedule
    .\pickup_eta_alerts\deploy.ps1 -Paused
#>
[CmdletBinding()]
param(
    [string]$AwsProfile = "paybot-admin",
    [string]$Region = "us-east-1",
    [string]$FunctionName = "circle-pickup-eta-alerts",
    [string]$Bucket = "circle-reload-suggestions",
    [string]$Prefix = "pickup-alerts/",
    [string]$CookieBucket = "circle-bot-cookies",
    [string]$CookieKey = "rubicon/cargotel.json",
    # Every morning at 11:00 Eastern, seven days a week (the user's choice, 2026-10-13).
    # The repeat window is 20 h, not 24: with exactly one run a day, a 24 h window would
    # skip a day whenever a run starts a few seconds earlier than the previous one.
    [string]$Schedule = "cron(0 11 * * ? *)",
    [string]$ScheduleTimezone = "America/New_York",
    [string]$MailFrom = "Circle Logistics <vehicles@circledelivers.com>",
    [string]$SesIdentity = "vehicles@circledelivers.com",
    [string]$ConfigurationSet = "",
    [string]$Cc = "",
    [string]$Status = "",
    [string]$StaleStatuses = "",
    [int]$RepeatHours = 20,
    [int]$MinAgeHours = 1,
    [int]$StaleEtaHours = 2,
    [int]$MemoryMb = 512,
    [int]$TimeoutSeconds = 900,
    [switch]$Send,
    [switch]$SkipSchedCheck,
    [switch]$SkipStaleCheck,
    [switch]$IncludeSameDay,
    [switch]$NoInferSalesRep,
    [switch]$SavePages,
    [switch]$Paused,
    [switch]$CodeOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

$Here = $PSScriptRoot                       # pickup_eta_alerts/
$Root = Split-Path -Parent $Here            # the project folder with the CargoTel modules
$Build = Join-Path $Here "build"
$Dist = Join-Path $Here "dist"
$Zip = Join-Path $Dist "function.zip"
$Aws = @("--profile", $AwsProfile, "--region", $Region)
$RoleName = "$FunctionName-role"
$SchedulerRoleName = "$FunctionName-scheduler-role"
# Everything the import chain needs, flat at the zip root.
$SharedModules = @("lambda_handler.py", "dispatch_watcher.py", "send_drafts.py", "suggest_reloads.py",
                   "cargotel_search.py", "bedrock_email.py", "gmail_send.py", "pull_load_detail.py")
# The email plumbing (transports, rep cache, test-copy redirect) comes from the due-date job.
$SiblingModules = @("due_date_alerts\due_date_alerts.py")
$OwnModules = @("pickup_eta_alerts.py", "pickup_eta_lambda.py")

function Invoke-Aws {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CliArgs)
    $out = & aws @CliArgs @Aws
    if ($LASTEXITCODE -ne 0) { throw "aws $($CliArgs -join ' ') failed (exit $LASTEXITCODE)" }
    return ($out -join "`n")
}

function Render-Template {
    param([string]$Path, [hashtable]$Values)
    $text = Get-Content -Raw -Path $Path
    foreach ($key in $Values.Keys) { $text = $text.Replace("{{$key}}", [string]$Values[$key]) }
    return $text
}

function Write-Utf8 {
    # BOM-less UTF-8: the AWS CLI rejects JSON files that start with a BOM.
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Ensure-Role {
    param([string]$Name, [string]$TrustFile, [string]$PolicyFile, [string]$ManagedPolicy = "")
    $arn = (Invoke-Aws iam list-roles --query "Roles[?RoleName=='$Name'].Arn" --output text).Trim()
    if (-not $arn) {
        $arn = (Invoke-Aws iam create-role --role-name $Name --assume-role-policy-document "file://$TrustFile" --query Role.Arn --output text).Trim()
        Write-Host "created role $Name; waiting for IAM to propagate"
        Start-Sleep -Seconds 12
    } else {
        Invoke-Aws iam update-assume-role-policy --role-name $Name --policy-document "file://$TrustFile" | Out-Null
    }
    Invoke-Aws iam put-role-policy --role-name $Name --policy-name "$Name-policy" --policy-document "file://$PolicyFile" | Out-Null
    if ($ManagedPolicy) { Invoke-Aws iam attach-role-policy --role-name $Name --policy-arn $ManagedPolicy | Out-Null }
    return $arn
}

function Test-FunctionExists {
    $name = (Invoke-Aws lambda list-functions --query "Functions[?FunctionName=='$FunctionName'].FunctionName" --output text).Trim()
    return ($name -eq $FunctionName)
}

# ---------------------------------------------------------------- account
Write-Host "== account"
$Account = (Invoke-Aws sts get-caller-identity --query Account --output text).Trim()
Write-Host "account $Account, region $Region, profile $AwsProfile"

# ---------------------------------------------------------------- build
Write-Host "== build $Zip"
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
New-Item -ItemType Directory -Force $Build | Out-Null
New-Item -ItemType Directory -Force $Dist | Out-Null
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
# Linux wheels for the Lambda runtime, whatever machine builds the zip.
& $Python -m pip install --quiet --requirement (Join-Path $Root "requirements.txt") --target $Build `
    --platform manylinux2014_x86_64 --only-binary=:all: --python-version 3.12 --implementation cp --upgrade
if ($LASTEXITCODE -ne 0) { throw "pip install into $Build failed" }
foreach ($m in $SharedModules) { Copy-Item (Join-Path $Root $m) $Build }
foreach ($m in $SiblingModules) { Copy-Item (Join-Path $Root $m) $Build }
foreach ($m in $OwnModules) { Copy-Item (Join-Path $Here $m) $Build }
Get-ChildItem $Build -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
if (Test-Path $Zip) { Remove-Item $Zip }
# Python's zipfile writes forward-slash entry names; Compress-Archive on Windows PowerShell 5.1 does not.
& $Python -c "import shutil, sys; shutil.make_archive(sys.argv[1], 'zip', sys.argv[2])" ($Zip -replace '\.zip$', '') $Build
if ($LASTEXITCODE -ne 0) { throw "zip failed" }
Write-Host ("zip {0:N1} MB" -f ((Get-Item $Zip).Length / 1MB))

if ($CodeOnly) {
    if (-not (Test-FunctionExists)) { throw "function $FunctionName does not exist yet; run without -CodeOnly first" }
    Write-Host "== code only: updating $FunctionName"
    Invoke-Aws lambda update-function-code --function-name $FunctionName --zip-file "fileb://$Zip" | Out-Null
    Invoke-Aws lambda wait function-updated-v2 --function-name $FunctionName | Out-Null
    Write-Host "done"
    exit 0
}

# ---------------------------------------------------------------- bucket prefixes
Write-Host "== bucket s3://$Bucket/$Prefix"
$found = (Invoke-Aws s3api list-buckets --query "Buckets[?Name=='$Bucket'].Name" --output text).Trim()
if ($found -ne $Bucket) { throw "bucket $Bucket does not exist; deploy the reload suggester first (..\deploy\deploy.ps1) or pass -Bucket" }
foreach ($p in @("state/", "outbox/", "pages/")) {
    Invoke-Aws s3api put-object --bucket $Bucket --key "$Prefix$p" | Out-Null
}
$repsKey = (Invoke-Aws s3api list-objects-v2 --bucket $Bucket --prefix "config/reps.csv" --query "Contents[].Key" --output text).Trim()
if ($repsKey -ne "config/reps.csv") {
    Write-Warning "s3://$Bucket/config/reps.csv is missing: every alert would go to the Cc list. Upload it with ..\deploy\deploy.ps1 -UploadConfig."
}

# ---------------------------------------------------------------- Lambda role
Write-Host "== roles"
$values = @{
    ACCOUNT = $Account; REGION = $Region; BUCKET = $Bucket; PREFIX = $Prefix
    COOKIE_BUCKET = $CookieBucket; COOKIE_KEY = $CookieKey
    SES_IDENTITY = $SesIdentity; SES_DOMAIN = ($SesIdentity -replace '^.*@', '')
}
$trustLambda = Join-Path $Build "trust-lambda.json"
Write-Utf8 $trustLambda (Render-Template (Join-Path $Root "deploy\trust-lambda.json") $values)
$policyLambda = Join-Path $Build "lambda-policy.json"
Write-Utf8 $policyLambda (Render-Template (Join-Path $Here "deploy\lambda-policy.json") $values)
$roleArn = Ensure-Role -Name $RoleName -TrustFile $trustLambda -PolicyFile $policyLambda `
    -ManagedPolicy "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
Write-Host "lambda role $roleArn"

# ---------------------------------------------------------------- function
Write-Host "== function $FunctionName"
$vars = [ordered]@{}
$vars["PICKUP_BUCKET"] = $Bucket
$vars["PICKUP_PREFIX"] = $Prefix
$vars["PICKUP_SEND"] = "0";             if ($Send) { $vars["PICKUP_SEND"] = "1" }
$vars["PICKUP_SKIP_SCHED_CHECK"] = "0"; if ($SkipSchedCheck) { $vars["PICKUP_SKIP_SCHED_CHECK"] = "1" }
$vars["PICKUP_SKIP_STALE_CHECK"] = "0"; if ($SkipStaleCheck) { $vars["PICKUP_SKIP_STALE_CHECK"] = "1" }
$vars["PICKUP_INCLUDE_SAME_DAY"] = "0"; if ($IncludeSameDay) { $vars["PICKUP_INCLUDE_SAME_DAY"] = "1" }
$vars["PICKUP_NO_INFER_SALES_REP"] = "0"; if ($NoInferSalesRep) { $vars["PICKUP_NO_INFER_SALES_REP"] = "1" }
$vars["PICKUP_SAVE_PAGES"] = "0";       if ($SavePages) { $vars["PICKUP_SAVE_PAGES"] = "1" }
$vars["PICKUP_PAUSED"] = "0";           if ($Paused) { $vars["PICKUP_PAUSED"] = "1" }
$vars["PICKUP_REPEAT_HOURS"] = [string]$RepeatHours
$vars["PICKUP_MIN_AGE_HOURS"] = [string]$MinAgeHours
$vars["PICKUP_STALE_ETA_HOURS"] = [string]$StaleEtaHours
$vars["MAIL_FROM"] = $MailFrom
$vars["TZ"] = "America/New_York"
if ($Status) { $vars["PICKUP_STATUS"] = $Status }
if ($StaleStatuses) { $vars["PICKUP_STALE_STATUSES"] = $StaleStatuses }
if ($Cc) { $vars["PICKUP_CC"] = $Cc }
if ($ConfigurationSet) { $vars["SES_CONFIGURATION_SET"] = $ConfigurationSet }
$envFile = Join-Path $Build "env.json"
Write-Utf8 $envFile (@{ Variables = $vars } | ConvertTo-Json -Compress)

if (-not (Test-FunctionExists)) {
    Invoke-Aws lambda create-function --function-name $FunctionName --runtime python3.12 --architectures x86_64 `
        --handler pickup_eta_lambda.handler --role $roleArn --zip-file "fileb://$Zip" `
        --timeout $TimeoutSeconds --memory-size $MemoryMb --environment "file://$envFile" `
        --description "Pickup-ETA alerts: emails the dispatch and sales reps the Spot Loads - Tracking loads with no pickup ETA, a passed pickup ETA, or no scheduled pickup date" | Out-Null
    Invoke-Aws lambda wait function-active-v2 --function-name $FunctionName | Out-Null
    Write-Host "created"
} else {
    Invoke-Aws lambda update-function-code --function-name $FunctionName --zip-file "fileb://$Zip" | Out-Null
    Invoke-Aws lambda wait function-updated-v2 --function-name $FunctionName | Out-Null
    Invoke-Aws lambda update-function-configuration --function-name $FunctionName --runtime python3.12 `
        --handler pickup_eta_lambda.handler --role $roleArn --timeout $TimeoutSeconds --memory-size $MemoryMb `
        --environment "file://$envFile" | Out-Null
    Invoke-Aws lambda wait function-updated-v2 --function-name $FunctionName | Out-Null
    Write-Host "updated code and configuration"
}
# Never two runs at once: they would race on the state files.
Invoke-Aws lambda put-function-concurrency --function-name $FunctionName --reserved-concurrent-executions 1 | Out-Null
$functionArn = (Invoke-Aws lambda get-function --function-name $FunctionName --query Configuration.FunctionArn --output text).Trim()

# ---------------------------------------------------------------- schedule
Write-Host "== schedule $Schedule ($ScheduleTimezone)"
$values["FUNCTION_ARN"] = $functionArn
$trustSched = Join-Path $Build "trust-scheduler.json"
Write-Utf8 $trustSched (Render-Template (Join-Path $Root "deploy\trust-scheduler.json") $values)
$policySched = Join-Path $Build "scheduler-policy.json"
Write-Utf8 $policySched (Render-Template (Join-Path $Root "deploy\scheduler-policy.json") $values)
$schedRoleArn = Ensure-Role -Name $SchedulerRoleName -TrustFile $trustSched -PolicyFile $policySched
$targetFile = Join-Path $Build "target.json"
Write-Utf8 $targetFile (@{ Arn = $functionArn; RoleArn = $schedRoleArn; Input = "{}"; RetryPolicy = @{ MaximumRetryAttempts = 0 } } | ConvertTo-Json -Compress)
$schedArgs = @("--name", $FunctionName, "--schedule-expression", $Schedule, "--schedule-expression-timezone", $ScheduleTimezone,
               "--flexible-time-window", "Mode=OFF", "--target", "file://$targetFile",
               "--description", "Runs the pickup-ETA alerts every morning at 11:00 Eastern")
$existing = (Invoke-Aws scheduler list-schedules --name-prefix $FunctionName --query "Schedules[?Name=='$FunctionName'].Name" --output text).Trim()
if ($existing -eq $FunctionName) {
    Invoke-Aws scheduler update-schedule @schedArgs | Out-Null
    Write-Host "updated"
} else {
    Invoke-Aws scheduler create-schedule @schedArgs | Out-Null
    Write-Host "created"
}

# ---------------------------------------------------------------- summary
$sendState = "OFF (emails go to s3://$Bucket/$($Prefix)outbox/)"
if ($Send) { $sendState = "ON via SES as $MailFrom" }
Write-Host ""
$staleState = "after $StaleEtaHours h"
if ($SkipStaleCheck) { $staleState = "off" }
Write-Host "done. sending $sendState; min age $MinAgeHours h; repeat after $RepeatHours h; passed-ETA rule $staleState; same-day loads included $($IncludeSameDay.IsPresent); sales rep inference $(-not $NoInferSalesRep.IsPresent); paused $($Paused.IsPresent)"
Write-Host "run one pass now:"
Write-Host "  aws lambda invoke --function-name $FunctionName --invocation-type Event --profile $AwsProfile --region $Region --payload '{}' --cli-binary-format raw-in-base64-out out.json"
Write-Host "tail the log:"
Write-Host "  aws logs tail /aws/lambda/$FunctionName --since 30m --follow --profile $AwsProfile --region $Region"
Write-Host "pause: .\pickup_eta_alerts\deploy.ps1 -Paused   (or disable schedule $FunctionName in EventBridge Scheduler)"
