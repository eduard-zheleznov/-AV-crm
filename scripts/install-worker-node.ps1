[CmdletBinding()]
param(
    [string]$InstallRoot = "C:\avito-crm",
    [string]$RepositoryUri = "https://github.com/eduard-zheleznov/-AV-crm.git",
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ReleaseCommit,
    [switch]$AllowPhysical
)

$ErrorActionPreference = "Stop"
$ExpectedAppVersion = "1.12.43.1"
$ExpectedExtensionVersion = "1.0.19"

function Test-Administrator {
    return ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator
    )
}

function Refresh-ProcessPath {
    $MachinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$MachinePath;$UserPath"
}

function Install-WingetPackage {
    param(
        [Parameter(Mandatory = $true)][string]$Id,
        [Parameter(Mandatory = $true)][string]$Label
    )

    & winget.exe list --id $Id --exact --accept-source-agreements | Out-Null
    if ($LASTEXITCODE -eq 0) {
        Write-Host "$Label уже установлен."
        return
    }
    Write-Host "Устанавливаем $Label..." -ForegroundColor Cyan
    & winget.exe install --id $Id --exact --source winget --silent --disable-interactivity --accept-package-agreements --accept-source-agreements
    if ($LASTEXITCODE -ne 0) {
        throw "winget не смог установить $Label ($Id); код $LASTEXITCODE."
    }
    Refresh-ProcessPath
}

if ($env:OS -ne "Windows_NT") {
    throw "Установщик рассчитан только на Windows."
}
if (-not (Test-Administrator)) {
    throw "Откройте PowerShell внутри VM от имени администратора."
}

$Computer = Get-CimInstance Win32_ComputerSystem
$VmIdentity = "$($Computer.Manufacturer) $($Computer.Model)"
$KnownVm = $VmIdentity -match "VirtualBox|VMware|Virtual Machine|KVM|QEMU|Parallels|Xen"
if (-not $KnownVm -and -not $AllowPhysical) {
    throw (
        "Компьютер не распознан как VM ($VmIdentity). " +
        "Скрипт не менял физическую Windows."
    )
}

$InstallRootFull = [IO.Path]::GetFullPath($InstallRoot)
if ($InstallRootFull -notmatch '^[A-Za-z]:\\[^\\]+') {
    throw "Небезопасный каталог установки: $InstallRootFull"
}

$Winget = Get-Command winget.exe -ErrorAction SilentlyContinue
if (-not $Winget) {
    throw "winget не найден. Обновите App Installer в Microsoft Store и повторите."
}

Install-WingetPackage -Id "Git.Git" -Label "Git"
Install-WingetPackage -Id "Python.Python.3.13" -Label "Python 3.13"
Install-WingetPackage -Id "Google.Chrome" -Label "Google Chrome Stable"
Install-WingetPackage -Id "UB-Mannheim.TesseractOCR" -Label "Tesseract OCR"
Refresh-ProcessPath

$Git = Get-Command git.exe -ErrorAction SilentlyContinue
if (-not $Git) {
    throw "Git установлен, но не появился в PATH. Перезапустите PowerShell и повторите ту же команду."
}

if (Test-Path -LiteralPath $InstallRootFull) {
    if (-not (Test-Path -LiteralPath (Join-Path $InstallRootFull ".git") -PathType Container)) {
        throw "Каталог $InstallRootFull уже есть и не является Git-репозиторием."
    }
    $TrackedChanges = & $Git.Source -C $InstallRootFull status --porcelain --untracked-files=no
    if ($LASTEXITCODE -ne 0 -or $TrackedChanges) {
        throw "В $InstallRootFull есть несохранённый код. Установка остановлена без изменений."
    }
    $RemoteUrl = (& $Git.Source -C $InstallRootFull remote get-url origin).Trim()
    if ($LASTEXITCODE -ne 0 -or $RemoteUrl.TrimEnd("/") -ne $RepositoryUri.TrimEnd("/")) {
        throw "Неверный origin в $InstallRootFull."
    }
}
else {
    & $Git.Source clone --no-tags $RepositoryUri $InstallRootFull
    if ($LASTEXITCODE -ne 0) {
        throw "Git clone завершился с кодом $LASTEXITCODE."
    }
}

& $Git.Source -C $InstallRootFull fetch --no-tags origin $ReleaseCommit
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось загрузить release commit $ReleaseCommit."
}
& $Git.Source -C $InstallRootFull checkout --detach $ReleaseCommit
if ($LASTEXITCODE -ne 0) {
    throw "Не удалось перейти на release commit $ReleaseCommit."
}
$ActualCommit = (& $Git.Source -C $InstallRootFull rev-parse HEAD).Trim()
if ($ActualCommit -ne $ReleaseCommit) {
    throw "Проверка commit не пройдена: $ActualCommit."
}

$StopPath = Join-Path $InstallRootFull "data\STOP"
New-Item -ItemType Directory -Path (Split-Path -Parent $StopPath) -Force | Out-Null
New-Item -ItemType File -Path $StopPath -Force | Out-Null

$InstallScript = Join-Path $InstallRootFull "scripts\install.ps1"
& $InstallScript -SkipPlaywright -SkipRemoteControlRefresh
if ($LASTEXITCODE -ne 0) {
    throw "Установка Avito CRM завершилась с кодом $LASTEXITCODE."
}

$Python = Join-Path $InstallRootFull ".venv\Scripts\python.exe"
$AppVersion = (& $Python -c "import avito_crm; print(avito_crm.__version__)").Trim()
$ManifestPath = Join-Path $InstallRootFull "chrome-extension\manifest.json"
$ExtensionVersion = (Get-Content -LiteralPath $ManifestPath -Raw -Encoding UTF8 | ConvertFrom-Json).version
if ($AppVersion -ne $ExpectedAppVersion -or $ExtensionVersion -ne $ExpectedExtensionVersion) {
    throw "Несовместимая пара: app=$AppVersion, extension=$ExtensionVersion."
}

& (Join-Path $InstallRootFull "scripts\install-chrome-extension.ps1")
if ($LASTEXITCODE -ne 0) {
    throw "Подготовка Chrome-расширения завершилась с ошибкой."
}

# The guest must keep its interactive desktop alive for Chrome rendering.
foreach ($PowerArguments in @(
    @("/change", "standby-timeout-ac", "0"),
    @("/change", "hibernate-timeout-ac", "0"),
    @("/change", "monitor-timeout-ac", "0")
)) {
    & powercfg.exe @PowerArguments | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось отключить сон Windows VM: powercfg $($PowerArguments -join ' ')"
    }
}

$InstallMarker = [ordered]@{
    installed_at = (Get-Date).ToUniversalTime().ToString("o")
    commit = $ActualCommit
    app_version = $AppVersion
    extension_version = $ExtensionVersion
}
$InstallMarker | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $InstallRootFull "data\worker-node-install.json") -Encoding UTF8

Write-Host ""
Write-Host "WORKER NODE INSTALLED: app=$AppVersion; extension=$ExtensionVersion; commit=$ActualCommit" -ForegroundColor Green
Write-Host "STOP включён; Google-пульт не устанавливался; CRM и очередь не затронуты."
Write-Host "В Chrome загрузите папку $InstallRootFull\chrome-extension и включите работу в инкогнито."
Write-Host "После этого запустите complete-worker-node.ps1 с папкой защищённого переноса."
