[CmdletBinding(SupportsShouldProcess)]
param(
    [string]$TaskName = "Avito CRM Remote Control",
    [ValidateRange(30, 900)]
    [int]$SafeStopTimeoutSeconds = 300
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$WorkerLock = Join-Path $ProjectRoot "data\worker.lock"

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Write-Host "Задача '$TaskName' уже отсутствует."
    exit 0
}

if ($PSCmdlet.ShouldProcess($TaskName, "остановить и удалить задачу удалённого пульта")) {
    if (Test-Path $Python) {
        & $Python -m avito_crm --root $ProjectRoot stop | Out-Null
    }

    $Deadline = (Get-Date).AddSeconds($SafeStopTimeoutSeconds)
    while ((Test-Path $WorkerLock) -and ((Get-Date) -lt $Deadline)) {
        Write-Host "Ждём безопасного завершения текущей строки..."
        Start-Sleep -Seconds 2
    }
    if (Test-Path $WorkerLock) {
        throw (
            "Рабочий процесс не завершился за $SafeStopTimeoutSeconds сек. " +
            "Задача оставлена включённой, чтобы не оборвать запись в CRM. " +
            "Повторите удаление позже."
        )
    }

    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Удалённый пульт отключён. Листы Google и история сохранены." -ForegroundColor Green
}
