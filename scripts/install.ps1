[CmdletBinding()]
param(
    [switch]$Dev
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
Set-Location $ProjectRoot

$Python = $null
foreach ($Candidate in @("py", "python")) {
    if (Get-Command $Candidate -ErrorAction SilentlyContinue) {
        $Python = $Candidate
        break
    }
}
if (-not $Python) {
    throw "Python 3.11+ не найден. Установите Python с https://www.python.org/downloads/windows/"
}

$VersionText = & $Python --version 2>&1
if ($LASTEXITCODE -ne 0 -or $VersionText -notmatch "Python 3\.(1[1-9]|[2-9][0-9])") {
    throw "Нужен Python 3.11 или новее; найдено: $VersionText"
}

if (-not (Test-Path ".venv")) {
    & $Python -m venv .venv
}
$VenvPython = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
& $VenvPython -m pip install --upgrade pip
if ($Dev) {
    & $VenvPython -m pip install -e ".[dev]"
} else {
    & $VenvPython -m pip install -e "."
}
& $VenvPython -m playwright install chromium
& $VenvPython -m avito_crm init

$TesseractCandidates = @(
    "$env:ProgramFiles\Tesseract-OCR\tesseract.exe",
    "${env:ProgramFiles(x86)}\Tesseract-OCR\tesseract.exe"
)
$Tesseract = $TesseractCandidates | Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
if (-not $Tesseract -and -not (Get-Command tesseract -ErrorAction SilentlyContinue)) {
    Write-Warning "Tesseract OCR не найден. Установите Windows-сборку Tesseract и укажите TESSERACT_CMD в .env."
}

Write-Host ""
Write-Host "Установка завершена. Следующие шаги:"
Write-Host "1. Заполните .env (секреты не коммитить)."
Write-Host "2. Выполните: .\scripts\doctor.ps1"
Write-Host "3. Добавьте 1 ссылку в queue-template.xlsx, закройте Excel и запустите:"
Write-Host "   .\scripts\first-test.ps1 -Source xlsx -File .\queue-template.xlsx -Sheet Лист1"
