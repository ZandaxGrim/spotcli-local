$ErrorActionPreference = "Stop"

$InstallRoot = Join-Path $env:LOCALAPPDATA "spotcli-local"
$Python = Join-Path $InstallRoot ".venv\Scripts\python.exe"
$Launcher = Join-Path $InstallRoot "spotcli.cmd"

if (-not (Test-Path $Python)) {
    Write-Error "spotcli-local is not installed yet. Run .\install.ps1 first."
    exit 1
}

Write-Host "Updating spotcli-local from:"
Write-Host $PSScriptRoot
Write-Host ""

##Force reinstall from this exact folder so an old wheel/site-packages copy
##can't hang around and make debugging way more annoying than it needs to be.
& $Python -m pip install --upgrade --force-reinstall $PSScriptRoot

@"
@echo off
"$Python" -m spotcli.app %*
"@ | Set-Content -Encoding ASCII $Launcher

##Keep our launcher first in PATH for the same reason install.ps1 does.
##Old pip installs can leave a spotcli.exe in Python\Scripts and steal the command.
$UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
$PathParts = @()

if ($UserPath) {
    $PathParts = $UserPath -split ';' |
        Where-Object { $_ -and $_.Trim() -and $_.TrimEnd('\') -ne $InstallRoot.TrimEnd('\') }
}

$NewUserPath = (($InstallRoot) + $PathParts) -join ';'
[Environment]::SetEnvironmentVariable("Path", $NewUserPath, "User")

$CurrentParts = $env:Path -split ';' |
    Where-Object { $_ -and $_.Trim() -and $_.TrimEnd('\') -ne $InstallRoot.TrimEnd('\') }
$env:Path = (($InstallRoot) + $CurrentParts) -join ';'

Write-Host ""
Write-Host "spotcli-local updated."
Write-Host "Installed app:"
& $Python -c "import spotcli.app; print(spotcli.app.__file__)"

$Resolved = Get-Command spotcli -ErrorAction SilentlyContinue
if ($Resolved) {
    Write-Host "spotcli resolves to: $($Resolved.Source)"
}

if ($Resolved -and $Resolved.Source -ne $Launcher) {
    Write-Warning "This shell still resolves another spotcli first. Open a new terminal and run: Get-Command spotcli"
}
