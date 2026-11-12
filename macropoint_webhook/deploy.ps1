<#
.SYNOPSIS
    Creates or updates the MacroPoint Location Updates receiver on AWS (Lambda + public
    function URL guarded by a secret path segment).

.DESCRIPTION
    Idempotent. The function writes the newest MacroPoint ping per CargoTel load to
    s3://<Bucket>/<Prefix>latest/<load id>.json, which the hourly tracking-update job reads.
    The callback URL to enter in MacroPoint (Manage Company > Company Callback Urls >
    Location Updates) is written to macropoint_webhook\callback_url.txt, which is
    git-ignored: it contains the secret. Nothing is changed in MacroPoint by this script.

.EXAMPLE
    .\macropoint_webhook\deploy.ps1                 # create or update, keep the existing secret
    .\macropoint_webhook\deploy.ps1 -CodeOnly       # code change only
    .\macropoint_webhook\deploy.ps1 -RotateToken    # new secret: the URL in MacroPoint must then be updated
#>
[CmdletBinding()]
param(
    [string]$AwsProfile = "paybot-admin",
    [string]$Region = "us-east-1",
    [string]$FunctionName = "circle-macropoint-webhook",
    [string]$Bucket = "circle-reload-suggestions",
    [string]$Prefix = "macropoint/",
    [string]$IdPattern = '^\d{6}$',
    [int]$MemoryMb = 128,
    [int]$TimeoutSeconds = 10,
    [switch]$CodeOnly,
    [switch]$RotateToken
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2

$Here = $PSScriptRoot
$Root = Split-Path -Parent $Here
$Build = Join-Path $Here "build"
$Dist = Join-Path $Here "dist"
$Zip = Join-Path $Dist "function.zip"
$UrlFile = Join-Path $Here "callback_url.txt"
$Aws = @("--profile", $AwsProfile, "--region", $Region)
$RoleName = "$FunctionName-role"

function Invoke-Aws {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$CliArgs)
    $out = & aws @CliArgs @Aws
    if ($LASTEXITCODE -ne 0) { throw "aws $($CliArgs -join ' ') failed (exit $LASTEXITCODE)" }
    return ($out -join "`n")
}

