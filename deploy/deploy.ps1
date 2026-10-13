<#
.SYNOPSIS
    Builds the Lambda zip and creates or updates the reload suggester on AWS.

.DESCRIPTION
    Idempotent: safe to re-run. Creates what is missing (bucket, IAM roles, function,
    EventBridge schedule) and updates what exists (code, configuration, schedule).
    Needs the AWS CLI v2 and the project's .venv (or a python on PATH).
    See docs/AWS_DEPLOYMENT.md for the picture and the rollout stages.

.EXAMPLE
    # stage one: everything runs on AWS, nothing is sent, emails land in s3://.../outbox/
    .\deploy\deploy.ps1 -UploadConfig

.EXAMPLE
    # stage three: send through SES with Claude wording, at most 20 emails per pass
    .\deploy\deploy.ps1 -Send -Llm

.EXAMPLE
    # a code change only (no IAM, bucket or schedule work)
    .\deploy\deploy.ps1 -CodeOnly

.EXAMPLE
    # kill switch without touching the schedule
    .\deploy\deploy.ps1 -Paused
#>
[CmdletBinding()]
param(
    [string]$AwsProfile = "paybot-admin",
    [string]$Region = "us-east-1",
    [string]$FunctionName = "circle-reload-suggester",
    [string]$Bucket = "circle-reload-suggestions",
    [string]$CookieBucket = "circle-bot-cookies",
    [string]$CookieKey = "rubicon/cargotel.json",
    # EventBridge Scheduler cron: minutes hours day-of-month month day-of-week year
    [string]$Schedule = "cron(0/15 6-21 ? * MON-SAT *)",
    [string]$ScheduleTimezone = "America/New_York",
    [string]$MailFrom = "Circle Logistics <vehicles@circledelivers.com>",
    [string]$SesIdentity = "vehicles@circledelivers.com",
    [string]$ConfigurationSet = "",
    [string]$ModelId = "us.anthropic.claude-sonnet-5-5",
    [ValidateSet("auto", "pilot")] [string]$Mode = "auto",
    [int]$MaxSends = 20,
    [int]$MemoryMb = 1024,
    [int]$TimeoutSeconds = 900,
    [switch]$Send,
    [switch]$Llm,
    [switch]$SavePages,
    [switch]$Paused,
    [switch]$UploadConfig,
    [switch]$CodeOnly
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

$Root = Split-Path -Parent $PSScriptRoot
$Build = Join-Path $Root "build"
$Dist = Join-Path $Root "dist"
$Zip = Join-Path $Dist "function.zip"
$Aws = @("--profile", $AwsProfile, "--region", $Region)
$RoleName = "$FunctionName-role"
$SchedulerRoleName = "$FunctionName-scheduler-role"
$Modules = @("lambda_handler.py", "dispatch_watcher.py", "send_drafts.py", "suggest_reloads.py",
             "cargotel_search.py", "bedrock_email.py", "gmail_send.py", "pull_load_detail.py")

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
foreach ($m in $Modules) { Copy-Item (Join-Path $Root $m) $Build }
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

# ---------------------------------------------------------------- bucket
Write-Host "== bucket s3://$Bucket"
$found = (Invoke-Aws s3api list-buckets --query "Buckets[?Name=='$Bucket'].Name" --output text).Trim()
if ($found -ne $Bucket) {
    if ($Region -eq "us-east-1") {
        Invoke-Aws s3api create-bucket --bucket $Bucket | Out-Null
    } else {
        Invoke-Aws s3api create-bucket --bucket $Bucket --create-bucket-configuration "LocationConstraint=$Region" | Out-Null
    }
    Write-Host "created"
}
Invoke-Aws s3api put-bucket-versioning --bucket $Bucket --versioning-configuration Status=Enabled | Out-Null
Invoke-Aws s3api put-public-access-block --bucket $Bucket --public-access-block-configuration `
    "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" | Out-Null
foreach ($p in @("config/", "state/", "drafts/", "outbox/", "pages/")) {
    Invoke-Aws s3api put-object --bucket $Bucket --key $p | Out-Null
}
if ($UploadConfig) {
    foreach ($f in @("reps.csv", "do_not_email.txt")) {
        $local = Join-Path $Root $f
        if (Test-Path $local) {
            Invoke-Aws s3 cp $local "s3://$Bucket/config/$f" | Out-Null
            Write-Host "uploaded config/$f"
        } else {
            Write-Host "no local $f; skipped"
        }
    }
}

# ---------------------------------------------------------------- Lambda role
Write-Host "== roles"
$values = @{
    ACCOUNT = $Account; REGION = $Region; BUCKET = $Bucket
    COOKIE_BUCKET = $CookieBucket; COOKIE_KEY = $CookieKey
    MODEL_ID = $ModelId; FOUNDATION_MODEL = ($ModelId -replace '^(us|eu|apac|global)\.', '')
    SES_IDENTITY = $SesIdentity; SES_DOMAIN = ($SesIdentity -replace '^.*@', '')
}
$trustLambda = Join-Path $Build "trust-lambda.json"
Write-Utf8 $trustLambda (Render-Template (Join-Path $PSScriptRoot "trust-lambda.json") $values)
$policyLambda = Join-Path $Build "lambda-policy.json"
Write-Utf8 $policyLambda (Render-Template (Join-Path $PSScriptRoot "lambda-policy.json") $values)
$roleArn = Ensure-Role -Name $RoleName -TrustFile $trustLambda -PolicyFile $policyLambda `
    -ManagedPolicy "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
Write-Host "lambda role $roleArn"

# ---------------------------------------------------------------- function
Write-Host "== function $FunctionName"
$vars = [ordered]@{}
$vars["RELOAD_BUCKET"] = $Bucket
$vars["RELOAD_SEND"] = "0";       if ($Send) { $vars["RELOAD_SEND"] = "1" }
$vars["RELOAD_LLM"] = "0";        if ($Llm) { $vars["RELOAD_LLM"] = "1" }
$vars["RELOAD_SAVE_PAGES"] = "0"; if ($SavePages) { $vars["RELOAD_SAVE_PAGES"] = "1" }
$vars["RELOAD_PAUSED"] = "0";     if ($Paused) { $vars["RELOAD_PAUSED"] = "1" }
$vars["RELOAD_MODE"] = $Mode
$vars["RELOAD_MAX_SENDS"] = [string]$MaxSends
$vars["MAIL_FROM"] = $MailFrom
$vars["BEDROCK_MODEL_ID"] = $ModelId
$vars["TZ"] = "America/New_York"
if ($ConfigurationSet) { $vars["SES_CONFIGURATION_SET"] = $ConfigurationSet }
$envFile = Join-Path $Build "env.json"
Write-Utf8 $envFile (@{ Variables = $vars } | ConvertTo-Json -Compress)

if (-not (Test-FunctionExists)) {
    Invoke-Aws lambda create-function --function-name $FunctionName --runtime python3.12 --architectures x86_64 `
        --handler lambda_handler.handler --role $roleArn --zip-file "fileb://$Zip" `
        --timeout $TimeoutSeconds --memory-size $MemoryMb --environment "file://$envFile" `
        --description "Reload suggestion emails: one CargoTel watcher pass and one sender pass per invocation" | Out-Null
    Invoke-Aws lambda wait function-active-v2 --function-name $FunctionName | Out-Null
    Write-Host "created"
} else {
    Invoke-Aws lambda update-function-code --function-name $FunctionName --zip-file "fileb://$Zip" | Out-Null
    Invoke-Aws lambda wait function-updated-v2 --function-name $FunctionName | Out-Null
    Invoke-Aws lambda update-function-configuration --function-name $FunctionName --runtime python3.12 `
        --handler lambda_handler.handler --role $roleArn --timeout $TimeoutSeconds --memory-size $MemoryMb `
        --environment "file://$envFile" | Out-Null
    Invoke-Aws lambda wait function-updated-v2 --function-name $FunctionName | Out-Null
    Write-Host "updated code and configuration"
}
# Never two passes at once: they would race on the state files.
Invoke-Aws lambda put-function-concurrency --function-name $FunctionName --reserved-concurrent-executions 1 | Out-Null
$functionArn = (Invoke-Aws lambda get-function --function-name $FunctionName --query Configuration.FunctionArn --output text).Trim()

# ---------------------------------------------------------------- schedule
Write-Host "== schedule $Schedule ($ScheduleTimezone)"
$values["FUNCTION_ARN"] = $functionArn
$trustSched = Join-Path $Build "trust-scheduler.json"
Write-Utf8 $trustSched (Render-Template (Join-Path $PSScriptRoot "trust-scheduler.json") $values)
$policySched = Join-Path $Build "scheduler-policy.json"
Write-Utf8 $policySched (Render-Template (Join-Path $PSScriptRoot "scheduler-policy.json") $values)
$schedRoleArn = Ensure-Role -Name $SchedulerRoleName -TrustFile $trustSched -PolicyFile $policySched
$targetFile = Join-Path $Build "target.json"
Write-Utf8 $targetFile (@{ Arn = $functionArn; RoleArn = $schedRoleArn; Input = "{}"; RetryPolicy = @{ MaximumRetryAttempts = 0 } } | ConvertTo-Json -Compress)
$schedArgs = @("--name", $FunctionName, "--schedule-expression", $Schedule, "--schedule-expression-timezone", $ScheduleTimezone,
               "--flexible-time-window", "Mode=OFF", "--target", "file://$targetFile",
               "--description", "Runs the reload suggester every 15 minutes during dispatch hours")
$existing = (Invoke-Aws scheduler list-schedules --name-prefix $FunctionName --query "Schedules[?Name=='$FunctionName'].Name" --output text).Trim()
if ($existing -eq $FunctionName) {
    Invoke-Aws scheduler update-schedule @schedArgs | Out-Null
    Write-Host "updated"
} else {
    Invoke-Aws scheduler create-schedule @schedArgs | Out-Null
    Write-Host "created"
}

# ---------------------------------------------------------------- summary
$sendState = "OFF (emails go to s3://$Bucket/outbox/)"
if ($Send) { $sendState = "ON via SES as $MailFrom" }
Write-Host ""
Write-Host "done. sending $sendState; mode $Mode; wording $(if ($Llm) { 'Claude on Bedrock' } else { 'template' }); paused $($Paused.IsPresent)"
Write-Host "run one pass now:"
Write-Host "  aws lambda invoke --function-name $FunctionName --profile $AwsProfile --region $Region --payload '{}' --cli-binary-format raw-in-base64-out out.json; Get-Content out.json"
Write-Host "tail the log:"
Write-Host "  aws logs tail /aws/lambda/$FunctionName --since 30m --follow --profile $AwsProfile --region $Region"
Write-Host "pause: .\deploy\deploy.ps1 -Paused   (or disable schedule $FunctionName in EventBridge Scheduler)"
