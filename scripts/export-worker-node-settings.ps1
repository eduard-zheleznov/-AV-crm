[CmdletBinding()]
param(
    [string]$SourceRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DestinationRoot = ([Environment]::GetFolderPath("Desktop")),
    [switch]$PassThru
)

$ErrorActionPreference = "Stop"

function Get-EnvValue {
    param([string]$Text, [string]$Name)
    $Pattern = "(?m)^" + [regex]::Escape($Name) + "=([^`r`n]*)"
    $Matches = [regex]::Matches($Text, $Pattern)
    if ($Matches.Count -gt 1) { throw "$Name встречается в .env несколько раз" }
    if ($Matches.Count -eq 0) { return $null }
    return $Matches[0].Groups[1].Value.Trim().Trim('"')
}

$SourceRoot = (Resolve-Path -LiteralPath $SourceRoot).Path
$EnvPath = Join-Path $SourceRoot ".env"
if (-not (Test-Path -LiteralPath $EnvPath -PathType Leaf)) {
    throw "Не найден $EnvPath"
}
$EnvText = [IO.File]::ReadAllText($EnvPath)
$GooglePathValue = Get-EnvValue $EnvText "GOOGLE_CREDENTIALS_FILE"
if ([string]::IsNullOrWhiteSpace($GooglePathValue)) {
    throw "В .env не задан GOOGLE_CREDENTIALS_FILE"
}
$GooglePathValue = [Environment]::ExpandEnvironmentVariables($GooglePathValue)
if ([IO.Path]::IsPathRooted($GooglePathValue)) {
    $GooglePath = $GooglePathValue
}
else {
    $GooglePath = Join-Path $SourceRoot $GooglePathValue
}
if (-not (Test-Path -LiteralPath $GooglePath -PathType Leaf)) {
    throw "Google service-account JSON не найден по пути из .env"
}

try {
    $GooglePayload = Get-Content -LiteralPath $GooglePath -Raw -Encoding UTF8 | ConvertFrom-Json
}
catch {
    throw "Google JSON не читается: $($_.Exception.Message)"
}
if (
    $GooglePayload.type -ne "service_account" -or
    [string]::IsNullOrWhiteSpace([string]$GooglePayload.client_email) -or
    [string]::IsNullOrWhiteSpace([string]$GooglePayload.private_key)
) {
    throw "Файл GOOGLE_CREDENTIALS_FILE не является ключом Google service account"
}

if (-not (Test-Path -LiteralPath $DestinationRoot -PathType Container)) {
    throw "Каталог назначения не существует: $DestinationRoot"
}
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$TransferDir = Join-Path $DestinationRoot "AvitoCrm-secure-transfer-$Timestamp"
if (Test-Path -LiteralPath $TransferDir) {
    throw "Каталог переноса уже существует: $TransferDir"
}
New-Item -ItemType Directory -Path $TransferDir | Out-Null

$CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
& icacls.exe $TransferDir /inheritance:r /grant:r "${CurrentIdentity}:(OI)(CI)(F)" "*S-1-5-32-544:(OI)(CI)(F)" | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось ограничить ACL папки переноса"
}

$TargetEnv = Join-Path $TransferDir "vds.env"
$TargetGoogle = Join-Path $TransferDir "google-service-account.json"
Copy-Item -LiteralPath $EnvPath -Destination $TargetEnv
Copy-Item -LiteralPath $GooglePath -Destination $TargetGoogle

$Git = Get-Command git.exe -ErrorAction SilentlyContinue
$Commit = "unknown"
if ($Git -and (Test-Path -LiteralPath (Join-Path $SourceRoot ".git") -PathType Container)) {
    $Commit = (& $Git.Source -C $SourceRoot rev-parse HEAD 2>$null).Trim()
    if ($LASTEXITCODE -ne 0) { $Commit = "unknown" }
}
$Manifest = [ordered]@{
    created_at = (Get-Date).ToUniversalTime().ToString("o")
    source_computer = $env:COMPUTERNAME
    source_commit = $Commit
    env_sha256 = (Get-FileHash -LiteralPath $TargetEnv -Algorithm SHA256).Hash
    google_sha256 = (Get-FileHash -LiteralPath $TargetGoogle -Algorithm SHA256).Hash
    contains_secrets = $true
}
$Manifest | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $TransferDir "transfer-manifest.json") -Encoding UTF8
$Readme = @"
СЕКРЕТНАЯ ПАПКА AVITO CRM

Содержит .env и Google service-account key.
Не отправляйте её в чат, email или GitHub.
Подключайте к VM только как read-only shared folder.
После импорта отключите shared folder и удалите эту папку штатным способом.
"@
Set-Content -LiteralPath (Join-Path $TransferDir "README-SECRET.txt") -Value $Readme -Encoding UTF8

Write-Host "SETTINGS EXPORT OK: $TransferDir" -ForegroundColor Green
Write-Host "Значения секретов не выводились. Папка доступна только текущему пользователю и администраторам."
if ($PassThru) { return $TransferDir }
