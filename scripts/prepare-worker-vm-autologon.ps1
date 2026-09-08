[CmdletBinding(SupportsShouldProcess = $true)]
param()

$ErrorActionPreference = "Stop"
$DownloadUri = "https://download.sysinternals.com/files/AutoLogon.zip"
$TargetDir = Join-Path $env:ProgramData "AvitoCrmVM\SysinternalsAutologon"
$ZipPath = Join-Path ([IO.Path]::GetTempPath()) ("Autologon-" + [guid]::NewGuid().ToString("N") + ".zip")

$Computer = Get-CimInstance Win32_ComputerSystem
$VmIdentity = "$($Computer.Manufacturer) $($Computer.Model)"
if ($VmIdentity -notmatch "VirtualBox|VMware|Virtual Machine|KVM|QEMU|Parallels|Xen") {
    throw "Автовход разрешён этим скриптом только внутри VM."
}
if (-not $PSCmdlet.ShouldProcess("Windows VM", "скачать и открыть Microsoft Sysinternals Autologon")) {
    return
}

try {
    Invoke-WebRequest -UseBasicParsing -Uri $DownloadUri -OutFile $ZipPath
    New-Item -ItemType Directory -Path $TargetDir -Force | Out-Null
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $TargetDir -Force
    $Exe = Join-Path $TargetDir "Autologon64.exe"
    if (-not (Test-Path -LiteralPath $Exe -PathType Leaf)) {
        throw "В архиве Microsoft нет Autologon64.exe"
    }
    $Signature = Get-AuthenticodeSignature -FilePath $Exe
    if (
        $Signature.Status -ne "Valid" -or
        [string]$Signature.SignerCertificate.Subject -notmatch "Microsoft"
    ) {
        throw "Цифровая подпись Microsoft Sysinternals не прошла проверку."
    }
    Start-Process -FilePath $Exe -ArgumentList "/accepteula"
}
finally {
    Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
}

Write-Host "Открыт официальный Microsoft Sysinternals Autologon." -ForegroundColor Green
Write-Host "Введите только пароль отдельной учётной записи VM и нажмите Enable."
Write-Host "Пароль хранится Windows как LSA secret; администратор хоста всё равно имеет доступ к диску VM."
