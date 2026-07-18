[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("google", "xlsx", "csv")]
    [string]$Source,
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 10000)]
    [int]$Limit,
    [string]$File,
    [string]$Sheet,
    [ValidateSet("full", "capture", "crm")]
    [string]$Mode = "full",
    [switch]$Live,
    [switch]$RetryManual
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Сначала выполните .\scripts\install.ps1" }
if (($Source -eq "xlsx" -or $Source -eq "csv") -and -not $File) {
    throw "Для Source=$Source требуется -File"
}

$Command = switch ($Mode) {
    "capture" { "capture" }
    "crm" { "sync-crm" }
    default { "run" }
}
$Arguments = @("-m", "avito_crm", $Command, "--source", $Source, "--limit", "$Limit")
if ($File) { $Arguments += @("--file", $File) }
if ($Sheet) { $Arguments += @("--sheet", $Sheet) }
if ($Live) { $Arguments += "--live" }
if ($RetryManual) { $Arguments += "--retry-manual" }
& $Python @Arguments
exit $LASTEXITCODE
