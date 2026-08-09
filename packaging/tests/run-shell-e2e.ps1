<#
.SYNOPSIS
    End-to-end shell tests (GUI session required): ready / timeout / version
    mismatch / crash+restart / startup failure / single instance /
    kill-on-parent-exit.  Asserts against the shell log file.

.PARAMETER ShellExe
    Path to the debug shell exe (default: desktop_shell\src-tauri\target\debug\winefox-desktop.exe).

.PARAMETER SidecarExe
    Path to a packaged sidecar exe (default: packaging\dist\winefox-sidecar\winefox-sidecar.exe).

.PARAMETER KeepRunning
    Leave the last instance running (for manual inspection) instead of killing it.

.EXAMPLE
    powershell -File packaging\tests\run-shell-e2e.ps1
#>
[CmdletBinding()]
param(
    [string]$ShellExe = "",
    [string]$SidecarExe = "",
    [switch]$KeepRunning
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)   # repo root
if (-not $ShellExe) { $ShellExe = Join-Path $Root "desktop_shell\src-tauri\target\debug\winefox-desktop.exe" }
if (-not $SidecarExe) { $SidecarExe = Join-Path $Root "packaging\dist\winefox-sidecar\winefox-sidecar.exe" }
if (-not (Test-Path $ShellExe)) { throw "shell not built: $ShellExe" }

$LogBase = Join-Path $env:TEMP "wf-e2e"
Remove-Item -Recurse -Force $LogBase -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Force -Path $LogBase | Out-Null
$failures = @()
$env:WINE_FOX_MAX_RESTARTS = "2"

function Stop-Shell {
    Get-Process winefox-desktop, WineFox -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Get-Process winefox-sidecar -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
    Start-Sleep -Milliseconds 800
}

function Run-Case($name, $envSidecar, $seconds, $expect) {
    Stop-Shell
    $dir = Join-Path $LogBase $name
    New-Item -ItemType Directory -Force -Path $dir | Out-Null
    $env:WINE_FOX_SIDECAR = $envSidecar
    $env:WINE_FOX_LOG_DIR = $dir
    $env:WINE_FOX_READY_TIMEOUT_MS = "4000"
    Start-Process $ShellExe | Out-Null
    Start-Sleep -Seconds $seconds
    $log = Join-Path $dir "winefox.log"
    $text = if (Test-Path $log) { Get-Content $log -Raw -Encoding UTF8 } else { "" }
    if ($text -match $expect) {
        Write-Host "[PASS] $name" -ForegroundColor Green
    } else {
        Write-Host "[FAIL] $name : expected '$expect' in log" -ForegroundColor Red
        $script:failures += $name
        Get-Content $log -ErrorAction SilentlyContinue | Select-Object -Last 6 | ForEach-Object { Write-Host "   $_" }
    }
}

Write-Host "== WineFox shell e2e ==" -ForegroundColor Cyan

Run-Case "ready"            $SidecarExe                                             7 'sidecar ready'
Run-Case "timeout"          (Join-Path $PSScriptRoot "wrapper_no_ready.py")         6 '握手超时'
Run-Case "version-mismatch" (Join-Path $PSScriptRoot "wrapper_version_mismatch.py") 6 '版本不匹配'
Run-Case "crash-restart"    (Join-Path $PSScriptRoot "wrapper_crash.py")           16 '自动重启'
Run-Case "startup-failure"  "D:\no-such-sidecar.exe"                                4 '启动 sidecar 失败'

# --------------------------------------------------------------- single instance
Stop-Shell
$dir = Join-Path $LogBase "single-instance"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$env:WINE_FOX_SIDECAR = $SidecarExe
$env:WINE_FOX_LOG_DIR = $dir
Start-Process $ShellExe | Out-Null
Start-Sleep -Seconds 5
$p2 = Start-Process $ShellExe -PassThru -Wait
Start-Sleep -Seconds 1
$alive = @(Get-Process winefox-desktop, WineFox -ErrorAction SilentlyContinue).Count
if ($alive -eq 1) { Write-Host "[PASS] single-instance" -ForegroundColor Green }
else { Write-Host "[FAIL] single-instance (running=$alive)" -ForegroundColor Red; $failures += "single-instance" }

# ------------------------------------------------------------ kill-on-parent-exit
$before = @(Get-Process winefox-sidecar -ErrorAction SilentlyContinue).Count
Stop-Shell
Start-Sleep -Seconds 2
$after = @(Get-Process winefox-sidecar -ErrorAction SilentlyContinue).Count
if ($before -gt 0 -and $after -eq 0) { Write-Host "[PASS] kill-on-parent-exit" -ForegroundColor Green }
else { Write-Host "[FAIL] kill-on-parent-exit (before=$before after=$after)" -ForegroundColor Red; $failures += "kill-on-parent-exit" }

if ($KeepRunning) {
    Start-Process $ShellExe | Out-Null
    Write-Host "instance left running for manual inspection" -ForegroundColor Yellow
}

if ($failures.Count -gt 0) {
    Write-Host "FAILURES: $($failures -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host "ALL SHELL E2E PASSED" -ForegroundColor Green
exit 0
