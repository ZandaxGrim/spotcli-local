param(
    [switch]$NoStartup
)

$ErrorActionPreference = "Stop"

$InstallRoot = Join-Path $env:LOCALAPPDATA "spotcli-local"
$Venv = Join-Path $InstallRoot ".venv"
$Python = Join-Path $Venv "Scripts\python.exe"
$Launcher = Join-Path $InstallRoot "spotcli.cmd"

function Test-SamePath([string]$A, [string]$B) {
    if ([string]::IsNullOrWhiteSpace($A) -or [string]::IsNullOrWhiteSpace($B)) {
        return $false
    }

    return $A.Trim().TrimEnd('\') -ieq $B.Trim().TrimEnd('\')
}

function Add-SpotCliToPath {
    ##Do not rebuild the user's PATH. We only prepend our launcher directory
    ##and leave every existing character after it alone. way less chance of
    ##Windows PATH bullshit eating somebody's setup.

    $UserPath = [Environment]::GetEnvironmentVariable("Path", "User")
    $HasInstallRoot = $false

    if ($UserPath) {
        foreach ($Entry in ($UserPath -split ';')) {
            if (Test-SamePath $Entry $InstallRoot) {
                $HasInstallRoot = $true
                break
            }
        }
    }

    if (-not $HasInstallRoot) {
        if ([string]::IsNullOrWhiteSpace($UserPath)) {
            $NewUserPath = $InstallRoot
        } else {
            $NewUserPath = "$InstallRoot;$UserPath"
        }

        [Environment]::SetEnvironmentVariable("Path", $NewUserPath, "User")
        Write-Host "Added $InstallRoot to the front of your user PATH."
    } else {
        Write-Host "SpotCLI launcher is already in your user PATH."
    }

    ##Same thing for this PowerShell window so `spotcli` works right away.
    $CurrentHasInstallRoot = $false
    foreach ($Entry in ($env:Path -split ';')) {
        if (Test-SamePath $Entry $InstallRoot) {
            $CurrentHasInstallRoot = $true
            break
        }
    }

    if (-not $CurrentHasInstallRoot) {
        $env:Path = "$InstallRoot;$env:Path"
    }
}

Write-Host "Installing spotcli-local into $InstallRoot"
New-Item -ItemType Directory -Force -Path $InstallRoot | Out-Null

if (-not (Test-Path $Python)) {
    $PyLauncher = Get-Command py -ErrorAction SilentlyContinue
    $PythonCommand = Get-Command python -ErrorAction SilentlyContinue

    if ($PyLauncher) {
        & $PyLauncher.Source -m venv $Venv
    } elseif ($PythonCommand) {
        & $PythonCommand.Source -m venv $Venv
    } else {
        throw "Python was not found. Install Python 3.11+ and run install.ps1 again."
    }
}

##Compile the important source files before touching the installed package.
##If somebody downloaded a broken edit, fail here instead of replacing a working install.
Write-Host "Checking source..."
& $Python -m py_compile (Join-Path $PSScriptRoot "src\spotcli\app.py")
& $Python -m py_compile (Join-Path $PSScriptRoot "src\spotcli\media\windows.py")
Write-Host "Source check passed."

& $Python -m pip install --upgrade pip
& $Python -m pip install --upgrade --force-reinstall $PSScriptRoot

@"
@echo off
"$Python" -m spotcli.app %*
"@ | Set-Content -Encoding ASCII $Launcher

Add-SpotCliToPath

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

if (-not $Resolved -or -not (Test-SamePath $Resolved.Source $Launcher)) {
    Write-Warning "Open a new terminal and run: Get-Command spotcli"
}
