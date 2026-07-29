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

function Test-ActiveWorkerLock {
    if (-not (Test-Path -LiteralPath $WorkerLock)) {
        return $false
    }
    try {
        $LockPayload = Get-Content -LiteralPath $WorkerLock -Raw -Encoding UTF8 |
            ConvertFrom-Json
        $LockProcessId = [int]$LockPayload.pid
        if ($LockProcessId -le 0) {
            throw "Некорректный PID в worker.lock"
        }
        [System.Diagnostics.Process]::GetProcessById($LockProcessId) | Out-Null
        return $true
    }
    catch {
        Write-Host "Удаляем устаревший worker.lock..."
        Remove-Item -LiteralPath $WorkerLock -Force -ErrorAction SilentlyContinue
        return (Test-Path -LiteralPath $WorkerLock)
    }
}

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    Write-Host "Задача '$TaskName' уже отсутствует."
    exit 0
}

if ($PSCmdlet.ShouldProcess($TaskName, "остановить и удалить задачу удалённого пульта")) {
    if (Test-Path $Python) {
        & $Python -m avito_crm --root $ProjectRoot stop | Out-Null
    }

    $Deadline = (Get-Date).AddSeconds($SafeStopTimeoutSeconds)
    while ((Test-ActiveWorkerLock) -and ((Get-Date) -lt $Deadline)) {
        Write-Host "Ждём безопасного завершения текущей строки..."
        Start-Sleep -Seconds 2
    }
    if (Test-ActiveWorkerLock) {
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
