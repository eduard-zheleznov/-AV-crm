[CmdletBinding()]
param(
    [string]$ShortcutName = "Avito в CRM"
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $ProjectRoot ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) {
    throw "Не найден $Pythonw. Сначала выполните .\scripts\install.ps1"
}

$Desktop = [Environment]::GetFolderPath("Desktop")
if (-not $Desktop) {
    throw "Не удалось определить папку рабочего стола"
}

$ShortcutPath = Join-Path $Desktop "$ShortcutName.lnk"
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $Pythonw
$Shortcut.Arguments = "-m avito_crm.gui --root `"$ProjectRoot`""
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Description = "Google Sheets → Avito → LPTracker CRM"
$Shortcut.IconLocation = "$Pythonw,0"
$Shortcut.Save()

Write-Host "Ярлык создан: $ShortcutPath" -ForegroundColor Green
