<#
.SYNOPSIS
    Builds the Lambda zip and creates or updates the sales rep sync on AWS.

.DESCRIPTION
    Idempotent, modelled on ..\duplicate_check\deploy.ps1. Shares the reload suggester's
    bucket but has its own function, roles, prefix and schedule. No email: the job only
    reads two CargoTel screens and presses the "Primary Sales Rep (Bulk)" widget.
    Needs the AWS CLI v2 and the project's .venv (or a python on PATH).

.EXAMPLE
    # stage one: runs on AWS, nothing written; the plan lands in s3://.../sales-rep-sync/outbox/
    .\sales_rep_sync\deploy.ps1

.EXAMPLE
    # live: fill the empty Sales Rep 1 of every load on Bot Pipelines > Sales Rep Edit
    .\sales_rep_sync\deploy.ps1 -Apply

.EXAMPLE
    # a code change only
    .\sales_rep_sync\deploy.ps1 -CodeOnly

.EXAMPLE
    # kill switch without touching the schedule
    .\sales_rep_sync\deploy.ps1 -Paused
#>
[CmdletBinding()]
param(
    [string]$AwsProfile = "paybot-admin",
    [string]$Region = "us-east-1",
    [string]$FunctionName = "circle-sales-rep-sync",
    [string]$Bucket = "circle-reload-suggestions",
    [string]$Prefix = "sales-rep-sync/",
    [string]$CookieBucket = "circle-bot-cookies",
    [string]$CookieKey = "rubicon/cargotel.json",
    # Every two hours (the user's choice, 2026-11-09) at :30 from 06:30 to 20:30 Eastern, seven
    # days a week: half an hour after the tracking-update alerts (which run on the hour) so the
    # two never post to CargoTel at once, and before the other jobs look for a load's sales rep.
    [string]$Schedule = "cron(30 6-20/2 * * ? *)",
    [string]$ScheduleTimezone = "America/New_York",
    [int]$MaxChanges = 200,
    [int]$MemoryMb = 1024,
    [int]$TimeoutSeconds = 900,
    [switch]$Apply,
    [switch]$Overwrite,
    [switch]$SavePages,
    [switch]$Paused,
    [switch]$CodeOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

$Here = $PSScriptRoot                       # sales_rep_sync/
$Root = Split-Path -Parent $Here            # the project folder with the CargoTel modules
$Build = Join-Path $Here "build"
$Dist = Join-Path $Here "dist"
$Zip = Join-Path $Dist "function.zip"
$Aws = @("--profile", $AwsProfile, "--region", $Region)
$RoleName = "$FunctionName-role"
$SchedulerRoleName = "$FunctionName-scheduler-role"
# Everything lambda_handler's import chain needs, flat at the zip root.
$SharedModules = @("lambda_handler.py", "dispatch_watcher.py", "send_drafts.py", "suggest_reloads.py",
                   "cargotel_search.py", "bedrock_email.py", "gmail_send.py", "pull_load_detail.py")
$OwnModules = @("sales_rep_sync.py", "sales_rep_lambda.py")

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
foreach ($p in @("state/", "reports/", "outbox/", "pages/")) {
    Invoke-Aws s3api put-object --bucket $Bucket --key "$Prefix$p" | Out-Null
}

# ---------------------------------------------------------------- Lambda role
Write-Host "== roles"
$values = @{
    ACCOUNT = $Account; REGION = $Region; BUCKET = $Bucket; PREFIX = $Prefix
    COOKIE_BUCKET = $CookieBucket; COOKIE_KEY = $CookieKey
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
$vars["SRS_BUCKET"] = $Bucket
$vars["SRS_PREFIX"] = $Prefix
$vars["SRS_APPLY"] = "0";       if ($Apply) { $vars["SRS_APPLY"] = "1" }
$vars["SRS_OVERWRITE"] = "0";   if ($Overwrite) { $vars["SRS_OVERWRITE"] = "1" }
$vars["SRS_SAVE_PAGES"] = "0";  if ($SavePages) { $vars["SRS_SAVE_PAGES"] = "1" }
$vars["SRS_PAUSED"] = "0";      if ($Paused) { $vars["SRS_PAUSED"] = "1" }
$vars["SRS_MAX_CHANGES"] = [string]$MaxChanges
$vars["TZ"] = "America/New_York"
$envFile = Join-Path $Build "env.json"
Write-Utf8 $envFile (@{ Variables = $vars } | ConvertTo-Json -Compress)

if (-not (Test-FunctionExists)) {
    Invoke-Aws lambda create-function --function-name $FunctionName --runtime python3.12 --architectures x86_64 `
        --handler sales_rep_lambda.handler --role $roleArn --zip-file "fileb://$Zip" `
        --timeout $TimeoutSeconds --memory-size $MemoryMb --environment "file://$envFile" `
        --description "Sales rep sync: gives the unassigned loads on Bot Pipelines > Sales Rep Edit the Default Primary Rep of their Bill To customer (Customer List)" | Out-Null
    Invoke-Aws lambda wait function-active-v2 --function-name $FunctionName | Out-Null
    Write-Host "created"
} else {
    Invoke-Aws lambda update-function-code --function-name $FunctionName --zip-file "fileb://$Zip" | Out-Null
    Invoke-Aws lambda wait function-updated-v2 --function-name $FunctionName | Out-Null
    Invoke-Aws lambda update-function-configuration --function-name $FunctionName --runtime python3.12 `
        --handler sales_rep_lambda.handler --role $roleArn --timeout $TimeoutSeconds --memory-size $MemoryMb `
        --environment "file://$envFile" | Out-Null
    Invoke-Aws lambda wait function-updated-v2 --function-name $FunctionName | Out-Null
    Write-Host "updated code and configuration"
}
# Never two runs at once: they would post the same loads twice.
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
               "--description", "Runs the sales rep sync (Bot Pipelines > Sales Rep Edit) every two hours at :30 from 06:30 to 20:30 Eastern")
$existing = (Invoke-Aws scheduler list-schedules --name-prefix $FunctionName --query "Schedules[?Name=='$FunctionName'].Name" --output text).Trim()
if ($existing -eq $FunctionName) {
    Invoke-Aws scheduler update-schedule @schedArgs | Out-Null
    Write-Host "updated"
} else {
    Invoke-Aws scheduler create-schedule @schedArgs | Out-Null
    Write-Host "created"
}

# ---------------------------------------------------------------- summary
$applyState = "OFF (dry run; the plan goes to s3://$Bucket/$($Prefix)outbox/)"
if ($Apply) { $applyState = "ON (the bulk widget is pressed in CargoTel)" }
Write-Host ""
Write-Host "done. applying $applyState; overwrite $($Overwrite.IsPresent); max $MaxChanges changes per run; paused $($Paused.IsPresent)"
Write-Host "run one pass now:"
Write-Host "  aws lambda invoke --function-name $FunctionName --invocation-type Event --profile $AwsProfile --region $Region --payload '{}' --cli-binary-format raw-in-base64-out out.json"
Write-Host "tail the log:"
Write-Host "  aws logs tail /aws/lambda/$FunctionName --since 30m --follow --profile $AwsProfile --region $Region"
Write-Host "pause: .\sales_rep_sync\deploy.ps1 -Paused   (or disable schedule $FunctionName in EventBridge Scheduler)"
