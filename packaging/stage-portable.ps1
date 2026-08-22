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

$StageDestination = Join-Path $DistRoot ("WineFoxDesktop.__stage_{0}" -f [guid]::NewGuid().ToString("N"))
if (-not $StageDestination.StartsWith($DistRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Portable staging path escaped $DistRoot"
}

# Build a complete replacement tree first. This keeps the existing portable
# folder usable when it is open in Explorer or held by a still-shutting-down
# process. In that case the final mirror fallback below updates files in place.
New-Item -ItemType Directory -Path $StageDestination | Out-Null
try {
    Copy-Item -LiteralPath (Join-Path $SidecarDir "winefox-sidecar.exe") -Destination (Join-Path $StageDestination "winefox-sidecar.exe")
    Copy-Item -LiteralPath (Join-Path $SidecarDir "_internal") -Destination (Join-Path $StageDestination "_internal") -Recurse
    Copy-Item -LiteralPath $ShellExe -Destination (Join-Path $StageDestination "WineFox.exe")
    Copy-Item -LiteralPath (Join-Path $Root "desktop_shell\VERSION.txt") -Destination (Join-Path $StageDestination "VERSION.txt")

    $replaced = $false
    if (Test-Path -LiteralPath $Destination) {
        try {
            Remove-Item -LiteralPath $Destination -Recurse -Force -ErrorAction Stop
        } catch {
            Write-Warning "Portable directory is locked; falling back to in-place mirror: $($_.Exception.Message)"
        }
    }

    if (-not (Test-Path -LiteralPath $Destination)) {
        try {
            Move-Item -LiteralPath $StageDestination -Destination $Destination -Force -ErrorAction Stop
            $replaced = $true
        } catch {
            Write-Warning "Atomic portable replacement unavailable; using in-place mirror: $($_.Exception.Message)"
        }
    }

    if (-not $replaced) {
        if (-not (Test-Path -LiteralPath $Destination)) {
            New-Item -ItemType Directory -Path $Destination | Out-Null
        }
        $robocopy = Get-Command "robocopy.exe" -ErrorAction SilentlyContinue
        if (-not $robocopy) {
            throw "robocopy.exe is required for locked portable-directory fallback"
        }
        & $robocopy.Source $StageDestination $Destination /MIR /COPY:DAT /DCOPY:DAT /R:2 /W:1 /NFL /NDL /NJH /NJS
        $rc = $LASTEXITCODE
        if ($rc -gt 7) { throw "robocopy portable mirror failed (rc=$rc)" }
    }
} finally {
    if (Test-Path -LiteralPath $StageDestination) {
        try {
            [System.IO.Directory]::Delete($StageDestination, $true)
        } catch {
            Write-Warning "Could not remove temporary portable stage ${StageDestination}: $($_.Exception.Message)"
        }
    }
}

$size = [math]::Round((Get-ChildItem -LiteralPath $Destination -Recurse -File | Measure-Object Length -Sum).Sum / 1MB, 1)
Write-Host "Portable WineFox staged at $Destination ($size MB)"
