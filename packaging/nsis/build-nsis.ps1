<#
.SYNOPSIS
    Stage the NSIS payload and compile installer.nsi with makensis.

.DESCRIPTION
    payload/ layout expected by installer.nsi:
        payload\WineFox.exe            (tauri release build)
        payload\winefox-sidecar.exe    (pyinstaller onedir exe)
        payload\*                      (any extra files, e.g. LICENSE)

    Version files are stamped from packaging\version.json.

.PARAMETER TauriExe
    Path to WineFox.exe (default: packaging\dist\tauri\WineFox.exe or
    desktop_shell\src-tauri\target\release\WineFox.exe).

.PARAMETER SidecarExe
    Path to winefox-sidecar.exe (default: packaging\dist\winefox-sidecar\winefox-sidecar.exe).

.PARAMETER Makensis
    Path to makensis.exe (default: from PATH, or portable NSIS in packaging\.build\nsis).
#>
[CmdletBinding()]
param(
    [string]$TauriExe = "",
    [string]$SidecarExe = "",
    [string]$Makensis = ""
)

$ErrorActionPreference = "Stop"
# this script lives in <root>\packaging\nsis
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$Packaging = Join-Path $Root "packaging"
$Nsis = Join-Path $Packaging "nsis"
$Payload = Join-Path $Nsis "payload"

if (-not $TauriExe) {
    $cands = @(
        (Join-Path $Packaging "dist\tauri\WineFox.exe"),
        (Join-Path $Packaging "dist\WineFox\WineFox.exe"),
        (Join-Path $Root "desktop_shell\src-tauri\target\release\WineFox.exe")
    )
    $TauriExe = $cands | Where-Object { Test-Path $_ } | Select-Object -First 1
}
if (-not $TauriExe) { throw "WineFox.exe not found; build with packaging\build.ps1 first" }

if (-not $SidecarExe) {
    $SidecarExe = Join-Path $Packaging "dist\winefox-sidecar\winefox-sidecar.exe"
}
if (-not (Test-Path $SidecarExe)) { throw "winefox-sidecar.exe not found: $SidecarExe" }

$versions = Get-Content (Join-Path $Packaging "version.json") | ConvertFrom-Json
$VERSION = $versions.app

# ------------------------------------------------------------ stage payload
Remove-Item -Recurse -Force $Payload -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $Payload | Out-Null
Copy-Item $TauriExe (Join-Path $Payload "WineFox.exe") -Force
# The sidecar is a PyInstaller ONEDIR build: the exe needs its _internal
# directory next to it (without it the bootloader hangs).  Copy the whole
# onedir into the payload.
$SidecarDist = Split-Path $SidecarExe
Copy-Item $SidecarExe (Join-Path $Payload "winefox-sidecar.exe") -Force
if (Test-Path (Join-Path $SidecarDist "_internal")) {
    Copy-Item -Recurse -Force (Join-Path $SidecarDist "_internal") (Join-Path $Payload "_internal")
} else {
    Write-Warning "no _internal dir next to $SidecarExe - onedir incomplete?"
}
$license = Get-ChildItem $Root -Recurse -Filter "LICENSE" -ErrorAction SilentlyContinue | Select-Object -First 1
if ($license) { Copy-Item $license.FullName (Join-Path $Payload "LICENSE.txt") -Force }
Set-Content (Join-Path $Payload "VERSION.txt") $VERSION -Encoding ASCII

# ------------------------------------------------------------- version check
# main program and sidecar must be the same version (enforced at build time)
$mainVer = (Get-Item (Join-Path $Payload "WineFox.exe")).VersionInfo.FileVersion
$sideVer = (Get-Item (Join-Path $Payload "winefox-sidecar.exe")).VersionInfo.FileVersion
Write-Host "WineFox.exe      version: $mainVer"
Write-Host "winefox-sidecar  version: $sideVer"
if ($mainVer -ne $VERSION -or $sideVer -ne $VERSION) {
    throw "version mismatch: app=$mainVer sidecar=$sideVer expected=$VERSION"
}

# ------------------------------------------------------------- makensis
if (-not $Makensis) {
    $Makensis = (Get-Command "makensis.exe" -ErrorAction SilentlyContinue).Source
    if (-not $Makensis) {
        $portable = Join-Path $Packaging ".build\nsis\makensis.exe"
        if (Test-Path $portable) { $Makensis = $portable }
    }
}
if (-not $Makensis) { throw "makensis.exe not found (install NSIS or extract a portable copy to packaging\.build\nsis)" }

Push-Location $Nsis
try {
    & $Makensis "installer.nsi"
    if ($LASTEXITCODE -ne 0) { throw "makensis failed (rc=$LASTEXITCODE)" }
} finally { Pop-Location }
Write-Host "installer: $Nsis\WineFox-Setup-$VERSION-x64.exe" -ForegroundColor Green
