[CmdletBinding()]
param(
    [string]$TaskName = "Avito CRM Remote Control",
    [ValidateRange(30, 1800)]
    [int]$SafeStopTimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$WorkerLock = Join-Path $ProjectRoot "data\worker.lock"
$ControllerLock = Join-Path $ProjectRoot "data\remote-control.lock"
$StatePath = Join-Path $ProjectRoot "data\remote-control.json"
$StopPath = Join-Path $ProjectRoot "data\STOP"

function Test-ActiveLock {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    try {
        $Payload = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        [Diagnostics.Process]::GetProcessById([int]$Payload.pid) | Out-Null
        return $true
    }
    catch { return $false }
}

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $Task) {
    throw "Задача '$TaskName' не найдена."
}
if (-not (Test-Path -LiteralPath $Python -PathType Leaf)) {
    throw "Python-окружение не найдено."
}

& $Python -m avito_crm --root $ProjectRoot stop | Out-Null
$Deadline = (Get-Date).AddSeconds($SafeStopTimeoutSeconds)
while (((Test-ActiveLock -Path $WorkerLock) -or (Test-Path -LiteralPath $StatePath)) -and ((Get-Date) -lt $Deadline)) {
    Start-Sleep -Seconds 2
}
if ((Test-ActiveLock -Path $WorkerLock) -or (Test-Path -LiteralPath $StatePath)) {
    throw "Узел не успел штатно завершить команду. Пульт не отключался."
}

Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
Disable-ScheduledTask -TaskName $TaskName | Out-Null
New-Item -ItemType File -Path $StopPath -Force | Out-Null
$ControllerDeadline = (Get-Date).AddSeconds(15)
while ((Test-ActiveLock -Path $ControllerLock) -and ((Get-Date) -lt $ControllerDeadline)) {
    Start-Sleep -Milliseconds 500
}
$Task = Get-ScheduledTask -TaskName $TaskName
if (
    $Task.State -ne "Disabled" -or
    (Test-ActiveLock -Path $WorkerLock) -or
    (Test-ActiveLock -Path $ControllerLock)
) {
    throw "Контроль отключения не пройден."
}

Write-Host "OLD NODE DISABLED: computer=$env:COMPUTERNAME; task=Disabled; worker=none; STOP=present" -ForegroundColor Green
Write-Host "Теперь можно активировать ровно один новый узел."
