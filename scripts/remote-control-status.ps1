[CmdletBinding()]
param(
    [string]$TaskName = "Avito CRM Remote Control",
    [switch]$ShowRecentLog
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

if (-not $Task) {
    Write-Host "Удалённый пульт не установлен." -ForegroundColor Yellow
    exit 1
}

$Info = Get-ScheduledTaskInfo -TaskName $TaskName
$LockPath = Join-Path $ProjectRoot "data\remote-control.lock"
$WorkerLockPath = Join-Path $ProjectRoot "data\worker.lock"
$StatePath = Join-Path $ProjectRoot "data\remote-control.json"
$StopPath = Join-Path $ProjectRoot "data\STOP"
$LogPath = Join-Path $ProjectRoot "logs\avito-crm.log"

function Get-LockDescription {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        return "нет"
    }
    try {
        $Payload = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
        $OwnerPid = [int]$Payload.pid
        $Process = Get-Process -Id $OwnerPid -ErrorAction Stop
        return "активен: PID=$OwnerPid, процесс=$($Process.ProcessName), с $($Payload.started_at)"
    }
    catch {
        return "файл есть, но активный владелец не подтверждён"
    }
}

Write-Host "Задача: $TaskName"
Write-Host "Состояние Task Scheduler: $($Task.State)"
Write-Host "Последний запуск: $($Info.LastRunTime)"
Write-Host "Последний код: $($Info.LastTaskResult)"
Write-Host "Контроллер: $(Get-LockDescription -Path $LockPath)"
Write-Host "Рабочий процесс: $(Get-LockDescription -Path $WorkerLockPath)"
Write-Host "Флаг безопасной остановки: $(if (Test-Path $StopPath) { 'есть' } else { 'нет' })"
if (Test-Path -LiteralPath $StatePath) {
    try {
        $State = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        Write-Host (
            "Незавершённая команда: ID=$($State.command_id), " +
            "этап=$($State.phase), цель=$($State.target), " +
            "остановка=$($State.stop_requested)"
        )
    }
    catch {
        Write-Host "Незавершённая команда: файл состояния повреждён или занят чтением."
    }
}
else {
    Write-Host "Незавершённая команда: нет"
}
Write-Host "Последнюю связь с компьютером смотрите в листе 'Управление' Google-таблицы."

if ($ShowRecentLog) {
    Write-Host ""
    Write-Host "Последние безопасные диагностические сообщения:"
    if (Test-Path -LiteralPath $LogPath) {
        Get-Content -LiteralPath $LogPath -Tail 300 -Encoding UTF8 |
            Select-String -Pattern (
                "Удалённый пульт|Команда [^:]+:|CRM готова|Подготовка CRM|" +
                "Синхронизация CRM|Запускаем Chromium|Начинаем круг|" +
                "Рабочий процесс пульта завершился|Ошибка цикла удалённого пульта"
            ) |
            Select-Object -Last 40
    }
    else {
        Write-Host "Лог пока не создан."
    }
}
