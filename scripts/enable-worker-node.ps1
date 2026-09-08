[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [switch]$ConfirmPreviousNodeStopped,
    [string]$TaskName = "Avito CRM Remote Control"
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

if (-not $ConfirmPreviousNodeStopped) {
    throw "Активация требует явного -ConfirmPreviousNodeStopped."
}
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if ($Task.State -ne "Disabled") {
    throw "Новый пульт до активации должен иметь статус Disabled."
}
if ((Test-ActiveLock -Path $WorkerLock) -or (Test-Path -LiteralPath $StatePath)) {
    throw "Новый узел не пассивен: worker или незавершённая команда обнаружены."
}

$ProbeCode = @'
import json
import sys
from pathlib import Path
from avito_crm.config import Settings
from avito_crm.remote_control import GoogleControlPanel
s = Settings.load(Path(sys.argv[1]))
p = GoogleControlPanel.connect(s)
p.control = p.spreadsheet.worksheet(s.google_control_worksheet)
c = p.read_command()
print(json.dumps({"start": c.start, "stop": c.stop}))
'@
$PanelJson = & $Python -c $ProbeCode $ProjectRoot
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось прочитать B4/B5. Узел остался выключен."
}
$Panel = $PanelJson | ConvertFrom-Json
if ([bool]$Panel.start -or [bool]$Panel.stop) {
    throw "В Google-пульте B4 или B5 включены. Активация отклонена."
}

try {
    & $Python -m avito_crm --root $ProjectRoot remote-control --setup-only
    if ($LASTEXITCODE -ne 0) { throw "Google setup завершился с кодом $LASTEXITCODE" }
    Remove-Item -LiteralPath $StopPath -Force -ErrorAction SilentlyContinue
    Enable-ScheduledTask -TaskName $TaskName | Out-Null
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    $Task = Get-ScheduledTask -TaskName $TaskName
    if ($Task.State -ne "Running" -or -not (Test-ActiveLock -Path $ControllerLock)) {
        throw "задача и активный lock контроллера не подтверждены"
    }
}
catch {
    New-Item -ItemType File -Path $StopPath -Force | Out-Null
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Disable-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue | Out-Null
    throw "Активация откачена: $($_.Exception.Message)"
}

Write-Host "NEW NODE ACTIVE: computer=$env:COMPUTERNAME; task=Running; B4=false; B5=false; STOP=absent" -ForegroundColor Green
Write-Host "Перед production выполните один avito-extension-test без CRM, затем canary 1."
