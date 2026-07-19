[CmdletBinding()]
param(
    [string]$TaskName = "Avito CRM Remote Control"
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
$StatePath = Join-Path $ProjectRoot "data\remote-control.json"

Write-Host "Задача: $TaskName"
Write-Host "Состояние Task Scheduler: $($Task.State)"
Write-Host "Последний запуск: $($Info.LastRunTime)"
Write-Host "Последний код: $($Info.LastTaskResult)"
Write-Host "Фоновый процесс: $(if (Test-Path $LockPath) { 'активен' } else { 'не подтверждён' })"
Write-Host "Незавершённая команда: $(if (Test-Path $StatePath) { 'есть' } else { 'нет' })"
Write-Host "Последнюю связь с компьютером смотрите в листе 'Управление' Google-таблицы."
