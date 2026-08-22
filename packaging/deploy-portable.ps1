<#
.SYNOPSIS
    一键部署：把最新构建的 sidecar + Tauri 壳更新进 dist\WineFoxDesktop 便携版。

.DESCRIPTION
    前置条件（已由本次会话完成）：
      * packaging\dist\winefox-sidecar\          — PyInstaller onedir（已重建）
      * desktop_shell\src-tauri\target\release\winefox-desktop.exe — Rust 壳（已重编）

    运行前请先完全退出 WineFox（托盘右键退出），否则文件被锁会替换失败。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File packaging\deploy-portable.ps1
#>
[CmdletBinding()]
param()
$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$SidecarDir = Join-Path $PSScriptRoot "dist\winefox-sidecar"
$ShellExe = Join-Path $Root "desktop_shell\src-tauri\target\release\winefox-desktop.exe"
$Dest = Join-Path $PSScriptRoot "dist\WineFoxDesktop"

if (-not (Test-Path -LiteralPath (Join-Path $SidecarDir "winefox-sidecar.exe"))) {
    throw "Missing rebuilt sidecar: $SidecarDir (run packaging\build.ps1 -SkipTauri -SkipNsis first)"
}
if (-not (Test-Path -LiteralPath $ShellExe)) {
    throw "Missing shell exe: $ShellExe (run cargo build --release in desktop_shell\src-tauri)"
}

foreach ($proc in @("WineFox.exe", "winefox-sidecar.exe")) {
    $running = Get-Process -Name ([IO.Path]::GetFileNameWithoutExtension($proc)) -ErrorAction SilentlyContinue
    if ($running) {
        throw "$proc 仍在运行（PID $($running.Id -join ', ')）。请先完全退出 WineFox（含托盘图标）再重新运行本脚本。"
    }
}

New-Item -ItemType Directory -Path $Dest -Force | Out-Null
# 优先整体替换 _internal（先删旧再拷新，避免混版本残留）
$oldInternal = Join-Path $Dest "_internal"
if (Test-Path -LiteralPath $oldInternal) { Remove-Item -LiteralPath $oldInternal -Recurse -Force }
Copy-Item -LiteralPath (Join-Path $SidecarDir "_internal") -Destination $oldInternal -Recurse
Copy-Item -LiteralPath (Join-Path $SidecarDir "winefox-sidecar.exe") -Destination (Join-Path $Dest "winefox-sidecar.exe") -Force
Copy-Item -LiteralPath $ShellExe -Destination (Join-Path $Dest "WineFox.exe") -Force
Copy-Item -LiteralPath (Join-Path $Root "desktop_shell\VERSION.txt") -Destination (Join-Path $Dest "VERSION.txt") -Force

Write-Host "部署完成 -> $Dest"
Write-Host "可以直接运行 $Dest\WineFox.exe 验证 LoRA 搜索（目标：10 秒内出结果）。"
