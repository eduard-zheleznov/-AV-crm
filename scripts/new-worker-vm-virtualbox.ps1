[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = "High")]
param(
    [string]$WindowsIso,
    [ValidatePattern('^[A-Za-z0-9._ -]+$')]
    [string]$VmName = "Avito-CRM-Worker",
    [ValidateRange(4096, 16384)]
    [int]$MemoryMB = 6144,
    [ValidateRange(2, 8)]
    [int]$CpuCount = 2,
    [ValidateRange(64, 512)]
    [int]$DiskGB = 80,
    [ValidatePattern('^[A-Za-z][A-Za-z0-9._-]{1,19}$')]
    [string]$GuestUser = "AvitoWorker",
    [string]$SettingsTransferDir,
    [switch]$InstallVirtualBox
)

$ErrorActionPreference = "Stop"
$PreflightScript = Join-Path $PSScriptRoot "worker-vm-host-preflight.ps1"
$VirtualBoxVersion = "7.2.8"
$VirtualBoxPath = Join-Path $env:ProgramFiles "Oracle\VirtualBox\VBoxManage.exe"
$TaskName = "Avito CRM Worker VM"

function Test-Administrator {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Invoke-VBox {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    & $script:VirtualBoxPath @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "VBoxManage завершился с кодом $LASTEXITCODE: $($Arguments -join ' ')"
    }
}

if (-not (Test-Path -LiteralPath $PreflightScript -PathType Leaf)) {
    throw "Не найден preflight: $PreflightScript"
}

$CurrentIdentity = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$InteractiveIdentity = [string](Get-CimInstance Win32_ComputerSystem).UserName
if (-not [string]::Equals($CurrentIdentity, $InteractiveIdentity, [StringComparison]::OrdinalIgnoreCase)) {
    throw (
        "PowerShell запущен от $CurrentIdentity, а в Windows вошёл $InteractiveIdentity. " +
        "Откройте PowerShell от имени администратора под тем же пользователем."
    )
}
$Preflight = & $PreflightScript -GuestMemoryMB $MemoryMB -GuestDiskGB $DiskGB -PassThru -Quiet
if ($Preflight.Status -eq "BLOCKED") {
    & $PreflightScript -GuestMemoryMB $MemoryMB -GuestDiskGB $DiskGB
    throw "Компьютер не прошёл обязательные требования. VM не создавалась."
}

if ([string]::IsNullOrWhiteSpace($WindowsIso)) {
    Add-Type -AssemblyName System.Windows.Forms
    $Dialog = New-Object System.Windows.Forms.OpenFileDialog
    $Dialog.Title = "Выберите официальный ISO Windows 11"
    $Dialog.Filter = "Windows ISO (*.iso)|*.iso"
    if ($Dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
        throw "ISO не выбран. Никаких изменений не выполнено."
    }
    $WindowsIso = $Dialog.FileName
}
$WindowsIso = (Resolve-Path -LiteralPath $WindowsIso).Path
if ([IO.Path]::GetExtension($WindowsIso) -ne ".iso") {
    throw "Нужен официальный файл Windows 11 с расширением .iso."
}
if ((Get-Item -LiteralPath $WindowsIso).Length -lt 4GB) {
    throw "ISO меньше 4 ГБ и не похож на официальный образ Windows 11."
}

if (-not [string]::IsNullOrWhiteSpace($SettingsTransferDir)) {
    $SettingsTransferDir = (Resolve-Path -LiteralPath $SettingsTransferDir).Path
    foreach ($RequiredName in @("vds.env", "google-service-account.json", "transfer-manifest.json")) {
        if (-not (Test-Path -LiteralPath (Join-Path $SettingsTransferDir $RequiredName) -PathType Leaf)) {
            throw "В папке переноса нет $RequiredName"
        }
    }
}

if (-not (Test-Path -LiteralPath $VirtualBoxPath -PathType Leaf)) {
    if (-not $InstallVirtualBox) {
        throw "VirtualBox не найден. Повторите с -InstallVirtualBox после ознакомления с лицензией Oracle."
    }
    if (-not (Test-Administrator)) {
        throw "Для установки VirtualBox откройте PowerShell от имени администратора."
    }
    $Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
    if (-not $Winget) {
        throw "winget не найден. Установите App Installer из Microsoft Store и повторите."
    }
    if (-not $PSCmdlet.ShouldProcess("Oracle VirtualBox $VirtualBoxVersion", "Установить через winget")) {
        return
    }
    & $Winget.Source install --id Oracle.VirtualBox --version $VirtualBoxVersion --exact --source winget --silent --disable-interactivity --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget не смог установить VirtualBox; код $LASTEXITCODE."
    }
    if (-not (Test-Path -LiteralPath $VirtualBoxPath -PathType Leaf)) {
        throw "VirtualBox установлен, но VBoxManage пока недоступен. Перезагрузите Windows и повторите без -InstallVirtualBox."
    }
}

$ExistingInfo = & $VirtualBoxPath showvminfo $VmName --machinereadable 2>$null
if ($LASTEXITCODE -eq 0) {
    throw "VM '$VmName' уже существует. Скрипт не изменяет и не удаляет существующие VM."
}

$VmBase = Join-Path $env:LOCALAPPDATA "AvitoCrmVM\VirtualBox"
$VmFolder = Join-Path $VmBase $VmName
$DiskPath = Join-Path $VmFolder "$VmName.vdi"
if (Test-Path -LiteralPath $VmFolder) {
    throw "Каталог уже существует: $VmFolder. VM не создавалась."
}

