[CmdletBinding()]
param(
    [string]$TaskName = "Avito CRM Remote Control",
    [switch]$NoStart,
    [ValidateRange(30, 1800)]
    [int]$SafeStopTimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Pythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$WorkerLock = Join-Path $ProjectRoot "data\worker.lock"

if (-not (Test-Path $Python) -or -not (Test-Path $Pythonw)) {
    throw "Python-окружение не найдено. Сначала выполните .\scripts\install.ps1"
}
Set-Location $ProjectRoot
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    Write-Host "Обновляем установленный пульт без обрыва текущей записи..."
    & $Python -m avito_crm --root $ProjectRoot stop | Out-Null

    $Deadline = (Get-Date).AddSeconds($SafeStopTimeoutSeconds)
    while ((Test-Path $WorkerLock) -and ((Get-Date) -lt $Deadline)) {
        Write-Host "Ждём безопасного завершения текущей строки..."
        Start-Sleep -Seconds 2
    }
    if (Test-Path $WorkerLock) {
        throw (
            "Рабочий процесс не завершился за $SafeStopTimeoutSeconds сек. " +
            "Пульт оставлен работающим, чтобы не оборвать запись в CRM."
        )
    }

    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

Write-Host "Проверяем Google и создаём листы удалённого пульта..."
& $Python -m avito_crm --root $ProjectRoot remote-control --setup-only
if ($LASTEXITCODE -ne 0) {
    throw "Листы пульта не подготовлены. Исправьте ошибку Google выше и повторите."
}

$CurrentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$Arguments = "-m avito_crm --root `"$ProjectRoot`" remote-control --allow-live-crm"
$Action = New-ScheduledTaskAction `
    -Execute $Pythonw `
    -Argument $Arguments `
    -WorkingDirectory $ProjectRoot
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentUser
$Principal = New-ScheduledTaskPrincipal `
    -UserId $CurrentUser `
    -LogonType Interactive `
    -RunLevel Limited
$TaskSettings = New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries

Register-ScheduledTask `
    -TaskName $TaskName `
    -Action $Action `
    -Trigger $Trigger `
    -Principal $Principal `
    -Settings $TaskSettings `
    -Description "Google Sheets remote control for Avito to LPTracker CRM" `
    -Force | Out-Null

if (-not $NoStart) {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 2
}

$Task = Get-ScheduledTask -TaskName $TaskName
$Info = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host ""
Write-Host "Удалённый пульт установлен." -ForegroundColor Green
Write-Host "Задача: $TaskName"
Write-Host "Состояние: $($Task.State)"
Write-Host "Последний код: $($Info.LastTaskResult)"
Write-Host "Пульт будет запускаться при входе $CurrentUser в Windows."
Write-Host "RDP можно отключать, но нельзя выходить из учётной записи Windows."
