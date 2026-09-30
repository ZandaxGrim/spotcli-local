$ErrorActionPreference = "Stop"
$InstallRoot = Join-Path $env:LOCALAPPDATA "spotcli-local"
$Python = Join-Path $InstallRoot ".venv\Scripts\python.exe"

if (-not (Test-Path $Python)) {
    Write-Error "spotcli-local is not installed yet. Run .\install.ps1 first."
    exit 1
}

& $Python -m pip install --upgrade $PSScriptRoot
Write-Host "spotcli-local updated. Your config and persistent cache were preserved."
