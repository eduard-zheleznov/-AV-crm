[CmdletBinding()]
param(
    [ValidateSet("google", "xlsx", "csv")]
    [string]$Source,
    [string]$File,
    [string]$Sheet,
    [switch]$OnlineCrm
)

$ErrorActionPreference = "Stop"
$ProjectRoot = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) { throw "Сначала выполните .\scripts\install.ps1" }

$Arguments = @("-m", "avito_crm", "doctor")
if ($Source) { $Arguments += @("--source", $Source) }
if ($File) { $Arguments += @("--file", $File) }
if ($Sheet) { $Arguments += @("--sheet", $Sheet) }
if ($OnlineCrm) { $Arguments += "--online-crm" }
& $Python @Arguments
exit $LASTEXITCODE
