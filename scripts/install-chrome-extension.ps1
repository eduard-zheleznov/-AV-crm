[CmdletBinding()]
param(
    [ValidateRange(1024, 65535)]
    [int]$Port = 8765
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$EnvPath = Join-Path $ProjectRoot ".env"
$ExtensionDir = Join-Path $ProjectRoot "chrome-extension"
$ConfigPath = Join-Path $ExtensionDir "config.local.js"

if (-not (Test-Path -LiteralPath $EnvPath)) {
    throw "Сначала выполните .\scripts\install.ps1: файл .env не найден"
}
if (-not (Test-Path -LiteralPath (Join-Path $ExtensionDir "manifest.json"))) {
    throw "Папка расширения повреждена: manifest.json не найден"
}

$EnvText = [System.IO.File]::ReadAllText($EnvPath)
$ExistingToken = [regex]::Match(
    $EnvText,
    "(?m)^AVITO_EXTENSION_TOKEN=([A-Za-z0-9_-]{32,})$"
)
if ($ExistingToken.Success) {
    $Token = $ExistingToken.Groups[1].Value
}
else {
    $TokenBytes = New-Object byte[] 32
    $Random = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $Random.GetBytes($TokenBytes)
    }
    finally {
        $Random.Dispose()
    }
    $Token = -join ($TokenBytes | ForEach-Object { $_.ToString("x2") })
}

$Values = [ordered]@{
    "AVITO_BROWSER_DRIVER" = "chrome_extension"
    "AVITO_EXTENSION_PORT" = [string]$Port
    "AVITO_EXTENSION_TOKEN" = $Token
    "AVITO_EXTENSION_INCOGNITO" = "true"
}
foreach ($Name in $Values.Keys) {
    $Pattern = "(?m)^" + [regex]::Escape($Name) + "=.*$"
    $Line = "$Name=$($Values[$Name])"
    $Count = [regex]::Matches($EnvText, $Pattern).Count
    if ($Count -gt 1) {
        throw "$Name встречается в .env несколько раз"
    }
    if ($Count -eq 1) {
        $EnvText = [regex]::Replace($EnvText, $Pattern, $Line)
    }
    else {
        if (-not $EnvText.EndsWith("`n")) {
            $EnvText += "`r`n"
        }
        $EnvText += "$Line`r`n"
    }
}

$DataDir = Join-Path $ProjectRoot "data"
New-Item -ItemType Directory -Path $DataDir -Force | Out-Null
Copy-Item -LiteralPath $EnvPath -Destination (Join-Path $DataDir ".env.before-extension") -Force
$Utf8Bom = New-Object System.Text.UTF8Encoding($true)
[System.IO.File]::WriteAllText($EnvPath, $EnvText, $Utf8Bom)

$ConfigText = @"
globalThis.AVITO_CRM_CONFIG = Object.freeze({
  port: $Port,
  token: "$Token"
});
"@
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($ConfigPath, $ConfigText, $Utf8NoBom)

$ChromeCandidates = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe"
)
$Chrome = $ChromeCandidates | Where-Object { $_ -and (Test-Path -LiteralPath $_) } |
    Select-Object -First 1
if (-not $Chrome) {
    throw "Обычный Google Chrome не найден"
}

Start-Process explorer.exe -ArgumentList "/e,`"$ExtensionDir`""
Start-Process $Chrome -ArgumentList "chrome://extensions/"

Write-Host ""
Write-Host "Локальный мост Chrome в режиме инкогнито подготовлен." -ForegroundColor Green
Write-Host "1. В открывшемся Chrome включите 'Режим разработчика'."
Write-Host "2. Нажмите 'Загрузить распакованное расширение'."
Write-Host "3. Выберите открытую папку: $ExtensionDir"
Write-Host "4. Откройте карточку расширения и включите 'Разрешить использование в режиме инкогнито'."
Write-Host "5. Нажмите 'Обновить' и проверьте версию расширения."
Write-Host "6. Не публикуйте config.local.js: в нём локальный секрет связи."
Write-Host "Перезагрузка Windows не требуется."
