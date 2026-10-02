param(
    [switch]$NoStartup
)

$ErrorActionPreference = "Stop"
$InstallRoot = Join-Path $env:LOCALAPPDATA "spotcli-local"
$Venv = Join-Path $InstallRoot ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Launcher = Join-Path $InstallRoot "spotcli.cmd"

Write-Host "Installing spotcli-local into $InstallRoot"
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null

if (-not (Test-Path $Python)) {
    py -m venv $Venv
}

& $Python -m pip install --upgrade pip
& $Python -m pip install --upgrade --force-reinstall $PSScriptRoot

@"
@echo off
"$Python" -m spotcli.app %*
"@ | Set-Content -Encoding ASCII $Launcher

##Put our launcher FIRST in PATH.
##If an old pip install left a spotcli.exe somewhere else, we don't want Windows
##randomly grabbing that stale copy instead of the install we literally just made.
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$PathParts = @()

if ($UserPath) {
    $PathParts = $UserPath -split ';' |
        Where-Object { $_ -and $_.Trim() -and $_.TrimEnd('\') -ne $InstallRoot.TrimEnd('\') }
}

$NewUserPath = (($InstallRoot) + $PathParts) -join ';'
[Environment]::SetEnvironmentVariable("Path", $NewUserPath, "User")

##Do the same for this PowerShell window so `spotcli` works immediately.
$CurrentParts = $env:Path -split ';' |
    Where-Object { $_ -and $_.Trim() -and $_.TrimEnd('\') -ne $InstallRoot.TrimEnd('\') }
$env:Path = (($InstallRoot) + $CurrentParts) -join ';'

if (-not $NoStartup) {
    & $Python -m spotcli.app --install-startup
}

Write-Host ""
Write-Host "Installed."
Write-Host "Launcher:         $Launcher"
Write-Host "Terminal command: spotcli"
Write-Host "Config/cache:     $env:APPDATA\spotcli"
Write-Host ""

$Resolved = Get-Command spotcli -ErrorAction SilentlyContinue
if ($Resolved) {
    Write-Host "spotcli resolves to: $($Resolved.Source)"
}

if ($Resolved -and $Resolved.Source -ne $Launcher) {
    Write-Warning "This shell still resolves another spotcli first. Open a new terminal and run: Get-Command spotcli"
}
