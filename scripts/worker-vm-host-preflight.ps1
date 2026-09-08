[CmdletBinding()]
param(
    [ValidateRange(4096, 16384)]
    [int]$GuestMemoryMB = 6144,
    [ValidateRange(64, 512)]
    [int]$GuestDiskGB = 80,
    [switch]$PassThru,
    [switch]$Quiet
)

$ErrorActionPreference = "Stop"

if ($env:OS -ne "Windows_NT") {
    throw "Проверка рассчитана только на Windows."
}

$Os = Get-CimInstance Win32_OperatingSystem
$Computer = Get-CimInstance Win32_ComputerSystem
$Processors = @(Get-CimInstance Win32_Processor)
$SystemDriveName = $Os.SystemDrive.TrimEnd(":")
$SystemVolume = Get-CimInstance Win32_LogicalDisk -Filter "DeviceID='$($Os.SystemDrive)'"

$TotalMemoryGB = [math]::Round(([double]$Computer.TotalPhysicalMemory / 1GB), 1)
$FreeDiskGB = [math]::Round(([double]$SystemVolume.FreeSpace / 1GB), 1)
$LogicalCpuCount = [int](($Processors | Measure-Object NumberOfLogicalProcessors -Sum).Sum)
$VirtualizationEnabled = [bool]$Computer.HypervisorPresent -or [bool](
    $Processors | Where-Object { $_.VirtualizationFirmwareEnabled }
)
$SlatAvailable = [bool](
    $Processors | Where-Object { $_.SecondLevelAddressTranslationExtensions }
)

$SystemDiskMedia = "Unknown"
try {
    $Partition = Get-Partition -DriveLetter $SystemDriveName -ErrorAction Stop
    $Disk = Get-Disk -Number $Partition.DiskNumber -ErrorAction Stop
    $PhysicalDisk = Get-PhysicalDisk -ErrorAction Stop |
        Where-Object {
            ([string]$_.DeviceId -eq [string]$Disk.Number) -or
            ($_.FriendlyName -eq $Disk.FriendlyName)
        } |
        Select-Object -First 1
    if ($PhysicalDisk -and $PhysicalDisk.MediaType) {
        $SystemDiskMedia = [string]$PhysicalDisk.MediaType
    }
}
catch {
    $SystemDiskMedia = "Unknown"
}

$VirtualBoxPath = Join-Path $env:ProgramFiles "Oracle\VirtualBox\VBoxManage.exe"
$VirtualBoxInstalled = Test-Path -LiteralPath $VirtualBoxPath -PathType Leaf
$WingetAvailable = $null -ne (Get-Command winget.exe -ErrorAction SilentlyContinue)
$IsAdministrator = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator
)

$Blockers = New-Object System.Collections.Generic.List[string]
$Warnings = New-Object System.Collections.Generic.List[string]

if (-not [Environment]::Is64BitOperatingSystem) {
    $Blockers.Add("Нужна 64-битная Windows.")
}
if ($TotalMemoryGB -lt 12) {
    $Blockers.Add("Оперативной памяти $TotalMemoryGB ГБ; для хоста и Chrome-VM нужно минимум 12 ГБ, рекомендуется 16 ГБ.")
}
elseif ($TotalMemoryGB -lt 16) {
    $Warnings.Add("Оперативной памяти $TotalMemoryGB ГБ: VM допустима с 4 ГБ, но параллельная работа может замедляться.")
}
if ($LogicalCpuCount -lt 4) {
    $Blockers.Add("Доступно логических процессоров: $LogicalCpuCount; нужно минимум 4.")
}
if ($FreeDiskGB -lt ($GuestDiskGB + 20)) {
    $Blockers.Add("Свободно $FreeDiskGB ГБ; для диска VM $GuestDiskGB ГБ и запаса нужно минимум $($GuestDiskGB + 20) ГБ.")
}
if (-not $VirtualizationEnabled) {
    $Blockers.Add("Аппаратная виртуализация не обнаружена. Включите Intel VT-x/AMD-V в UEFI/BIOS.")
}
if (-not $SlatAvailable -and -not $Computer.HypervisorPresent) {
    $Warnings.Add("Windows не подтвердила SLAT. VirtualBox должен проверить поддержку при первом запуске.")
}
if ($SystemDiskMedia -eq "HDD") {
    $Warnings.Add("Системный диск — HDD. Chrome и OCR в VM могут заметно замедлять основной компьютер; SSD настоятельно рекомендуется.")
}
elseif ($SystemDiskMedia -eq "Unknown") {
    $Warnings.Add("Тип системного диска определить не удалось. Перед production убедитесь, что используется SSD.")
}
if (-not $WingetAvailable -and -not $VirtualBoxInstalled) {
    $Warnings.Add("winget отсутствует: VirtualBox придётся установить вручную с virtualbox.org.")
}
if (-not $IsAdministrator) {
    $Warnings.Add("PowerShell открыт без прав администратора. Они понадобятся только на шаге установки VirtualBox.")
}

$Status = if ($Blockers.Count -gt 0) {
    "BLOCKED"
}
elseif ($Warnings.Count -gt 0) {
    "READY_WITH_WARNINGS"
}
else {
    "READY"
}

$Result = [pscustomobject][ordered]@{
    Status = $Status
    Computer = $env:COMPUTERNAME
    Windows = $Os.Caption
    WindowsVersion = $Os.Version
    TotalMemoryGB = $TotalMemoryGB
    RequestedGuestMemoryMB = $GuestMemoryMB
    LogicalCpuCount = $LogicalCpuCount
    FreeSystemDiskGB = $FreeDiskGB
    RequestedGuestDiskGB = $GuestDiskGB
    SystemDiskMediaType = $SystemDiskMedia
    VirtualizationEnabled = $VirtualizationEnabled
    SlatAvailable = $SlatAvailable
    HypervisorPresent = [bool]$Computer.HypervisorPresent
    VirtualBoxInstalled = $VirtualBoxInstalled
    WingetAvailable = $WingetAvailable
    Administrator = $IsAdministrator
    Blockers = @($Blockers)
    Warnings = @($Warnings)
}

if (-not $Quiet) {
    Write-Host "Проверка компьютера для Avito CRM Worker VM" -ForegroundColor Cyan
    Write-Host "Статус: $Status" -ForegroundColor $(if ($Status -eq "BLOCKED") { "Red" } elseif ($Status -eq "READY") { "Green" } else { "Yellow" })
    Write-Host "Windows: $($Os.Caption) $($Os.Version)"
    Write-Host "Память: $TotalMemoryGB ГБ; CPU: $LogicalCpuCount логических; свободно: $FreeDiskGB ГБ"
    Write-Host "Системный диск: $SystemDiskMedia; виртуализация: $VirtualizationEnabled; SLAT: $SlatAvailable"
    Write-Host "VirtualBox: $VirtualBoxInstalled; winget: $WingetAvailable; администратор: $IsAdministrator"
    foreach ($Item in $Blockers) { Write-Host "БЛОКЕР: $Item" -ForegroundColor Red }
    foreach ($Item in $Warnings) { Write-Host "ВНИМАНИЕ: $Item" -ForegroundColor Yellow }
}

if ($PassThru) {
    return $Result
}
if ($Status -eq "BLOCKED") { exit 3 }
if ($Status -eq "READY_WITH_WARNINGS") { exit 2 }
exit 0
