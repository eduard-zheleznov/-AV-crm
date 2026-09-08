[CmdletBinding()]
param(
    [string]$TaskName = "Avito CRM Remote Control",
    [switch]$NoStart,
    [switch]$SkipGoogleSetup,
    [switch]$Disabled,
    [ValidateRange(30, 1800)]
    [int]$SafeStopTimeoutSeconds = 600
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$Pythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
$WorkerLock = Join-Path $ProjectRoot "data\worker.lock"

if ($SkipGoogleSetup -and -not $Disabled) {
    throw "-SkipGoogleSetup разрешён только вместе с -Disabled."
}

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

if (-not (Test-Path $Python) -or -not (Test-Path $Pythonw)) {
    throw "Python-окружение не найдено. Сначала выполните .\scripts\install.ps1"
}
Set-Location $ProjectRoot
$Existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($Existing) {
    Write-Host "Обновляем установленный пульт без обрыва текущей записи..."
    & $Python -m avito_crm --root $ProjectRoot stop | Out-Null

    $Deadline = (Get-Date).AddSeconds($SafeStopTimeoutSeconds)
    while ((Test-ActiveWorkerLock) -and ((Get-Date) -lt $Deadline)) {
        Write-Host "Ждём безопасного завершения текущей строки..."
        Start-Sleep -Seconds 2
    }
    if (Test-ActiveWorkerLock) {
        throw (
            "Рабочий процесс не завершился за $SafeStopTimeoutSeconds сек. " +
            "Пульт оставлен работающим, чтобы не оборвать запись в CRM."
        )
    }

    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
}

if (-not $SkipGoogleSetup) {
    Write-Host "Проверяем Google и создаём листы удалённого пульта..."
    & $Python -m avito_crm --root $ProjectRoot remote-control --setup-only
    if ($LASTEXITCODE -ne 0) {
        throw "Листы пульта не подготовлены. Исправьте ошибку Google выше и повторите."
    }
}
else {
    Write-Host "Google не изменяется: задача регистрируется в пассивном режиме."
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

if ($Disabled) {
    Disable-ScheduledTask -TaskName $TaskName | Out-Null
}
elseif (-not $NoStart) {
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
if ($Disabled) {
    Write-Host "Пульт зарегистрирован, но отключён. Команды Google он не читает."
}
else {
    Write-Host "Пульт будет запускаться при входе $CurrentUser в Windows."
    Write-Host "RDP можно отключать, но нельзя выходить из учётной записи Windows."
}