function Write-Utf8 {
    param([string]$Path, [string]$Text)
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

function Test-FunctionExists {
    $name = (Invoke-Aws lambda list-functions --query "Functions[?FunctionName=='$FunctionName'].FunctionName" --output text).Trim()
    return ($name -eq $FunctionName)
}

function New-Token {
    $bytes = New-Object byte[] 24
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    return (($bytes | ForEach-Object { $_.ToString("x2") }) -join "")
}

Write-Host "== account"
$Account = (Invoke-Aws sts get-caller-identity --query Account --output text).Trim()
Write-Host "account $Account, region $Region, profile $AwsProfile"

# ---------------------------------------------------------------- build (no dependencies: boto3 is in the runtime)
Write-Host "== build $Zip"
if (Test-Path $Build) { Remove-Item -Recurse -Force $Build }
New-Item -ItemType Directory -Force $Build | Out-Null
New-Item -ItemType Directory -Force $Dist | Out-Null
Copy-Item (Join-Path $Here "macropoint_webhook.py") $Build
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { $Python = "python" }
if (Test-Path $Zip) { Remove-Item $Zip }
& $Python -c "import shutil, sys; shutil.make_archive(sys.argv[1], 'zip', sys.argv[2])" ($Zip -replace '\.zip$', '') $Build
if ($LASTEXITCODE -ne 0) { throw "zip failed" }

$exists = Test-FunctionExists
if ($CodeOnly) {
    if (-not $exists) { throw "function $FunctionName does not exist yet; run without -CodeOnly first" }
    Invoke-Aws lambda update-function-code --function-name $FunctionName --zip-file "fileb://$Zip" | Out-Null
    Invoke-Aws lambda wait function-updated-v2 --function-name $FunctionName | Out-Null
    Write-Host "code updated"
    exit 0
}

# ---------------------------------------------------------------- role
Write-Host "== role $RoleName"
$trust = Join-Path $Build "trust.json"
Write-Utf8 $trust (Get-Content -Raw (Join-Path $Root "deploy\trust-lambda.json"))
$policy = Join-Path $Build "policy.json"
Write-Utf8 $policy ((Get-Content -Raw (Join-Path $Here "deploy\lambda-policy.json")).Replace("{{BUCKET}}", $Bucket).Replace("{{PREFIX}}", $Prefix))
$roleArn = (Invoke-Aws iam list-roles --query "Roles[?RoleName=='$RoleName'].Arn" --output text).Trim()
if (-not $roleArn) {
    $roleArn = (Invoke-Aws iam create-role --role-name $RoleName --assume-role-policy-document "file://$trust" --query Role.Arn --output text).Trim()
    Write-Host "created role; waiting for IAM to propagate"
    Start-Sleep -Seconds 12
}
Invoke-Aws iam put-role-policy --role-name $RoleName --policy-name "$RoleName-policy" --policy-document "file://$policy" | Out-Null
Invoke-Aws iam attach-role-policy --role-name $RoleName --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole" | Out-Null

# ---------------------------------------------------------------- secret path segment (kept across deploys)
$token = ""
if ($exists -and -not $RotateToken) {
    $token = (Invoke-Aws lambda get-function-configuration --function-name $FunctionName --query "Environment.Variables.MP_PATH_TOKEN" --output text).Trim()
    if ($token -eq "None") { $token = "" }
}
if (-not $token) { $token = New-Token; Write-Host "new secret path segment generated" }

$envFile = Join-Path $Build "env.json"
$vars = [ordered]@{ MP_PATH_TOKEN = $token; MP_BUCKET = $Bucket; MP_PREFIX = $Prefix; MP_ID_PATTERN = $IdPattern; MP_HISTORY = "20" }
Write-Utf8 $envFile (@{ Variables = $vars } | ConvertTo-Json -Compress)

# ---------------------------------------------------------------- function
Write-Host "== function $FunctionName"
if (-not $exists) {
    Invoke-Aws lambda create-function --function-name $FunctionName --runtime python3.12 --architectures x86_64 `
        --handler macropoint_webhook.handler --role $roleArn --zip-file "fileb://$Zip" `
        --timeout $TimeoutSeconds --memory-size $MemoryMb --environment "file://$envFile" `
        --description "MacroPoint Location Updates callback: keeps the newest ping per CargoTel load in S3" | Out-Null
    Invoke-Aws lambda wait function-active-v2 --function-name $FunctionName | Out-Null
    Write-Host "created"
} else {
    Invoke-Aws lambda update-function-code --function-name $FunctionName --zip-file "fileb://$Zip" | Out-Null
    Invoke-Aws lambda wait function-updated-v2 --function-name $FunctionName | Out-Null
    Invoke-Aws lambda update-function-configuration --function-name $FunctionName --runtime python3.12 `
        --handler macropoint_webhook.handler --role $roleArn --timeout $TimeoutSeconds --memory-size $MemoryMb `
        --environment "file://$envFile" | Out-Null
    Invoke-Aws lambda wait function-updated-v2 --function-name $FunctionName | Out-Null
    Write-Host "updated"
}
# A ping is answered in milliseconds; no retries of a failed async call (there are none: function URLs are synchronous).

# ---------------------------------------------------------------- public function URL (the secret path segment is the guard)
Write-Host "== function URL"
$url = ""
try {
    $url = (Invoke-Aws lambda get-function-url-config --function-name $FunctionName --query FunctionUrl --output text).Trim()
} catch { $url = "" }
if (-not $url) {
    $url = (Invoke-Aws lambda create-function-url-config --function-name $FunctionName --auth-type NONE --query FunctionUrl --output text).Trim()
    Write-Host "created $url"
}
# Public invoke through the URL needs both permissions (lambda:InvokeFunctionUrl, and since late 2025 lambda:InvokeFunction
# limited to calls made through the function URL). "already exists" errors on a re-run are expected.
$null = & aws lambda add-permission --function-name $FunctionName --statement-id FunctionUrlPublicAccess `
    --action lambda:InvokeFunctionUrl --principal "*" --function-url-auth-type NONE @Aws 2>$null
$null = & aws lambda add-permission --function-name $FunctionName --statement-id FunctionUrlPublicInvoke `
    --action lambda:InvokeFunction --principal "*" --invoked-via-function-url @Aws 2>$null

$callback = $url.TrimEnd("/") + "/" + $token
Write-Utf8 $UrlFile ($callback + "`n")
Write-Host ""
Write-Host "done. Function URL $url"
Write-Host "The callback URL for MacroPoint (Location Updates, Authentication Type: No Auth) is in $UrlFile (git-ignored; it holds the secret)."
Write-Host "Nothing was changed in MacroPoint."
