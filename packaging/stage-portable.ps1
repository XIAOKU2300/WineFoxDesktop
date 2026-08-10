[CmdletBinding()]
param(
    [string]$Destination = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
$SidecarDir = Join-Path $PSScriptRoot "dist\winefox-sidecar"
$ShellExe = Join-Path $Root "desktop_shell\src-tauri\target\release\winefox-desktop.exe"
if (-not $Destination) {
    $Destination = Join-Path $PSScriptRoot "dist\WineFoxDesktop"
}
$Destination = [System.IO.Path]::GetFullPath($Destination)
$DistRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "dist"))
if (-not $Destination.StartsWith($DistRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Portable destination must stay under $DistRoot"
}
if (-not (Test-Path -LiteralPath $SidecarDir)) { throw "Missing sidecar onedir: $SidecarDir" }
if (-not (Test-Path -LiteralPath $ShellExe)) { throw "Missing Tauri release executable: $ShellExe" }

if (Test-Path -LiteralPath $Destination) {
    Remove-Item -LiteralPath $Destination -Recurse -Force
}
New-Item -ItemType Directory -Path $Destination | Out-Null
Copy-Item -LiteralPath (Join-Path $SidecarDir "winefox-sidecar.exe") -Destination (Join-Path $Destination "winefox-sidecar.exe")
Copy-Item -LiteralPath (Join-Path $SidecarDir "_internal") -Destination (Join-Path $Destination "_internal") -Recurse
Copy-Item -LiteralPath $ShellExe -Destination (Join-Path $Destination "WineFox.exe")
Copy-Item -LiteralPath (Join-Path $Root "desktop_shell\VERSION.txt") -Destination (Join-Path $Destination "VERSION.txt")

$size = [math]::Round((Get-ChildItem -LiteralPath $Destination -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "Portable WineFox staged at $Destination ($size MB)"
