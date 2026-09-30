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
& $Python -m pip install --upgrade $PSScriptRoot

@"
@echo off
"$Python" -m spotcli.app %*
"@ | Set-Content -Encoding ASCII $Launcher

# Add the stable launcher directory to the current user's PATH so `spotcli`
# works from new PowerShell/cmd windows without activating the venv.
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$PathParts = @()
if ($UserPath) {
    $PathParts = $UserPath -split ';' | Where-Object { $_ -and $_.Trim() }
}
if ($PathParts -notcontains $InstallRoot) {
    $NewUserPath = (($PathParts + $InstallRoot) -join ';')
    [Environment]::SetEnvironmentVariable("Path", $NewUserPath, "User")
    Write-Host "Added $InstallRoot to your user PATH."
} else {
    Write-Host "spotcli install directory is already on your user PATH."
}
if (($env:Path -split ';') -notcontains $InstallRoot) {
    $env:Path = "$env:Path;$InstallRoot"
}

if (-not $NoStartup) {
    & $Python -m spotcli.app --install-startup
}

Write-Host ""
Write-Host "Installed. You do not need to rebuild or reinstall on each launch."
Write-Host "Manual launcher: $Launcher"
Write-Host "Terminal command: spotcli (open a new terminal if this one does not see the PATH update)"
Write-Host "Config/cache:     $env:APPDATA\spotcli"
Write-Host ""
Write-Host "To update later, extract a newer spotcli release and run .\update.ps1"
