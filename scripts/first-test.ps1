[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("google", "xlsx", "csv")]
    [string]$Source,
    [string]$File,
    [string]$Sheet
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Сначала выполните .\scripts\install.ps1" }
if (($Source -eq "xlsx" -or $Source -eq "csv") -and -not $File) {
    throw "Для Source=$Source требуется -File"
}

function Get-SourceArguments {
    $Result = @("--source", $Source)
    if ($File) { $Result += @("--file", $File) }
    if ($Sheet) { $Result += @("--sheet", $Sheet) }
    return $Result
}

function Invoke-App {
    param([string[]]$Arguments)
    & $Python -m avito_crm @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "Шаг завершился с ошибкой (exit code $LASTEXITCODE). Исправьте её до продолжения."
    }
}

Set-Location $ProjectRoot
$SourceArguments = Get-SourceArguments

Write-Host "=== ШАГ 1/5: OCR, Chromium и доступ к очереди ===" -ForegroundColor Cyan
Invoke-App -Arguments (@("doctor") + $SourceArguments)

Write-Host ""
Write-Host "=== ШАГ 2/5: доступные проекты LPTracker ===" -ForegroundColor Cyan
Invoke-App -Arguments @("crm-projects")
Write-Host "Проверьте LPTRACKER_PROJECT_ID в .env."
$null = Read-Host "После сохранения .env нажмите Enter"
Invoke-App -Arguments @("crm-check")

Write-Host ""
Write-Host "=== ШАГ 3/5: открываем и распознаём РОВНО ОДИН номер ===" -ForegroundColor Cyan
Invoke-App -Arguments (
    @("capture") + $SourceArguments + @("--limit", "1", "--interactive-check")
)
Write-Host "Номер подтверждён и записан в колонку 'Телефон'; CRM ещё не изменялась."

Write-Host ""
Write-Host "=== ШАГ 4/5: создаём РОВНО ОДИН тестовый лид ===" -ForegroundColor Yellow
Write-Host "Будет использован уже распознанный номер; Avito повторно не открывается."
$LiveConfirmation = Read-Host "Для подтверждения введите СОЗДАТЬ 1 ЛИД"
if ($LiveConfirmation.Trim().ToUpperInvariant() -ne "СОЗДАТЬ 1 ЛИД") {
    Write-Host "Остановка без записи в CRM."
    exit 0
}
Invoke-App -Arguments (
    @("sync-crm") + $SourceArguments + @("--limit", "1", "--live", "--require-goal")
)

Write-Host ""
Write-Host "=== ШАГ 5/5: итог ===" -ForegroundColor Green
Invoke-App -Arguments @("status")
Write-Host "Откройте созданный лид в LPTracker и проверьте телефон и поле 'Тег+ для новых с Ав и Ян'."
