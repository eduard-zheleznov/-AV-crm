param(
    [string]$Root = "D:\avito-crm",
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"

if (-not $OutputDir) {
    $OutputDir = Join-Path ([Environment]::GetFolderPath("Desktop")) "avito-mac-handoff"
}
$envPath = Join-Path $Root ".env"
if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Не найден локальный файл настроек: $envPath"
}

$allowed = @(
    "LPTRACKER_BASE_URL",
    "LPTRACKER_LOGIN",
    "LPTRACKER_PASSWORD",
    "LPTRACKER_PROJECT_ID",
    "LPTRACKER_PROJECT_NAME",
    "LPTRACKER_FIELD_NAME",
    "LPTRACKER_FIELD_VALUE",
    "LPTRACKER_SERVICE_NAME",
    "LPTRACKER_TIMEZONE",
    "ROBOT_HANDOFF_SOURCE_FUNNEL_NAME",
    "ROBOT_HANDOFF_SOURCE_FIELD_VALUES",
    "ROBOT_HANDOFF_TARGET_FUNNEL_NAME",
    "ROBOT_HANDOFF_FIELD_NAME",
    "ROBOT_HANDOFF_FIELD_VALUE",
    "ROBOT_HANDOFF_STAGE_DATE_FIELD_NAME",
    "ROBOT_HANDOFF_STAGE_DELAY_DAYS",
    "ROBOT_HANDOFF_POLL_SECONDS",
    "ROBOT_HANDOFF_LOOKBACK_HOURS",
    "ROBOT_HANDOFF_BATCH_SIZE",
    "ROBOT_HANDOFF_MIN_CONFIDENCE",
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GEMINI_API_BASE_URL",
    "GEMINI_MAX_AUDIO_BYTES",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_PRIMARY_CHAT_IDS",
    "TELEGRAM_BACKUP_CHAT_IDS",
    "TELEGRAM_REMINDER_MINUTES",
    "TELEGRAM_REQUEST_TIMEOUT_SECONDS",
    "TELEGRAM_SEND_ATTEMPTS",
    "MAX_API_BASE_URL",
    "MAX_BOT_TOKEN",
    "MAX_PRIMARY_RECIPIENTS",
    "MAX_BACKUP_RECIPIENTS",
    "MAX_REQUEST_TIMEOUT_SECONDS",
    "MAX_SEND_ATTEMPTS",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_SECURITY",
    "SMTP_USERNAME",
    "SMTP_PASSWORD",
    "SMTP_FROM_ADDRESS",
    "EMAIL_PRIMARY_RECIPIENTS",
    "EMAIL_BACKUP_RECIPIENTS",
    "EMAIL_REQUEST_TIMEOUT_SECONDS",
    "EMAIL_SEND_ATTEMPTS"
)

$allowedSet = @{}
foreach ($name in $allowed) { $allowedSet[$name] = $true }
$values = [ordered]@{}
foreach ($line in [IO.File]::ReadAllLines($envPath)) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
        $name = $Matches[1]
        if ($allowedSet.ContainsKey($name)) {
            $values[$name] = $line
        }
    }
}

$required = @("LPTRACKER_LOGIN", "LPTRACKER_PASSWORD", "GEMINI_API_KEY")
$missing = @($required | Where-Object { -not $values.Contains($_) })
$hasProject = $values.Contains("LPTRACKER_PROJECT_ID") -or $values.Contains("LPTRACKER_PROJECT_NAME")
if (-not $hasProject) { $missing += "LPTRACKER_PROJECT_ID или LPTRACKER_PROJECT_NAME" }
if ($missing.Count -gt 0) {
    throw "Не заполнены обязательные настройки: $($missing -join ', ')"
}

New-Item -ItemType Directory -Path $OutputDir -Force | Out-Null
$destination = Join-Path $OutputDir "handoff.env"
$result = New-Object System.Collections.Generic.List[string]
$result.Add("# Настройки только для Mac-worker. Содержит секреты; не отправлять в чат или Git.")
$result.Add("ROBOT_HANDOFF_ENABLED=true")
foreach ($name in $allowed) {
    if ($values.Contains($name)) { $result.Add([string]$values[$name]) }
}
[IO.File]::WriteAllLines(
    $destination,
    $result,
    (New-Object System.Text.UTF8Encoding($false))
)

try {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
    & icacls.exe $destination /inheritance:r /grant:r "${identity}:(R,W)" | Out-Null
} catch {
    Write-Warning "Не удалось ограничить ACL файла. Не передавайте его через публичные каналы."
}

Write-Host "ГОТОВО: настройки Mac сохранены в $destination" -ForegroundColor Green
Write-Host "Экспортировано параметров: $($result.Count - 2). Значения и секреты не выводились."
