[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$SourceRoot = "D:\avito-crm",
    [string]$WindowsIso,
    [ValidatePattern('^[A-Za-z0-9._ -]+$')]
    [string]$VmName = "Avito-CRM-Worker",
    [ValidateRange(4096, 16384)]
    [int]$MemoryMB = 6144,
    [ValidateRange(2, 8)]
    [int]$CpuCount = 2,
    [ValidateRange(64, 512)]
    [int]$DiskGB = 80,
    [switch]$InstallVirtualBox,
    [switch]$AcceptWarnings
)

$ErrorActionPreference = "Stop"
$PreflightScript = Join-Path $PSScriptRoot "worker-vm-host-preflight.ps1"
$ExportScript = Join-Path $PSScriptRoot "export-worker-node-settings.ps1"
$CreateScript = Join-Path $PSScriptRoot "new-worker-vm-virtualbox.ps1"

$Preflight = & $PreflightScript -GuestMemoryMB $MemoryMB -GuestDiskGB $DiskGB -PassThru
if ($Preflight.Status -eq "BLOCKED") {
    throw "Компьютер не прошёл preflight. Ничего не установлено."
}
if ($Preflight.Status -eq "READY_WITH_WARNINGS" -and -not $AcceptWarnings) {
    throw "Есть предупреждения. Снача оцените их; затем повторите с -AcceptWarnings."
}
if (-not $PSCmdlet.ShouldProcess($env:COMPUTERNAME, "экспортировать настройки и создать изолированную VM")) {
    return
}

$TransferDir = & $ExportScript -SourceRoot $SourceRoot -PassThru
if (-not $TransferDir -or -not (Test-Path -LiteralPath $TransferDir -PathType Container)) {
    throw "Папка защищённого переноса не создана."
}

try {
    & $CreateScript `
        -WindowsIso $WindowsIso `
        -VmName $VmName `
        -MemoryMB $MemoryMB `
        -CpuCount $CpuCount `
        -DiskGB $DiskGB `
        -SettingsTransferDir $TransferDir `
        -InstallVirtualBox:$InstallVirtualBox `
        -Confirm:$false
}
catch {
    Write-Warning "Создание VM не завершено. Папка секретов сохранена: $TransferDir"
    throw
}

Write-Host "HOST PREPARATION OK: VM=$VmName; transfer=$TransferDir" -ForegroundColor Green
Write-Host "Дождитесь рабочего стола Windows в VM. После этого нужна одна bootstrap-команда внутри VM."
