[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[0-9a-fA-F]{40}$')]
    [string]$ReleaseCommit,
    [switch]$Create,
    [string]$SourceRoot = "D:\avito-crm",
    [string]$WindowsIso,
    [switch]$InstallVirtualBox,
    [switch]$AcceptWarnings
)

$ErrorActionPreference = "Stop"
$RepositoryRaw = "https://raw.githubusercontent.com/eduard-zheleznov/-AV-crm/$ReleaseCommit/scripts"
$ToolkitRoot = Join-Path $env:LOCALAPPDATA ("AvitoCrmVM\toolkit-" + $ReleaseCommit.Substring(0, 12))
$ScriptNames = @(
    "worker-vm-host-preflight.ps1",
    "export-worker-node-settings.ps1",
    "new-worker-vm-virtualbox.ps1",
    "prepare-worker-vm-host.ps1"
)

New-Item -ItemType Directory -Path $ToolkitRoot -Force | Out-Null
foreach ($Name in $ScriptNames) {
    $Target = Join-Path $ToolkitRoot $Name
    Invoke-WebRequest -UseBasicParsing -Uri "$RepositoryRaw/$Name" -OutFile $Target
    if (-not (Test-Path -LiteralPath $Target -PathType Leaf) -or (Get-Item -LiteralPath $Target).Length -lt 100) {
        throw "Не удалось загрузить $Name из exact commit $ReleaseCommit."
    }
}

if (-not $Create) {
    & (Join-Path $ToolkitRoot "worker-vm-host-preflight.ps1")
    exit $LASTEXITCODE
}

& (Join-Path $ToolkitRoot "prepare-worker-vm-host.ps1") `
    -SourceRoot $SourceRoot `
    -WindowsIso $WindowsIso `
    -InstallVirtualBox:$InstallVirtualBox `
    -AcceptWarnings:$AcceptWarnings
