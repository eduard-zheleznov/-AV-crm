[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$TransferDir,
    [string]$ComputerLabel = "$env:COMPUTERNAME VM",
    [string]$TaskName = "Avito CRM Remote Control"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$ExpectedAppVersion = "1.12.43"
$ExpectedExtensionVersion = "1.0.19"
$StopPath = Join-Path $ProjectRoot "data\STOP"
$WorkerLock = Join-Path $ProjectRoot "data\worker.lock"
$StatePath = Join-Path $ProjectRoot "data\remote-control.json"

function Test-ActiveLock {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $Payload = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        [Diagnostics.Process]::GetProcessById([int]$Payload.pid) | Out-Null
        return $true
    }
    catch {
        return $false
    }
}

if (Test-ActiveLock -Path $WorkerLock) {
    throw "Рабочий процесс активен. Настройка не начата."
}
if (Test-Path -LiteralPath $StatePath -PathType Leaf) {
    throw "Есть незавершённая локальная команда. Настройка не начата."
}
$ExistingTask = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($ExistingTask -and $ExistingTask.State -ne "Disabled") {
    throw "Задача '$TaskName' уже существует и не отключена. Скрипт её не изменял."
}

$TransferDir = (Resolve-Path -LiteralPath $TransferDir).Path
$ManifestPath = Join-Path $TransferDir "transfer-manifest.json"
$TransferEnv = Join-Path $TransferDir "vds.env"
$TransferGoogle = Join-Path $TransferDir "google-service-account.json"
foreach ($RequiredPath in @($ManifestPath, $TransferEnv, $TransferGoogle)) {
    if (-not (Test-Path -LiteralPath $RequiredPath -PathType Leaf)) {
        throw "Не найден файл переноса: $RequiredPath"
    }
}
$Manifest = Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
$ActualEnvHash = (Get-FileHash -LiteralPath $TransferEnv -Algorithm SHA256).Hash
$ActualGoogleHash = (Get-FileHash -LiteralPath $TransferGoogle -Algorithm SHA256).Hash
if (
    $ActualEnvHash -ne [string]$Manifest.env_sha256 -or
    $ActualGoogleHash -ne [string]$Manifest.google_sha256
) {
    throw "Хеши папки переноса не совпали. Импорт отменён."
}

$Git = Get-Command git.exe -ErrorAction Stop
$ActualCommit = (& $Git.Source -C $ProjectRoot rev-parse HEAD).Trim()
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$ActualAppVersion = (& $Python -c "import avito_crm; print(avito_crm.__version__)").Trim()
$ManifestVersion = (Get-Content -LiteralPath (Join-Path $ProjectRoot "chrome-extension\manifest.json") -Raw -Encoding UTF8 | ConvertFrom-Json).version
$InstallMarkerPath = Join-Path $ProjectRoot "data\worker-node-install.json"
if (-not (Test-Path -LiteralPath $InstallMarkerPath -PathType Leaf)) {
    throw "Нет маркера install-worker-node.ps1. Настройка отменена."
}
$InstallMarker = Get-Content -LiteralPath $InstallMarkerPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (
    $ActualCommit -ne [string]$InstallMarker.commit -or
    $ActualAppVersion -ne $ExpectedAppVersion -or
    $ManifestVersion -ne $ExpectedExtensionVersion -or
    $ActualAppVersion -ne [string]$InstallMarker.app_version -or
    $ManifestVersion -ne [string]$InstallMarker.extension_version
) {
    throw "Версия узла не прошла контроль: app=$ActualAppVersion; extension=$ManifestVersion; commit=$ActualCommit"
}

New-Item -ItemType Directory -Path (Split-Path -Parent $StopPath) -Force | Out-Null
New-Item -ItemType File -Path $StopPath -Force | Out-Null
& (Join-Path $PSScriptRoot "import-vds-settings.ps1") -TransferDir $TransferDir -NotificationComputerName $ComputerLabel
& (Join-Path $PSScriptRoot "doctor.ps1") -Source google -OnlineCrm
try {
    & (Join-Path $PSScriptRoot "install-remote-control.ps1") -TaskName $TaskName -NoStart -SkipGoogleSetup -Disabled
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    if ($Task.State -ne "Disabled") {
        throw "контрольная задача не осталась отключённой"
    }
}
catch {
    New-Item -ItemType File -Path $StopPath -Force | Out-Null
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
    throw "Пассивная регистрация откачена: $($_.Exception.Message)"
}
if (-not (Test-Path -LiteralPath $StopPath -PathType Leaf)) {
    throw "STOP не сохранён."
}

Write-Host ""
Write-Host "WORKER NODE READY (PASSIVE): app=$ActualAppVersion; extension=$ManifestVersion; commit=$ActualCommit" -ForegroundColor Green
Write-Host "Google и LPTracker проверены без записи."
Write-Host "STOP: есть; пульт: Disabled; B4/B5 и очередь не изменялись."
Write-Host "Не включайте этот пульт, пока прежний компьютер не будет штатно отключён."
