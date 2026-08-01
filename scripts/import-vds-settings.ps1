[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TransferDir
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvPath = Join-Path $ProjectRoot ".env"
$SourceEnv = Join-Path $TransferDir "vds.env"
$SourceGoogle = Join-Path $TransferDir "google-service-account.json"
$DataDir = Join-Path $ProjectRoot "data"
$TargetGoogle = Join-Path $DataDir "google-service-account.json"
$ExtensionConfigPath = Join-Path $ProjectRoot "chrome-extension\config.local.js"

function Get-EnvValue {
    param(
        [string]$Text,
        [string]$Name
    )

    # Do not anchor on `$`: .NET treats LF and CRLF differently in multiline
    # mode. Excluding both newline characters gives the same value for either.
    $Pattern = "(?m)^" + [regex]::Escape($Name) + "=([^`r`n]*)"
    $Matches = [regex]::Matches($Text, $Pattern)
    if ($Matches.Count -gt 1) {
        throw "$Name встречается в .env несколько раз"
    }
    if ($Matches.Count -eq 0) {
        return $null
    }
    return $Matches[0].Groups[1].Value
}

function Set-EnvValue {
    param(
        [string]$Text,
        [string]$Name,
        [string]$Value
    )

    if ($Value.Contains("`r") -or $Value.Contains("`n")) {
        throw "$Name содержит недопустимый перевод строки"
    }
    $Pattern = "(?m)^" + [regex]::Escape($Name) + "=[^`r`n]*"
    $Count = [regex]::Matches($Text, $Pattern).Count
    if ($Count -gt 1) {
        throw "$Name встречается в .env несколько раз"
    }
    $Line = "$Name=$Value"
    if ($Count -eq 1) {
        return [regex]::Replace($Text, $Pattern, $Line)
    }
    if (-not $Text.EndsWith("`n")) {
        $Text += "`r`n"
    }
    return $Text + $Line + "`r`n"
}

foreach ($RequiredPath in @($EnvPath, $SourceEnv, $SourceGoogle)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Не найден файл: $RequiredPath"
    }
}

$LocalText = [System.IO.File]::ReadAllText($EnvPath)
$MergedText = [System.IO.File]::ReadAllText($SourceEnv)

if (-not (Test-Path -LiteralPath $ExtensionConfigPath -PathType Leaf)) {
    throw "Не найдена локальная конфигурация расширения обычного Chrome"
}
$ExtensionConfigText = [System.IO.File]::ReadAllText($ExtensionConfigPath)
$TokenMatch = [regex]::Match(
    $ExtensionConfigText,
    'token\s*:\s*"([A-Za-z0-9_-]{32,})"'
)
if (-not $TokenMatch.Success) {
    throw "В config.local.js не найден токен моста обычного Chrome"
}
[string]$LocalToken = $TokenMatch.Groups[1].Value

$SpreadsheetId = Get-EnvValue $MergedText "GOOGLE_SPREADSHEET_ID"
$Login = Get-EnvValue $MergedText "LPTRACKER_LOGIN"
$Password = Get-EnvValue $MergedText "LPTRACKER_PASSWORD"
$ProjectId = Get-EnvValue $MergedText "LPTRACKER_PROJECT_ID"
$ProjectName = Get-EnvValue $MergedText "LPTRACKER_PROJECT_NAME"
if ([string]::IsNullOrWhiteSpace($SpreadsheetId)) {
    throw "В vds.env не задан GOOGLE_SPREADSHEET_ID"
}
if ([string]::IsNullOrWhiteSpace($Login) -or $Login -eq "operator@example.com") {
    throw "В vds.env не задан LPTRACKER_LOGIN"
}
if ([string]::IsNullOrWhiteSpace($Password) -or $Password -eq "replace-me") {
    throw "В vds.env не задан LPTRACKER_PASSWORD"
}
if ([string]::IsNullOrWhiteSpace($ProjectId) -and [string]::IsNullOrWhiteSpace($ProjectName)) {
    throw "В vds.env не задан проект LPTracker"
}