$Credential = Get-Credential -UserName $GuestUser -Message "Пароль отдельной Windows VM (не пароль основной Windows)"
if ($Credential.UserName -ne $GuestUser) {
    throw "Имя гостевого пользователя должно остаться '$GuestUser'."
}
$PasswordPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Credential.Password)
$PlainPassword = $null
$PasswordFile = Join-Path ([IO.Path]::GetTempPath()) ("avito-vm-password-" + [guid]::NewGuid().ToString("N") + ".txt")
$VmRegistered = $false

try {
    $PlainPassword = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($PasswordPointer)
    if ($PlainPassword.Length -lt 12) {
        throw "Пароль отдельной Windows VM должен содержать не менее 12 символов."
    }
    [IO.File]::WriteAllText($PasswordFile, $PlainPassword + [Environment]::NewLine, (New-Object Text.UTF8Encoding($false)))
    & icacls.exe $PasswordFile /inheritance:r /grant:r "${CurrentIdentity}:(R)" "*S-1-5-32-544:(R)" | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось защитить временный файл пароля VM."
    }

    if (-not $PSCmdlet.ShouldProcess($VmName, "Создать изолированную Windows 11 VM")) {
        return
    }

    New-Item -ItemType Directory -Path $VmBase -Force | Out-Null
    Invoke-VBox @("createvm", "--name", $VmName, "--ostype", "Windows11_64", "--basefolder", $VmBase, "--register")
    $VmRegistered = $true
    Invoke-VBox @(
        "modifyvm", $VmName,
        "--memory", [string]$MemoryMB,
        "--cpus", [string]$CpuCount,
        "--vram", "128",
        "--graphicscontroller", "vboxsvga",
        "--firmware", "efi",
        "--tpm-type", "2.0",
        "--ioapic", "on",
        "--nested-paging", "on",
        "--clipboard-mode", "disabled",
        "--clipboard-file-transfers", "disabled",
        "--drag-and-drop", "disabled",
        "--audio-enabled", "off",
        "--usb-ohci", "off",
        "--usb-ehci", "off",
        "--usb-xhci", "off",
        "--nic1", "nat",
        "--cable-connected1", "on",
        "--boot1", "dvd",
        "--boot2", "disk",
        "--boot3", "none",
        "--boot4", "none",
        "--default-frontend", "headless"
    )
    Invoke-VBox @("createmedium", "disk", "--filename", $DiskPath, "--size", [string]($DiskGB * 1024), "--format", "VDI", "--variant", "Standard")
    Invoke-VBox @("storagectl", $VmName, "--name", "SATA", "--add", "sata", "--controller", "IntelAhci", "--hostiocache", "on")
    Invoke-VBox @("storageattach", $VmName, "--storagectl", "SATA", "--port", "0", "--device", "0", "--type", "hdd", "--medium", $DiskPath)
    if ($SettingsTransferDir) {
        Invoke-VBox @(
            "sharedfolder", "add", $VmName,
            "--name", "AvitoCrmTransfer",
            "--hostpath", $SettingsTransferDir,
            "--readonly",
            "--automount"
        )
    }

    $IsoHash = (Get-FileHash -LiteralPath $WindowsIso -Algorithm SHA256).Hash
    Write-Host "ISO SHA256: $IsoHash" -ForegroundColor Cyan
    Invoke-VBox @("unattended", "detect", "--iso", $WindowsIso, "--machine-readable")
    Invoke-VBox @(
        "unattended", "install", $VmName,
        "--iso", $WindowsIso,
        "--user", $GuestUser,
        "--user-password-file", $PasswordFile,
        "--full-user-name", "Avito CRM Worker",
        "--install-additions",
        "--hostname", "avito-worker.local",
        "--start-vm", "gui"
    )

    $StateDir = Join-Path $env:ProgramData "AvitoCrmVM"
    New-Item -ItemType Directory -Path $StateDir -Force | Out-Null
    $StartScript = Join-Path $StateDir "start-worker-vm.ps1"
    $StartScriptText = @"
`$ErrorActionPreference = "Stop"
`$VBox = '$($VirtualBoxPath.Replace("'", "''"))'
`$Name = '$($VmName.Replace("'", "''"))'
`$Info = & `$VBox showvminfo `$Name --machinereadable 2>`$null
if (`$LASTEXITCODE -ne 0) { exit 2 }
if (`$Info -match 'VMState="running"') { exit 0 }
& `$VBox startvm `$Name --type headless | Out-Null
exit `$LASTEXITCODE
"@
    [IO.File]::WriteAllText($StartScript, $StartScriptText, (New-Object Text.UTF8Encoding($true)))
    $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$StartScript`""
    $Trigger = New-ScheduledTaskTrigger -AtLogOn -User $CurrentIdentity
    $Principal = New-ScheduledTaskPrincipal -UserId $CurrentIdentity -LogonType Interactive -RunLevel Limited
    $Settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Description "Start isolated Avito CRM Worker VM" -Force | Out-Null

    Write-Host "VM создана и установка Windows запущена." -ForegroundColor Green
    Write-Host "Имя: $VmName; память: $MemoryMB МБ; CPU: $CpuCount; диск: $DiskGB ГБ; сеть: NAT."
    Write-Host "Общий буфер, перенос файлов, USB и звук отключены."
    if ($SettingsTransferDir) {
        Write-Host "Защищённая папка настроек подключена к VM только для чтения."
    }
    Write-Host "После появления рабочего стола Windows выполните внутри VM scripts\install-worker-node.ps1."
}
catch {
    if ($VmRegistered) {
        Write-Warning "Создание прервано. VM сохранена для диагностики и автоматически не удалялась."
        Write-Warning "Для безопасного отключения без удаления файлов: VBoxManage unregistervm `"$VmName`""
    }
    throw
}
finally {
    if ($PasswordPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($PasswordPointer)
    }
    $PlainPassword = $null
    Remove-Item -LiteralPath $PasswordFile -Force -ErrorAction SilentlyContinue
}
