$ErrorActionPreference = "Stop"

$InstallRoot = Join-Path $env:LOCALAPPDATA "spotcli-local"
$Python = Join-Path $InstallRoot ".venv\Scripts\python.exe"
$Launcher = Join-Path $InstallRoot "spotcli.cmd"

function Test-SamePath([string]$A, [string]$B) {
    if ([string]::IsNullOrWhiteSpace($A) -or [string]::IsNullOrWhiteSpace($B)) {
        return $false
    }

    return $A.Trim().TrimEnd('\') -ieq $B.Trim().TrimEnd('\')
}

function Add-SpotCliToPath {
    ##Never reconstruct the full PATH here. Just prepend our launcher when
    ##it's missing and leave the rest of the user's PATH exactly as it was.

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
    }

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

if (-not (Test-Path $Python)) {
    Write-Error "spotcli-local is not installed yet. Run .\install.ps1 first."
    exit 1
}

Write-Host "Updating spotcli-local from:"
Write-Host $PSScriptRoot
Write-Host ""

##Check the source before pip gets a chance to replace the working install.
Write-Host "Checking source..."
& $Python -m py_compile (Join-Path $PSScriptRoot "src\spotcli\app.py")
& $Python -m py_compile (Join-Path $PSScriptRoot "src\spotcli\media\windows.py")
Write-Host "Source check passed."

##Force reinstall from this exact folder. This avoids stale site-packages copies
##while we're updating from an extracted release or local checkout.
& $Python -m pip install --upgrade --force-reinstall $PSScriptRoot

@"
@echo off
"$Python" -m spotcli.app %*
"@ | Set-Content -Encoding ASCII $Launcher

Add-SpotCliToPath

Write-Host ""
Write-Host "spotcli-local updated."
Write-Host "Installed app:"
& $Python -c "import spotcli.app; print(spotcli.app.__file__)"

$Resolved = Get-Command spotcli -ErrorAction SilentlyContinue
if ($Resolved) {
    Write-Host "spotcli resolves to: $($Resolved.Source)"
}

if (-not $Resolved -or -not (Test-SamePath $Resolved.Source $Launcher)) {
    Write-Warning "Open a new terminal and run: Get-Command spotcli"
}
