[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9._ -]+$')]
    [string]$VmName = "Avito-CRM-Worker"
)

$ErrorActionPreference = "Stop"
$VirtualBoxPath = Join-Path $env:ProgramFiles "Oracle\VirtualBox\VBoxManage.exe"
if (-not (Test-Path -LiteralPath $VirtualBoxPath -PathType Leaf)) {
    throw "VBoxManage не найден."
}

$Info = & $VirtualBoxPath showvminfo $VmName --machinereadable 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "VM '$VmName' не найдена."
}
if ($Info -notmatch 'SharedFolderNameMachineMapping\d+="AvitoCrmTransfer"') {
    Write-Host "Папка AvitoCrmTransfer уже отключена."
    exit 0
}

& $VirtualBoxPath sharedfolder remove $VmName --name "AvitoCrmTransfer"
if ($LASTEXITCODE -ne 0) {
    throw "VirtualBox не смог отключить AvitoCrmTransfer."
}
Write-Host "TRANSFER DISCONNECTED: VM=$VmName; share=absent" -ForegroundColor Green
Write-Host "Файлы на хосте не удалялись. Удалите папку AvitoCrm-secure-transfer-* после контроля VM."
