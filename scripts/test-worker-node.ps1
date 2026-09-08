[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^https://(www\.)?avito\.ru/')]
    [string]$Url,
    [string]$TaskName = "Avito CRM Remote Control"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$StopPath = Join-Path $ProjectRoot "data\STOP"
$WorkerLock = Join-Path $ProjectRoot "data\worker.lock"
$StatePath = Join-Path $ProjectRoot "data\remote-control.json"

$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction Stop
if ($Task.State -ne "Disabled") {
    throw "Для offline smoke пульт должен быть Disabled."
}
if ((Test-Path -LiteralPath $WorkerLock) -or (Test-Path -LiteralPath $StatePath)) {
    throw "Найден worker.lock или незавершённая команда. Тест не запущен."
}

Remove-Item -LiteralPath $StopPath -Force -ErrorAction SilentlyContinue
try {
    & $Python -m avito_crm --root $ProjectRoot avito-extension-test $Url --max-clicks 1
    if ($LASTEXITCODE -ne 0) {
        throw "avito-extension-test завершился с кодом $LASTEXITCODE"
    }
}
finally {
    New-Item -ItemType Directory -Path (Split-Path -Parent $StopPath) -Force | Out-Null
    New-Item -ItemType File -Path $StopPath -Force | Out-Null
}

Write-Host "OFFLINE SMOKE OK: CRM=unchanged; queue=unchanged; STOP=present; task=Disabled" -ForegroundColor Green
