# WineFox 更新清单签名工具（发布机专用）。
# 用法：
#   rtk powershell -NoProfile -ExecutionPolicy Bypass -File packaging\updater\build-feed.ps1 [-Tag v8.6.0] [-Notes "..."]
# 前提：
#   * 已构建 packaging\nsis\WineFox-Setup-<版本>-x64.exe
#   * 私钥位于 ~\.winefox\update.key（口令在 ~\.winefox\update.pass 或环境变量
#     WINEFOX_UPDATE_KEY_PASSPHRASE）
#   * python 可执行且已安装 cryptography
# 产物：packaging\nsis\latest.json（随安装包一起上传到 GitHub Release 即可）。

param(
    [string]$Tag = "",
    [string]$Notes = ""
)

$ErrorActionPreference = "Stop"

$updaterDir = $PSScriptRoot
$root = Split-Path -Parent (Split-Path -Parent $updaterDir)

$version = (Get-Content (Join-Path $root "packaging\version.json") | ConvertFrom-Json).app
$installer = Join-Path $root "packaging\nsis\WineFox-Setup-$version-x64.exe"
if (-not (Test-Path $installer)) {
    throw "未找到安装包：$installer（请先完成 WINDOWS_BUILD_RUNBOOK 第 7.4 步）"
}
if (-not $Tag) { $Tag = "v$version" }

$filename = "WineFox-Setup-$version-x64.exe"
$url = "https://github.com/XIAOKU2300/WineFoxDesktop/releases/download/$Tag/$filename"

$key = Join-Path $HOME ".winefox\update.key"
if (-not (Test-Path $key)) {
    throw "未找到更新私钥：$key（私钥只能在发布机，不得进入代码仓库）"
}

$out = Join-Path $root "packaging\nsis\latest.json"
& python (Join-Path $updaterDir "sign_feed.py") `
    --version $version `
    --url $url `
    --installer $installer `
    --key $key `
    --notes $Notes `
    --out $out
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

Write-Host "更新清单已生成：$out"
Write-Host "请把 latest.json 与本安装包一起上传到 GitHub Release（Tag 为 $Tag）"