try {
    $GooglePayload = Get-Content -LiteralPath $SourceGoogle -Raw | ConvertFrom-Json
}
catch {
    throw "Не удалось прочитать Google JSON: $($_.Exception.Message)"
}
if (
    $GooglePayload.type -ne "service_account" -or
    [string]::IsNullOrWhiteSpace([string]$GooglePayload.client_email) -or
    [string]::IsNullOrWhiteSpace([string]$GooglePayload.private_key)
) {
    throw "Переданный JSON не является ключом Google service account"
}

# Preserve only settings that identify this physical computer and its ordinary Chrome.
$LocalSettingNames = @(
    "APP_DATA_DIR",
    "APP_OUTPUT_DIR",
    "APP_LOGS_DIR",
    "AVITO_HEADLESS",
    "AVITO_BROWSER_CHANNEL",
    "AVITO_PROFILE_DIR",
    "AVITO_EXTENSION_PORT",
    "AVITO_EXTENSION_TOKEN",
    "AVITO_EXTENSION_CONNECT_TIMEOUT_SECONDS",
    "AVITO_SCREENSHOT_DIR",
    "TESSERACT_CMD"
)
foreach ($Name in $LocalSettingNames) {
    $Value = Get-EnvValue $LocalText $Name
    if ($null -ne $Value) {
        $MergedText = Set-EnvValue $MergedText $Name $Value
    }
}

$MergedText = Set-EnvValue $MergedText "AVITO_BROWSER_DRIVER" "chrome_extension"
$MergedText = Set-EnvValue $MergedText "AVITO_EXTENSION_TOKEN" $LocalToken
$MergedText = Set-EnvValue $MergedText "GOOGLE_CREDENTIALS_FILE" $TargetGoogle
# The new computer must never create live leads outside the approved local window.
$MergedText = Set-EnvValue $MergedText "LOCAL_TIME_GUARD_ENABLED" "true"

New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
$Timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$EnvBackup = Join-Path $DataDir ".env.before-vds-import-$Timestamp"
Copy-Item -LiteralPath $EnvPath -Destination $EnvBackup -Force
if (Test-Path -LiteralPath $TargetGoogle) {
    Copy-Item -LiteralPath $TargetGoogle -Destination "$TargetGoogle.before-$Timestamp" -Force
}
Copy-Item -LiteralPath $SourceGoogle -Destination $TargetGoogle -Force

$TempEnv = Join-Path $ProjectRoot ".env.importing"
$Utf8Bom = New-Object System.Text.UTF8Encoding($true)
try {
    [System.IO.File]::WriteAllText($TempEnv, $MergedText, $Utf8Bom)
    Move-Item -LiteralPath $TempEnv -Destination $EnvPath -Force
}
finally {
    Remove-Item -LiteralPath $TempEnv -Force -ErrorAction SilentlyContinue
}

$WrittenText = [System.IO.File]::ReadAllText($EnvPath)
[string]$WrittenToken = Get-EnvValue $WrittenText "AVITO_EXTENSION_TOKEN"
if (-not [string]::Equals($WrittenToken, $LocalToken, [System.StringComparison]::Ordinal)) {
    throw "Контроль импорта не пройден: токен Chrome изменился"
}
if ((Get-EnvValue $WrittenText "AVITO_BROWSER_DRIVER") -ne "chrome_extension") {
    throw "Контроль импорта не пройден: не сохранён обычный Chrome"
}
[string]$WrittenGooglePath = Get-EnvValue $WrittenText "GOOGLE_CREDENTIALS_FILE"
if (
    [string]::IsNullOrWhiteSpace($WrittenGooglePath) -or
    -not (Test-Path -LiteralPath $WrittenGooglePath -PathType Leaf)
) {
    throw "Контроль импорта не пройден: не записан Google JSON"
}

Write-Host "Настройки VDS импортированы без вывода секретов." -ForegroundColor Green
Write-Host "Обычный Chrome: сохранён."
Write-Host "Защита местного времени 10:00–19:45: включена."
Write-Host "Резервная копия .env: $EnvBackup"
Write-Host "После успешной проверки удалите папку переноса: $TransferDir"
