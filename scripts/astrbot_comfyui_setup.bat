@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>nul
title AstrBot ComfyUI Runtime Setup
set "ASTRBOT_SETUP_VERSION=8.5.1"
set "ASTRBOT_SETUP_BAT=%~f0"
set "ASTRBOT_SETUP_TMP=%TEMP%\astrbot_comfyui_setup_%RANDOM%%RANDOM%.ps1"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell.exe"

echo.
echo ============================================================
echo  AstrBot ComfyUI 运行环境控制台  v%ASTRBOT_SETUP_VERSION%
echo ============================================================
echo  单文件万能脚本：安装、检测、修复、下载模型都在这里。
echo.
echo  常用模式:
echo    -Mode lite      轻量修复：Bridge + controlnet_aux + Depth-Anything
echo    -Mode full      完整绘图环境：模型 + 推荐节点 + Bridge
echo    -Mode complete  完整运行环境：绘图 + Anima/Qwen + Wan 视频 + Bridge
echo    -Mode check     只检测，不安装
echo.
echo  示例:
echo    %~nx0 -ComfyRoot "D:\ComfyUI-aki\ComfyUI" -Mode complete -UseHfMirror
echo.
if "%~1"=="" (
  echo  未传参数，将进入交互式路径选择和主菜单。
) else (
  echo  当前参数: %*
)
echo ============================================================
echo.

"%PS%" -NoProfile -ExecutionPolicy Bypass -Command "$ErrorActionPreference='Stop'; $path=$env:ASTRBOT_SETUP_BAT; $tmp=$env:ASTRBOT_SETUP_TMP; $text=[IO.File]::ReadAllText($path,[Text.Encoding]::UTF8); $marker='__ASTRBOT_POWERSHELL_BELOW__'; $idx=$text.LastIndexOf($marker); if($idx -lt 0){ throw '找不到内嵌 PowerShell 标记' }; $script=$text.Substring($idx + $marker.Length).TrimStart([char]13,[char]10); [IO.File]::WriteAllText($tmp,$script,[Text.UTF8Encoding]::new($true))"
if errorlevel 1 (
  echo [错误] 提取内嵌 PowerShell 脚本失败。
  if "%~1"=="" pause
  goto :fail
)

"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%ASTRBOT_SETUP_TMP%" %*
set "EXIT_CODE=%ERRORLEVEL%"
if exist "%ASTRBOT_SETUP_TMP%" del /f /q "%ASTRBOT_SETUP_TMP%" >nul 2>nul

echo.
if "%EXIT_CODE%"=="0" (
  echo [完成] AstrBot ComfyUI 脚本运行完成。
) else (
  echo [错误] AstrBot ComfyUI 脚本失败，退出码 %EXIT_CODE%。
)
if "%~1"=="" pause
exit /b %EXIT_CODE%

:fail
if exist "%ASTRBOT_SETUP_TMP%" del /f /q "%ASTRBOT_SETUP_TMP%" >nul 2>nul
exit /b 1

__ASTRBOT_POWERSHELL_BELOW__
param(
    [string]$ComfyRoot = "",

    [switch]$InstallCustomNodes,
    [switch]$DownloadMinimal,
    [switch]$InstallHqNode,
    [switch]$InstallAnimaNodes,
    [switch]$InstallWanNodes,
    [switch]$DownloadWanModels,
    [switch]$DownloadWanQuality,
    [switch]$DownloadClipVision,
    [switch]$DownloadAnimaModels,
    [switch]$InstallCompleteRuntime,
    [switch]$InstallLoraBridge,
    [string]$BridgeToken = "",
    [string]$BridgeProxy = "",
    [switch]$UseClashLan,
    [string]$ClashHost = "",
    [int]$ClashPort = 7890,
    [switch]$UseHfMirror,
    [ValidateSet("", "menu", "lite", "full", "complete", "check")]
    [string]$Mode = "",
    [switch]$CheckOnly,
    [switch]$NonInteractive
)

$ErrorActionPreference = "Stop"

function Write-Title {
    param([string]$Text)
    Write-Host ""
    Write-Host ("==== {0} ====" -f $Text)
}

function Write-Rule {
    Write-Host "======================================================================"
}

function Write-Header {
    param([string]$Subtitle = "")
    Clear-Host
    Write-Rule
    Write-Host " AstrBot ComfyUI 运行环境控制台"
    Write-Host "版本 8.5.1 |  单文件安装 / 修复 / 模型管理"
    if ($Subtitle) {
        Write-Host " $Subtitle"
    }
    Write-Rule
}

function Write-MenuItem {
    param(
        [string]$Key,
        [string]$Title,
        [string]$Hint = ""
    )
    if ($Hint) {
        Write-Host (" [{0}] {1}" -f $Key.PadLeft(2, "0"), $Title)
        Write-Host ("      {0}" -f $Hint)
    } else {
        Write-Host (" [{0}] {1}" -f $Key.PadLeft(2, "0"), $Title)
    }
}

function Wait-ReturnToMenu {
    Write-Host ""
    [void](Read-Host "按 Enter 返回主菜单")
}

function Normalize-InputPath {
    param([string]$Path)
    $value = str $Path
    return $value.Trim().Trim('"').Trim("'")
}

function str {
    param($Value)
    if ($null -eq $Value) { return "" }
    return [string]$Value
}

function Test-ComfyRoot {
    param([string]$Path)
    if (-not $Path -or -not (Test-Path -LiteralPath $Path)) {
        return $false
    }
    return (
        (Test-Path -LiteralPath (Join-Path $Path "models")) -and
        (
            (Test-Path -LiteralPath (Join-Path $Path "custom_nodes")) -or
            (Test-Path -LiteralPath (Join-Path $Path "main.py"))
        )
    )
}

function Find-ComfyRootCandidates {
    param([string]$InputPath)
    $raw = Normalize-InputPath $InputPath
    if (-not $raw -or -not (Test-Path -LiteralPath $raw)) {
        return @()
    }

    $resolved = (Resolve-Path -LiteralPath $raw -ErrorAction Stop).Path
    $candidates = New-Object System.Collections.Generic.List[string]

    foreach ($candidate in @(
        $resolved,
        (Join-Path $resolved "ComfyUI"),
        (Join-Path $resolved "ComfyUI_windows_portable\ComfyUI"),
        (Join-Path $resolved "A绘世启动器\ComfyUI")
    )) {
        if ($candidate -and -not $candidates.Contains($candidate)) {
            $candidates.Add($candidate)
        }
    }

    try {
        Get-ChildItem -LiteralPath $resolved -Directory -ErrorAction SilentlyContinue |
            ForEach-Object {
                foreach ($candidate in @(
                    $_.FullName,
                    (Join-Path $_.FullName "ComfyUI"),
                    (Join-Path $_.FullName "ComfyUI_windows_portable\ComfyUI")
                )) {
                    if ($candidate -and -not $candidates.Contains($candidate)) {
                        $candidates.Add($candidate)
                    }
                }
            }
    } catch {
        # Ignore scan errors and keep explicit candidates.
    }

    return @($candidates | Where-Object { Test-ComfyRoot $_ } | Select-Object -Unique)
}

function Resolve-ComfyRootInteractive {
    param([string]$InitialPath)

    $path = Normalize-InputPath $InitialPath
    while ($true) {
        if (-not $path) {
            Write-Host "请输入新机器秋叶整合包里的 ComfyUI 文件夹路径。"
            Write-Host "可以填 ComfyUI 根目录，也可以填它的上一级目录。"
            $path = Normalize-InputPath (Read-Host "ComfyUI 路径")
        }

        $candidates = @(Find-ComfyRootCandidates $path)
        if ($candidates.Count -eq 1) {
            return $candidates[0]
        }
        if ($candidates.Count -gt 1) {
            Write-Host ""
            Write-Host "检测到多个可能的 ComfyUI 根目录："
            for ($i = 0; $i -lt $candidates.Count; $i++) {
                Write-Host ("  {0}. {1}" -f ($i + 1), $candidates[$i])
            }
            $choice = Normalize-InputPath (Read-Host "选择序号")
            if ($choice -match '^\d+$') {
                $index = [int]$choice - 1
                if ($index -ge 0 -and $index -lt $candidates.Count) {
                    return $candidates[$index]
                }
            }
            Write-Warning "序号无效，重新输入。"
            $path = ""
            continue
        }

        Write-Warning "没有识别到 ComfyUI 根目录：$path"
        Write-Host "根目录里通常会有 models、custom_nodes、main.py。"
        $path = ""
    }
}

function Resolve-ComfyRootStrict {
    param([string]$Root)
    $candidates = @(Find-ComfyRootCandidates $Root)
    if ($candidates.Count -lt 1) {
        throw "这个路径不像 ComfyUI 根目录：$Root"
    }
    return $candidates[0]
}

function New-Dir {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) {
        New-Item -ItemType Directory -Path $Path | Out-Null
    }
}

function Convert-HfUrl {
    param([string]$Url)
    if ($script:UseHfMirror) {
        return $Url.Replace("https://huggingface.co/", "https://hf-mirror.com/")
    }
    return $Url
}

function Convert-HfMirrorUrl {
    param([string]$Url)
    return (str $Url).Replace("https://huggingface.co/", "https://hf-mirror.com/")
}

function Is-HuggingFaceUrl {
    param([string]$Url)
    return ((str $Url).StartsWith("https://huggingface.co/"))
}

function Convert-HfDirectUrl {
    param([string]$Url)
    return (str $Url).Replace("https://hf-mirror.com/", "https://huggingface.co/")
}

function Is-HfMirrorUrl {
    param([string]$Url)
    return ((str $Url).StartsWith("https://hf-mirror.com/"))
}

function Normalize-HttpProxy {
    param([string]$Proxy)
    $value = Normalize-InputPath (str $Proxy)
    if (-not $value) {
        return ""
    }
    if ($value -notmatch '^[A-Za-z][A-Za-z0-9+.-]*://') {
        $value = "http://$value"
    }
    return $value
}

function Resolve-DownloadProxy {
    return Normalize-HttpProxy (Resolve-BridgeProxy)
}

function Invoke-CurlReachability {
    param(
        [string]$Url,
        [string]$Proxy,
        [switch]$UseRangeProbe
    )
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if (-not $curl) {
        return $null
    }

    $curlArgs = @("-L", "--fail", "--silent", "--show-error", "--max-time", "25", "--retry", "1")
    if ($Proxy) {
        $curlArgs += @("--proxy", $Proxy)
    }
    if ($UseRangeProbe) {
        $curlArgs += @("--range", "0-0")
    } else {
        $curlArgs += @("-I")
    }
    $curlArgs += @("-o", "NUL", $Url)
    & curl.exe @curlArgs
    return ($LASTEXITCODE -eq 0)
}

function Invoke-WebRequestReachability {
    param(
        [string]$Url,
        [string]$Proxy,
        [switch]$UseRangeProbe
    )
    try {
        $args = @{
            Uri = $Url
            MaximumRedirection = 10
            TimeoutSec = 25
            UseBasicParsing = $true
        }
        if ($Proxy) {
            $args.Proxy = $Proxy
        }
        if ($UseRangeProbe) {
            $args.Method = "Get"
            $args.Headers = @{ Range = "bytes=0-0" }
        } else {
            $args.Method = "Head"
        }
        $response = Invoke-WebRequest @args
        return ([int]$response.StatusCode -ge 200 -and [int]$response.StatusCode -lt 400)
    } catch {
        return $false
    }
}

function Test-DownloadUrlReachable {
    param(
        [string]$Url,
        [string]$Proxy = ""
    )
    Write-Host "[检测] $Url"
    $headResult = Invoke-CurlReachability $Url $Proxy
    if ($null -ne $headResult) {
        if ($headResult) {
            Write-Host "        可访问"
            return $true
        }
        $rangeResult = Invoke-CurlReachability $Url $Proxy -UseRangeProbe
        if ($rangeResult) {
            Write-Host "        可访问"
            return $true
        }
        Write-Host "        不可访问"
        return $false
    }

    if (Invoke-WebRequestReachability $Url $Proxy) {
        Write-Host "        可访问"
        return $true
    }
    if (Invoke-WebRequestReachability $Url $Proxy -UseRangeProbe) {
        Write-Host "        可访问"
        return $true
    }
    Write-Host "        不可访问"
    return $false
}

function Resolve-ReachableDownloadUrl {
    param(
        [string]$Url,
        [string]$Proxy = ""
    )
    $candidates = New-Object System.Collections.Generic.List[string]
    $preferredUrl = Convert-HfUrl $Url
    if ($preferredUrl -and -not $candidates.Contains($preferredUrl)) {
        $candidates.Add($preferredUrl)
    }
    if (Is-HuggingFaceUrl $Url) {
        $mirrorUrl = Convert-HfMirrorUrl $Url
        if ($mirrorUrl -and -not $candidates.Contains($mirrorUrl)) {
            $candidates.Add($mirrorUrl)
        }
    } elseif (Is-HfMirrorUrl $Url) {
        $directUrl = Convert-HfDirectUrl $Url
        if ($directUrl -and -not $candidates.Contains($directUrl)) {
            $candidates.Add($directUrl)
        }
    }

    for ($i = 0; $i -lt $candidates.Count; $i++) {
        $candidate = $candidates[$i]
        if ($i -gt 0) {
            if (Is-HfMirrorUrl $candidate) {
                Write-Host "[提示] Hugging Face 直连失败，自动尝试 hf-mirror.com 镜像。"
            } elseif (Is-HuggingFaceUrl $candidate) {
                Write-Host "[提示] hf-mirror.com 镜像失败，自动尝试 Hugging Face 直连。"
            }
        }
        if (Test-DownloadUrlReachable $candidate $Proxy) {
            return $candidate
        }
    }

    throw "下载链接不可达：$($candidates -join ' / ')。建议在主菜单选择 12 开启 hf-mirror，或设置 Clash/LAN 代理。"
}

function Get-SafetensorsFileProblem {
    param([string]$Path)
    try {
        $item = Get-Item -LiteralPath $Path -ErrorAction Stop
        if ($item.Length -lt 16) {
            return "safetensors file is too small"
        }
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        try {
            $lenBytes = New-Object byte[] 8
            if ($fs.Read($lenBytes, 0, 8) -ne 8) {
                return "cannot read safetensors header length"
            }
            $headerLength = [BitConverter]::ToUInt64($lenBytes, 0)
            if ($headerLength -lt 2 -or $headerLength -gt 134217728) {
                return "safetensors header length is invalid"
            }
            if (([uint64]$item.Length) -lt (8 + $headerLength)) {
                return "safetensors header is incomplete"
            }
            $headerBytes = New-Object byte[] ([int]$headerLength)
            if ($fs.Read($headerBytes, 0, [int]$headerLength) -ne [int]$headerLength) {
                return "cannot read complete safetensors header"
            }
            $headerText = [System.Text.Encoding]::UTF8.GetString($headerBytes)
            $header = $headerText | ConvertFrom-Json -ErrorAction Stop
            $dataLength = ([uint64]$item.Length) - 8 - $headerLength
            $maxEnd = [uint64]0
            $tensorCount = 0
            foreach ($prop in $header.PSObject.Properties) {
                if ($prop.Name -eq "__metadata__") {
                    continue
                }
                $offsets = $prop.Value.data_offsets
                if ($null -eq $offsets -or $offsets.Count -lt 2) {
                    continue
                }
                $start = [uint64]$offsets[0]
                $end = [uint64]$offsets[1]
                if ($end -lt $start) {
                    return "safetensors tensor offset is invalid"
                }
                if ($end -gt $dataLength) {
                    return "safetensors tensor data exceeds file size"
                }
                if ($end -gt $maxEnd) {
                    $maxEnd = $end
                }
                $tensorCount += 1
            }
            if ($tensorCount -lt 1) {
                return "safetensors contains no tensor metadata"
            }
            if ($maxEnd -ne $dataLength) {
                return "safetensors data is incomplete or has trailing junk"
            }
            return ""
        } finally {
            $fs.Dispose()
        }
    } catch {
        return "safetensors header cannot be parsed: $($_.Exception.Message)"
    }
}

function Get-GgufFileProblem {
    param([string]$Path)
    try {
        $fs = [System.IO.File]::Open($Path, [System.IO.FileMode]::Open, [System.IO.FileAccess]::Read, [System.IO.FileShare]::Read)
        try {
            $magicBytes = New-Object byte[] 4
            if ($fs.Read($magicBytes, 0, 4) -ne 4) {
                return "cannot read GGUF magic"
            }
            $magic = [System.Text.Encoding]::ASCII.GetString($magicBytes)
            if ($magic -ne "GGUF") {
                return "GGUF magic is invalid"
            }
            return ""
        } finally {
            $fs.Dispose()
        }
    } catch {
        return "GGUF header cannot be read: $($_.Exception.Message)"
    }
}

function Get-ModelFileProblem {
    param(
        [string]$Path,
        [double]$MinGB = 0,
        [switch]$AllowSmall
    )
    if (-not (Test-Path -LiteralPath $Path)) {
        return "missing"
    }
    $item = Get-Item -LiteralPath $Path -ErrorAction Stop
    if ($item.PSIsContainer) {
        return ""
    }
    if ($MinGB -gt 0) {
        $minBytes = [int64]($MinGB * 1GB)
        if ($item.Length -lt $minBytes) {
            return ("file is too small: {0:N2} GB, expected at least {1:N2} GB" -f ($item.Length / 1GB), $MinGB)
        }
    } elseif ((-not $AllowSmall) -and $item.Length -lt 1MB) {
        return ("file is too small: {0:N2} MB" -f ($item.Length / 1MB))
    }
    $ext = [System.IO.Path]::GetExtension($Path).ToLowerInvariant()
    if ($ext -eq ".safetensors") {
        return Get-SafetensorsFileProblem $Path
    }
    if ($ext -eq ".gguf") {
        return Get-GgufFileProblem $Path
    }
    return ""
}

function Download-File {
    param(
        [string]$Url,
        [string]$Destination,
        [double]$MinGB = 0,
        [switch]$AllowSmall
    )
    if (Test-Path -LiteralPath $Destination) {
        $problem = Get-ModelFileProblem $Destination $MinGB -AllowSmall:$AllowSmall
        if (-not $problem) {
            Write-Host "[跳过] 文件已存在且校验通过：$Destination"
            return
        }
        $backup = "$Destination.bad-$(Get-Date -Format yyyyMMddHHmmss)"
        Write-Host "[损坏] 现有文件无效：$Destination"
        Write-Host "       原因：$problem"
        Write-Host "       移走：$backup"
        Move-Item -LiteralPath $Destination -Destination $backup -Force
    }
    New-Dir (Split-Path -Parent $Destination)
    $proxy = Resolve-DownloadProxy
    $finalUrl = Resolve-ReachableDownloadUrl $Url $proxy
    Write-Host "[下载] $finalUrl"
    Write-Host "           -> $Destination"
    if ($proxy) {
        Write-Host "           代理：$proxy"
    }

    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        $curlArgs = @("-L", "--fail", "-C", "-", "--retry", "5", "--retry-delay", "5")
        if ($proxy) {
            $curlArgs += @("--proxy", $proxy)
        }
        $curlArgs += @("-o", $Destination, $finalUrl)
        & curl.exe @curlArgs
        if ($LASTEXITCODE -ne 0) {
            throw "curl 下载失败，退出码 $LASTEXITCODE"
        }
    } else {
        if ($proxy) {
            Invoke-WebRequest -Uri $finalUrl -OutFile $Destination -Proxy $proxy
        } else {
            Invoke-WebRequest -Uri $finalUrl -OutFile $Destination
        }
    }

    $problemAfterDownload = Get-ModelFileProblem $Destination $MinGB -AllowSmall:$AllowSmall
    if ($problemAfterDownload) {
        throw "下载后的文件仍然无效：$Destination；$problemAfterDownload"
    }
}

function Download-TextFile {
    param(
        [string]$Url,
        [string]$Destination
    )
    New-Dir (Split-Path -Parent $Destination)
    $proxy = Resolve-DownloadProxy
    $finalUrl = Resolve-ReachableDownloadUrl $Url $proxy
    Write-Host "[下载] $finalUrl"
    Write-Host "           -> $Destination"
    if ($proxy) {
        Write-Host "           代理：$proxy"
    }

    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        $curlArgs = @("-L", "--fail", "--retry", "5", "--retry-delay", "3")
        if ($proxy) {
            $curlArgs += @("--proxy", $proxy)
        }
        $curlArgs += @("-o", $Destination, $finalUrl)
        & curl.exe @curlArgs
        if ($LASTEXITCODE -ne 0) {
            throw "curl 下载失败，退出码 $LASTEXITCODE"
        }
        return
    }

    if ($proxy) {
        Invoke-WebRequest -Uri $finalUrl -OutFile $Destination -Proxy $proxy
    } else {
        Invoke-WebRequest -Uri $finalUrl -OutFile $Destination
    }
}

function Get-GitHubRepoParts {
    param([string]$RepoUrl)
    $value = (str $RepoUrl).Trim().TrimEnd("/")
    $value = $value -replace '\.git$', ''
    if ($value -match '^https://github\.com/([^/]+)/([^/]+)$') {
        return @{
            Owner = $Matches[1]
            Repo = $Matches[2]
        }
    }
    return $null
}

function Invoke-GitHubApiJson {
    param([string]$Url)
    $proxy = Resolve-DownloadProxy
    $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($curl) {
        $tmp = [System.IO.Path]::GetTempFileName()
        try {
            $curlArgs = @("-L", "--fail", "--silent", "--show-error", "--max-time", "25", "-H", "User-Agent: AstrBot-ComfyUI-Setup")
            if ($proxy) {
                $curlArgs += @("--proxy", $proxy)
            }
            $curlArgs += @("-o", $tmp, $Url)
            & curl.exe @curlArgs
            if ($LASTEXITCODE -ne 0) {
                return $null
            }
            $text = Get-Content -LiteralPath $tmp -Raw -ErrorAction Stop
            if (-not $text) {
                return $null
            }
            return ($text | ConvertFrom-Json)
        } catch {
            return $null
        } finally {
            if (Test-Path -LiteralPath $tmp) {
                Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue
            }
        }
    }

    try {
        $args = @{
            Uri = $Url
            Headers = @{ "User-Agent" = "AstrBot-ComfyUI-Setup" }
            TimeoutSec = 25
            UseBasicParsing = $true
        }
        if ($proxy) {
            $args.Proxy = $proxy
        }
        return Invoke-RestMethod @args
    } catch {
        return $null
    }
}

function Resolve-GitHubArchiveUrl {
    param([string]$RepoUrl)
    $parts = Get-GitHubRepoParts $RepoUrl
    if (-not $parts) {
        return ""
    }

    $apiUrl = "https://api.github.com/repos/$($parts.Owner)/$($parts.Repo)"
    $repoInfo = Invoke-GitHubApiJson $apiUrl
    $branches = New-Object System.Collections.Generic.List[string]
    if ($repoInfo -and $repoInfo.default_branch) {
        $branches.Add([string]$repoInfo.default_branch)
    }
    foreach ($branch in @("main", "master")) {
        if (-not $branches.Contains($branch)) {
            $branches.Add($branch)
        }
    }

    $proxy = Resolve-DownloadProxy
    foreach ($branch in $branches) {
        $archiveUrl = "https://github.com/$($parts.Owner)/$($parts.Repo)/archive/refs/heads/$branch.zip"
        if (Test-DownloadUrlReachable $archiveUrl $proxy) {
            return $archiveUrl
        }
    }
    return ""
}

function Install-NodeRepoFromArchive {
    param(
        [string]$CustomNodesDir,
        [string]$RepoUrl,
        [string]$FolderName
    )
    $target = Join-Path $CustomNodesDir $FolderName
    $archiveUrl = Resolve-GitHubArchiveUrl $RepoUrl
    if (-not $archiveUrl) {
        throw "找不到可访问的 GitHub zip 包：$RepoUrl"
    }

    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("astrbot_node_" + [System.Guid]::NewGuid().ToString("N"))
    New-Dir $tempRoot
    $zipPath = Join-Path $tempRoot "$FolderName.zip"
    try {
        Write-Host "[zip 安装] $RepoUrl"
        Download-File $archiveUrl $zipPath -AllowSmall
        Expand-Archive -LiteralPath $zipPath -DestinationPath $tempRoot -Force
        $source = Get-ChildItem -LiteralPath $tempRoot -Directory -ErrorAction Stop | Select-Object -First 1
        if (-not $source) {
            throw "zip 包里没有源码目录：$archiveUrl"
        }
        if (-not $source.FullName.StartsWith($tempRoot, [System.StringComparison]::OrdinalIgnoreCase)) {
            throw "解压路径不安全：$($source.FullName)"
        }
        New-Dir $CustomNodesDir
        Move-Item -LiteralPath $source.FullName -Destination $target
        Write-Host "[完成] 已安装自定义节点：$FolderName"
    } finally {
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Install-NodeRepoWithGit {
    param(
        [string]$CustomNodesDir,
        [string]$RepoUrl,
        [string]$FolderName
    )
    $target = Join-Path $CustomNodesDir $FolderName
    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("astrbot_git_" + [System.Guid]::NewGuid().ToString("N"))
    New-Dir $tempRoot
    $tempTarget = Join-Path $tempRoot $FolderName
    try {
        $proxy = Resolve-DownloadProxy
        Write-Host "[git 克隆] $RepoUrl"
        if ($proxy) {
            Write-Host "            代理：$proxy"
            & git -c "http.proxy=$proxy" -c "https.proxy=$proxy" clone $RepoUrl $tempTarget
        } else {
            & git clone $RepoUrl $tempTarget
        }
        if ($LASTEXITCODE -ne 0) {
            Write-Warning "git 克隆失败，改用 GitHub zip 包：$RepoUrl"
            return $false
        }
        New-Dir $CustomNodesDir
        Move-Item -LiteralPath $tempTarget -Destination $target
        return $true
    } finally {
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Install-NodeRepo {
    param(
        [string]$CustomNodesDir,
        [string]$RepoUrl,
        [string]$FolderName
    )
    $target = Join-Path $CustomNodesDir $FolderName
    if (Test-Path -LiteralPath $target) {
        Write-Host "[跳过] 自定义节点已存在：$FolderName"
        return
    }
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        if (Install-NodeRepoWithGit $CustomNodesDir $RepoUrl $FolderName) {
            return
        }
    } else {
        Write-Warning "未找到 git，改用 GitHub zip 包：$RepoUrl"
    }
    Install-NodeRepoFromArchive $CustomNodesDir $RepoUrl $FolderName
}

function Add-PythonCandidate {
    param(
        [System.Collections.Generic.List[string]]$Candidates,
        [string]$Path
    )
    $value = Normalize-InputPath (str $Path)
    if ($value) {
        $Candidates.Add($value) | Out-Null
    }
}

function Add-RunningComfyPythonCandidates {
    param([System.Collections.Generic.List[string]]$Candidates)
    $roots = @(
        (Normalize-InputPath (str $script:Root)),
        (Normalize-InputPath (str (Split-Path -Parent $script:Root)))
    ) | Where-Object { $_ }
    try {
        $processes = Get-CimInstance Win32_Process -ErrorAction Stop | Where-Object {
            $_.Name -in @("python.exe", "pythonw.exe") -and $_.ExecutablePath
        }
        foreach ($proc in $processes) {
            $cmd = str $proc.CommandLine
            $exe = str $proc.ExecutablePath
            $matched = $false
            foreach ($root in $roots) {
                if (
                    ($cmd -and $cmd.IndexOf($root, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) -or
                    ($exe -and $exe.IndexOf($root, [System.StringComparison]::OrdinalIgnoreCase) -ge 0)
                ) {
                    $matched = $true
                    break
                }
            }
            if (-not $matched -and $cmd -and $cmd.IndexOf("ComfyUI", [System.StringComparison]::OrdinalIgnoreCase) -ge 0) {
                $matched = $true
            }
            if ($matched) {
                Add-PythonCandidate $Candidates $exe
            }
        }
    } catch {
    }
}

function Add-CondaEnvPythonCandidates {
    param([System.Collections.Generic.List[string]]$Candidates)
    $roots = New-Object System.Collections.Generic.List[string]
    foreach ($root in @(
        (str $env:CONDA_PREFIX),
        (Join-Path (Split-Path -Parent $script:Root) "envs"),
        "$env:USERPROFILE\.conda\envs"
    )) {
        $value = Normalize-InputPath (str $root)
        if ($value) {
            $roots.Add($value) | Out-Null
        }
    }
    $systemPython = Get-Command python -ErrorAction SilentlyContinue
    if ($systemPython -and $systemPython.Source) {
        try {
            $base = Split-Path -Parent (Split-Path -Parent $systemPython.Source)
            Add-PythonCandidate $roots (Join-Path $base "envs")
        } catch {
        }
    }
    foreach ($envRoot in ($roots | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $envRoot)) {
            continue
        }
        try {
            $envs = Get-ChildItem -LiteralPath $envRoot -Directory -ErrorAction Stop | Sort-Object @{
                Expression = {
                    if ($_.Name -match '(?i)comfy|aki|torch|sd|stable') { 0 } else { 1 }
                }
            }, Name
            foreach ($env in $envs) {
                Add-PythonCandidate $Candidates (Join-Path $env.FullName "python.exe")
            }
        } catch {
        }
    }
}

function Get-PythonVersionText {
    param([string]$Python)
    try {
        $output = & $Python -c "import sys; print('%d.%d.%d' % (sys.version_info[0], sys.version_info[1], sys.version_info[2]))" 2>$null
        if ($LASTEXITCODE -ne 0) {
            return ""
        }
        return (str ($output | Select-Object -First 1)).Trim()
    } catch {
        return ""
    }
}

function Test-ComfyPythonCandidate {
    param([string]$Python)
    try {
        $resolved = (Resolve-Path -LiteralPath $Python -ErrorAction Stop).Path
    } catch {
        return $false
    }
    if (-not (Test-Path -LiteralPath $resolved)) {
        return $false
    }
    $version = Get-PythonVersionText $resolved
    if ($version -notmatch '^(\d+)\.(\d+)\.(\d+)') {
        Write-Host "[跳过] Python 无法读取版本：$resolved"
        return $false
    }
    $major = [int]$Matches[1]
    $minor = [int]$Matches[2]
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 10)) {
        Write-Host "[跳过] Python 版本过旧：$resolved ($version)，ComfyUI 依赖需要 Python 3.10+"
        return $false
    }
    & $resolved -m pip --version *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[跳过] Python 缺少 pip：$resolved ($version)"
        return $false
    }
    $script:ResolvedComfyPythonVersion = $version
    return $true
}

function Resolve-ComfyPython {
    $script:ResolvedComfyPythonVersion = ""
    $candidates = New-Object System.Collections.Generic.List[string]
    Add-PythonCandidate $candidates (str $env:COMFYUI_PYTHON)
    Add-RunningComfyPythonCandidates $candidates
    foreach ($candidate in @(
        (Join-Path $script:Root "..\python_embeded\python.exe"),
        (Join-Path $script:Root "..\python\python.exe"),
        (Join-Path $script:Root "..\python310\python.exe"),
        (Join-Path $script:Root "..\python311\python.exe"),
        (Join-Path $script:Root "..\python312\python.exe"),
        (Join-Path $script:Root "python_embeded\python.exe"),
        (Join-Path $script:Root "python\python.exe"),
        (Join-Path $script:Root "venv\Scripts\python.exe"),
        (Join-Path $script:Root ".venv\Scripts\python.exe"),
        (Join-Path (Split-Path -Parent $script:Root) "python_embeded\python.exe"),
        (Join-Path (Split-Path -Parent $script:Root) "python\python.exe"),
        (Join-Path (Split-Path -Parent $script:Root) "python310\python.exe"),
        (Join-Path (Split-Path -Parent $script:Root) "python311\python.exe"),
        (Join-Path (Split-Path -Parent $script:Root) "python312\python.exe"),
        (Join-Path (Split-Path -Parent $script:Root) "venv\Scripts\python.exe"),
        (Join-Path (Split-Path -Parent $script:Root) ".venv\Scripts\python.exe")
    )) {
        Add-PythonCandidate $candidates $candidate
    }
    Add-CondaEnvPythonCandidates $candidates
    $systemPython = Get-Command python -ErrorAction SilentlyContinue
    if ($systemPython) {
        Add-PythonCandidate $candidates $systemPython.Source
    }
    foreach ($candidate in ($candidates | Select-Object -Unique)) {
        if (Test-ComfyPythonCandidate $candidate) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return ""
}

function Add-GitToPath {
    param([string]$GitExe)
    if (-not $GitExe) {
        return
    }
    try {
        $gitPath = (Resolve-Path -LiteralPath $GitExe -ErrorAction Stop).Path
    } catch {
        return
    }
    $gitDir = Split-Path -Parent $gitPath
    if (-not $gitDir) {
        return
    }
    $pathParts = @($env:PATH -split ';' | Where-Object { $_ })
    if (-not ($pathParts | Where-Object { $_ -ieq $gitDir })) {
        $env:PATH = "$gitDir;$env:PATH"
    }
}

function Resolve-GitCommand {
    foreach ($candidate in @(
        (str $env:GIT_EXE),
        (Join-Path $script:Root "tools\PortableGit\cmd\git.exe"),
        (Join-Path (Split-Path -Parent $script:Root) "tools\PortableGit\cmd\git.exe"),
        "$env:ProgramFiles\Git\cmd\git.exe",
        "${env:ProgramFiles(x86)}\Git\cmd\git.exe",
        "$env:LocalAppData\Programs\Git\cmd\git.exe"
    )) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            Add-GitToPath $candidate
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    $git = Get-Command git -ErrorAction SilentlyContinue
    if ($git) {
        Add-GitToPath $git.Source
        return $git.Source
    }
    return ""
}

function Install-PortableGit {
    $target = Join-Path $script:Root "tools\PortableGit"
    $gitExe = Join-Path $target "cmd\git.exe"
    if (Test-Path -LiteralPath $gitExe) {
        Add-GitToPath $gitExe
        return $gitExe
    }

    Write-Host "[Git] 未找到 git，自动下载 PortableGit 到 ComfyUI\tools\PortableGit"
    $release = Invoke-GitHubApiJson "https://api.github.com/repos/git-for-windows/git/releases/latest"
    $assetUrl = ""
    if ($release -and $release.assets) {
        foreach ($asset in $release.assets) {
            $name = str $asset.name
            if ($name -match '^PortableGit-.*-64-bit\.7z\.exe$' -and $asset.browser_download_url) {
                $assetUrl = str $asset.browser_download_url
                break
            }
        }
    }
    if (-not $assetUrl) {
        throw "无法从 Git for Windows 最新版本解析 PortableGit 下载地址。请检查 GitHub 网络或代理设置。"
    }

    $tempRoot = Join-Path ([System.IO.Path]::GetTempPath()) ("astrbot_portable_git_" + [System.Guid]::NewGuid().ToString("N"))
    New-Dir $tempRoot
    $archive = Join-Path $tempRoot "PortableGit.7z.exe"
    try {
        Download-File $assetUrl $archive -MinGB 0.03
        New-Dir $target
        Write-Host "[Git] 解压 PortableGit..."
        & $archive -y "-o$target" | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "PortableGit 解压失败，退出码 $LASTEXITCODE"
        }
        if (-not (Test-Path -LiteralPath $gitExe)) {
            throw "PortableGit 解压后未找到 cmd\git.exe"
        }
        Add-GitToPath $gitExe
        Write-Host "[Git] 已启用：$gitExe"
        return $gitExe
    } finally {
        if (Test-Path -LiteralPath $tempRoot) {
            Remove-Item -LiteralPath $tempRoot -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Ensure-GitForRequirements {
    param([string]$RequirementsPath)
    if (-not (Test-Path -LiteralPath $RequirementsPath)) {
        return ""
    }
    $text = Get-Content -LiteralPath $RequirementsPath -Raw -ErrorAction SilentlyContinue
    if ($text -notmatch '(?im)^\s*git\+') {
        return Resolve-GitCommand
    }
    $git = Resolve-GitCommand
    if ($git) {
        Write-Host "[Git] 已找到：$git"
        return $git
    }
    try {
        return Install-PortableGit
    } catch {
        Write-Warning "PortableGit auto install failed; using zip fallback for known git+ requirements: $($_.Exception.Message)"
        return ""
    }
}

function New-GitlessRequirementsFile {
    param([string]$RequirementsPath)
    $text = Get-Content -LiteralPath $RequirementsPath -Raw -ErrorAction SilentlyContinue
    if ($text -notmatch '(?im)^\s*git\+') {
        return $RequirementsPath
    }
    $updated = $text -replace '(?im)^\s*git\+https://github\.com/facebookresearch/sam2(?:\.git)?(?:@[^\s#]+)?(?:#.*)?\s*$', 'https://github.com/facebookresearch/sam2/archive/refs/heads/main.zip'
    if ($updated -eq $text) {
        return $RequirementsPath
    }
    $tempReq = Join-Path ([System.IO.Path]::GetTempPath()) ("astrbot_requirements_" + [System.Guid]::NewGuid().ToString("N") + ".txt")
    Set-Content -LiteralPath $tempReq -Value $updated -Encoding UTF8
    Write-Host "[pip] git is unavailable; generated zip fallback requirements: $tempReq"
    return $tempReq
}

function Install-NodeRequirements {
    param([string]$FolderName)
    $target = Join-Path $script:CustomNodes $FolderName
    $requirements = Join-Path $target "requirements.txt"
    if (-not (Test-Path -LiteralPath $requirements)) {
        Write-Host "[跳过] $FolderName 没有 requirements.txt"
        return
    }
    $python = Resolve-ComfyPython
    if (-not $python) {
        throw "找不到可用的 ComfyUI Python 3.10+，无法安装 $FolderName 的 Python 依赖。脚本已跳过系统里的老 Python/Anaconda base。请设置环境变量 COMFYUI_PYTHON 指向服务器真实运行 ComfyUI 的 python.exe。"
    }
    Write-Host "[pip] 安装 $FolderName requirements.txt"
    Write-Host "      Python: $python  $script:ResolvedComfyPythonVersion"
    $git = Ensure-GitForRequirements $requirements
    if ($git) {
        Write-Host "      Git: $git"
    }
    $installRequirements = $requirements
    if (-not $git) {
        $installRequirements = New-GitlessRequirementsFile $requirements
    }
    & $python -m pip install -r $installRequirements
    if ($LASTEXITCODE -ne 0) {
        throw "$FolderName Python 依赖安装失败，退出码 $LASTEXITCODE"
    }
}

function Install-SubjectAuditRequirements {
    $python = Resolve-ComfyPython
    if (-not $python) {
        throw "Cannot find ComfyUI Python 3.10+ for Illustrious subject audit dependency ultralytics."
    }
    Write-Host "[pip] Install Illustrious subject audit dependency: ultralytics"
    Write-Host "      Python: $python  $script:ResolvedComfyPythonVersion"
    & $python -m pip install ultralytics
    if ($LASTEXITCODE -ne 0) {
        throw "Illustrious subject audit dependency install failed, exit code $LASTEXITCODE"
    }
}

function New-BridgeToken {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $rng.GetBytes($bytes)
    } finally {
        $rng.Dispose()
    }
    return [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
}

function Resolve-BridgeProxy {
    $explicit = Normalize-InputPath (str $script:BridgeProxy)
    if ($explicit) {
        return Normalize-HttpProxy $explicit
    }
    if (-not $script:UseClashLan) {
        return ""
    }
    $hostValue = Normalize-InputPath (str $script:ClashHost)
    if (-not $hostValue) {
        $hostValue = "127.0.0.1"
    }
    $portValue = [int]$script:ClashPort
    if ($portValue -le 0) {
        $portValue = 7890
    }
    return Normalize-HttpProxy ("http://{0}:{1}" -f $hostValue, $portValue)
}

function Get-BridgeConfigObject {
    param([string]$ConfigPath)
    $config = $null
    if (Test-Path -LiteralPath $ConfigPath) {
        try {
            $config = Get-Content -LiteralPath $ConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
        } catch {
            $config = $null
        }
    }
    if ($null -eq $config) {
        $config = [ordered]@{
            token = ""
            allowed_hosts = @("civitai.com", "civitai.red", "civitaired.com", "huggingface.co", "hf-mirror.com", "github.com", "raw.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com", "release-assets.githubusercontent.com")
            chunk_size_mb = 1
            connect_timeout = 30
            sock_read_timeout = 120
            fetch_timeout = 45
            max_concurrent_downloads = 1
            proxy_cooldown = 60
            proxy = ""
        }
    }
    if ($config -is [System.Collections.IDictionary]) {
        $config = [pscustomobject]$config
    }
    $defaults = [ordered]@{
        token = ""
        allowed_hosts = @("civitai.com", "civitai.red", "civitaired.com", "huggingface.co", "hf-mirror.com", "github.com", "raw.githubusercontent.com", "objects.githubusercontent.com", "github-releases.githubusercontent.com", "release-assets.githubusercontent.com")
        chunk_size_mb = 1
        connect_timeout = 30
        sock_read_timeout = 120
        fetch_timeout = 45
        max_concurrent_downloads = 1
        proxy_cooldown = 60
        proxy = ""
    }
    foreach ($key in $defaults.Keys) {
        if ($null -eq $config.PSObject.Properties[$key]) {
            $config | Add-Member -NotePropertyName $key -NotePropertyValue $defaults[$key]
        }
    }
    return $config
}

function Write-EmbeddedLoraBridge {
    param([string]$Target)
    New-Dir $Target
    $init = @'
import asyncio
import ipaddress
import json
import os
import re
import secrets
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web

try:
    import folder_paths
    from server import PromptServer
except Exception:
    folder_paths = None
    PromptServer = None


BRIDGE_VERSION = "1.4.1"
CONFIG_PATH = Path(__file__).with_name("config.json")
DEFAULT_ALLOWED_HOSTS = [
    "civitai.com",
    "civitai.red",
    "civitaired.com",
    "huggingface.co",
    "hf-mirror.com",
    "github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "github-releases.githubusercontent.com",
    "release-assets.githubusercontent.com",
]
ALLOWED_SUFFIXES = {".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".onnx", ".gguf", ".zip"}
SAFE_HEADER_NAMES = {"authorization", "cookie", "referer", "user-agent"}
MODEL_TARGET_ALIASES = {
    "loras": ("loras", "lora"),
    "checkpoints": ("checkpoints", "checkpoint"),
    "controlnet": ("controlnet", "control_net"),
    "vae": ("vae",),
    "clip": ("clip",),
    "unet": ("unet",),
    "upscale_models": ("upscale_models", "upscale_model"),
    "embeddings": ("embeddings", "embedding"),
    "yolo": ("yolo",),
    "ultralytics_bbox": ("ultralytics/bbox",),
    "ultralytics_segm": ("ultralytics/segm",),
}
TARGET_TYPE_ALIASES = {
    "lora": "loras",
    "loras": "loras",
    "checkpoint": "checkpoints",
    "checkpoints": "checkpoints",
    "ckpt": "checkpoints",
    "controlnet": "controlnet",
    "control_net": "controlnet",
    "vae": "vae",
    "clip": "clip",
    "unet": "unet",
    "upscale": "upscale_models",
    "upscale_model": "upscale_models",
    "upscale_models": "upscale_models",
    "embedding": "embeddings",
    "embeddings": "embeddings",
    "yolo": "yolo",
    "ultralytics_bbox": "ultralytics_bbox",
    "ultralytics-bbox": "ultralytics_bbox",
    "yolo_bbox": "ultralytics_bbox",
    "bbox": "ultralytics_bbox",
    "foot_detector": "ultralytics_bbox",
    "ultralytics_segm": "ultralytics_segm",
    "ultralytics-segm": "ultralytics_segm",
    "yolo_segm": "ultralytics_segm",
    "segm": "ultralytics_segm",
    "person_detector": "ultralytics_segm",
    "controlnet_aux": "controlnet_aux_ckpts",
    "controlnet_aux_ckpts": "controlnet_aux_ckpts",
    "custom_nodes_ckpts": "controlnet_aux_ckpts",
}
CUSTOM_TARGET_RELATIVE_ROOTS = {
    "controlnet_aux_ckpts": ("custom_nodes", "comfyui_controlnet_aux", "ckpts"),
}
TASKS = {}
YOLO_MODEL_CACHE = {}
_DOWNLOAD_SEMAPHORE = None
_DOWNLOAD_SEMAPHORE_SIZE = 0
_PROXY_CIRCUITS = {}


def _safe_filename(value: str) -> str:
    name = str(value or "").strip() or f"astrbot_lora_{int(time.time())}.safetensors"
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if not name:
        name = f"astrbot_lora_{int(time.time())}.safetensors"
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        name = f"{Path(name).stem or 'astrbot_lora'}.safetensors"
    return name[:180]


IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif", ".heic", ".avif"}


def _safe_image_filename(value: str) -> str:
    name = str(value or "").strip()
    name = name.replace("\\", "/").split("/")[-1]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', "_", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    suffix = Path(name).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        name = f"{Path(name).stem or 'subject_audit'}.png"
    if not Path(name).stem:
        name = f"subject_audit{Path(name).suffix or '.png'}"
    return name[:180]


def _load_config() -> dict:
    default = {
        "token": secrets.token_urlsafe(32),
        "allowed_hosts": list(DEFAULT_ALLOWED_HOSTS),
        "chunk_size_mb": 1,
        "connect_timeout": 30,
        "sock_read_timeout": 120,
        "fetch_timeout": 45,
        "max_concurrent_downloads": 1,
        "proxy_cooldown": 60,
        "proxy": "",
        "allow_local_requests_without_token": True,
    }
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                merged = dict(default)
                merged.update(data)
                return merged
        except Exception:
            pass
    try:
        CONFIG_PATH.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass
    return default


CONFIG = _load_config()


def _json_error(message: str, status: int = 400):
    return web.json_response({"ok": False, "error": str(message or "error")}, status=status)


def _download_semaphore():
    global _DOWNLOAD_SEMAPHORE, _DOWNLOAD_SEMAPHORE_SIZE
    size = max(int(CONFIG.get("max_concurrent_downloads") or 1), 1)
    if _DOWNLOAD_SEMAPHORE is None or _DOWNLOAD_SEMAPHORE_SIZE != size:
        _DOWNLOAD_SEMAPHORE = asyncio.Semaphore(size)
        _DOWNLOAD_SEMAPHORE_SIZE = size
    return _DOWNLOAD_SEMAPHORE


def _request_token(request, payload: dict = None) -> str:
    payload = payload if isinstance(payload, dict) else {}
    return (
        request.headers.get("X-AstrBot-Bridge-Token")
        or request.query.get("token")
        or str(payload.get("token") or "")
    ).strip()


def _request_remote_host(request) -> str:
    remote = str(getattr(request, "remote", "") or "").strip()
    if not remote:
        try:
            peer = request.transport.get_extra_info("peername") if request.transport else None
            if isinstance(peer, (tuple, list)) and peer:
                remote = str(peer[0] or "").strip()
        except Exception:
            remote = ""
    return remote.strip("[]")


def _request_is_local_or_private(request) -> bool:
    if not bool(CONFIG.get("allow_local_requests_without_token", True)):
        return False
    host = _request_remote_host(request).split("%", 1)[0]
    if host.lower() in {"localhost", "localhost.localdomain"}:
        return True
    try:
        ip = ipaddress.ip_address(host)
    except Exception:
        return False
    return bool(ip.is_loopback or ip.is_private or ip.is_link_local)


def _check_auth(request, payload: dict = None) -> bool:
    expected = str(CONFIG.get("token") or "").strip()
    if not expected:
        return True
    if _request_is_local_or_private(request):
        return True
    provided = _request_token(request, payload=payload)
    return bool(provided and secrets.compare_digest(provided, expected))


def _comfy_root() -> Path:
    try:
        return Path(__file__).resolve().parents[2]
    except Exception:
        return Path.cwd()


def _models_root() -> Path:
    if folder_paths is not None:
        try:
            value = str(getattr(folder_paths, "models_dir", "") or "").strip()
            if value:
                return Path(value).expanduser()
        except Exception:
            pass
    return _comfy_root() / "models"


def _lora_dir() -> Path:
    folders = []
    if folder_paths is not None:
        try:
            folders = list(folder_paths.get_folder_paths("loras") or [])
        except Exception:
            folders = []
        if not folders:
            try:
                folders = [str(Path(folder_paths.models_dir) / "loras")]
            except Exception:
                folders = []
    if not folders:
        folders = [str(Path(__file__).resolve().parents[2] / "models" / "loras")]
    target = Path(str(folders[0])).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _model_target_dir(target_type: str) -> Path:
    target_type = TARGET_TYPE_ALIASES.get(str(target_type or "").strip().lower(), target_type)
    folder_names = MODEL_TARGET_ALIASES.get(target_type)
    if not folder_names:
        raise RuntimeError(f"unsupported download target type: {target_type}")
    if target_type == "loras":
        return _lora_dir()
    folders = []
    if folder_paths is not None:
        for name in folder_names:
            try:
                folders.extend(str(x) for x in (folder_paths.get_folder_paths(name) or []) if str(x or "").strip())
            except Exception:
                continue
    if not folders:
        folders = [str(_models_root() / folder_names[0])]
    target = Path(str(folders[0])).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return target


def _custom_target_dir(target_type: str) -> Path:
    parts = CUSTOM_TARGET_RELATIVE_ROOTS.get(target_type)
    if not parts:
        raise RuntimeError(f"unsupported download target type: {target_type}")
    target = _comfy_root().joinpath(*parts)
    target.mkdir(parents=True, exist_ok=True)
    return target


def _safe_relative_path(value: str) -> Path:
    text = str(value or "").strip().replace("\\", "/").strip("/")
    if not text:
        return Path()
    parts = [part for part in text.split("/") if part]
    if any(part in {".", ".."} or ":" in part or "\x00" in part for part in parts):
        raise RuntimeError("unsafe target subdir")
    if len(parts) > 12 or sum(len(part) for part in parts) > 240:
        raise RuntimeError("target subdir is too long")
    return Path(*parts)


def _resolve_download_target(payload: dict, filename: str) -> tuple:
    raw_target = (
        payload.get("target_type")
        or payload.get("target_folder")
        or payload.get("folder")
        or "loras"
    )
    target_type = TARGET_TYPE_ALIASES.get(str(raw_target or "").strip().lower(), str(raw_target or "").strip().lower())
    if target_type in MODEL_TARGET_ALIASES:
        root = _model_target_dir(target_type)
    elif target_type in CUSTOM_TARGET_RELATIVE_ROOTS:
        root = _custom_target_dir(target_type)
    else:
        raise RuntimeError(f"unsupported download target type: {raw_target}")
    root = root.resolve()
    subdir = _safe_relative_path(payload.get("target_subdir") or payload.get("subdir") or "")
    target_dir = (root / subdir).resolve()
    try:
        target_dir.relative_to(root)
    except Exception:
        raise RuntimeError("unsafe target path")
    target_dir.mkdir(parents=True, exist_ok=True)
    target = (target_dir / filename).resolve()
    try:
        target.relative_to(root)
    except Exception:
        raise RuntimeError("unsafe target path")
    return target, root, target_type, str(subdir).replace("\\", "/")


def _host_allowed(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return False
    host = (parsed.hostname or "").strip().lower()
    allowed = CONFIG.get("allowed_hosts") or DEFAULT_ALLOWED_HOSTS
    for item in allowed:
        item = str(item or "").strip().lower()
        if item and (host == item or host.endswith(f".{item}")):
            return True
    return False


def _safe_proxy(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    if not re.match(r"^[A-Za-z][A-Za-z0-9+.-]*://", value):
        value = f"http://{value}"
    parsed = urlparse(value)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return value
    return ""


def _safe_int(value, default: int, minimum: int = 0, maximum: int = None) -> int:
    try:
        number = int(float(value))
    except Exception:
        number = int(default)
    number = max(number, int(minimum))
    if maximum is not None:
        number = min(number, int(maximum))
    return number


def _proxy_key(proxy: str) -> str:
    return _safe_proxy(proxy)


def _proxy_circuit_message(proxy: str) -> str:
    key = _proxy_key(proxy)
    if not key:
        return ""
    state = _PROXY_CIRCUITS.get(key)
    if not isinstance(state, dict):
        return ""
    blocked_until = float(state.get("blocked_until") or 0)
    if blocked_until <= time.time():
        _PROXY_CIRCUITS.pop(key, None)
        return ""
    remain = max(1, int(blocked_until - time.time()))
    return f"proxy {key} is temporarily blocked for about {remain}s: {state.get('reason') or 'network failure'}"


def _check_proxy_circuit(proxy: str):
    message = _proxy_circuit_message(proxy)
    if message:
        raise RuntimeError(message)


def _mark_proxy_failure(proxy: str, exc: Exception):
    key = _proxy_key(proxy)
    if not key:
        return
    cooldown = max(int(CONFIG.get("proxy_cooldown") or 60), 5)
    _PROXY_CIRCUITS[key] = {
        "blocked_until": time.time() + cooldown,
        "reason": str(exc),
        "updated_at": time.time(),
    }


def _mark_proxy_success(proxy: str):
    key = _proxy_key(proxy)
    if key:
        _PROXY_CIRCUITS.pop(key, None)


def _looks_like_proxy_failure(exc: Exception) -> bool:
    return isinstance(exc, (aiohttp.ClientError, TimeoutError, OSError))


def _download_headers(payload: dict) -> dict:
    raw = payload.get("headers") if isinstance(payload, dict) else {}
    if not isinstance(raw, dict):
        return {}
    headers = {}
    for key, value in raw.items():
        name = str(key or "").strip()
        if name.lower() in SAFE_HEADER_NAMES and value:
            headers[name] = str(value)
    if "User-Agent" not in headers and "user-agent" not in {x.lower() for x in headers}:
        headers["User-Agent"] = "AstrBot-ComfyUI-LoRA-Bridge/1.0"
    return headers


def _safe_response_headers(headers) -> dict:
    names = {"content-type", "content-length", "retry-after", "x-ratelimit-remaining", "x-ratelimit-reset"}
    result = {}
    for key, value in (headers or {}).items():
        if str(key or "").lower() in names:
            result[str(key)] = str(value)
    return result


def _task_snapshot(task_id: str) -> dict:
    task = TASKS.get(str(task_id or ""))
    if not isinstance(task, dict):
        return {}
    public = {
        "ok": True,
        "task_id": task.get("task_id"),
        "status": task.get("status"),
        "filename": task.get("filename"),
        "target_type": task.get("target_type") or "loras",
        "target_subdir": task.get("target_subdir") or "",
        "done": int(task.get("done") or 0),
        "total": int(task.get("total") or 0),
        "error": task.get("error") or "",
        "updated_at": task.get("updated_at") or time.time(),
    }
    if task.get("status") == "done":
        public["path"] = task.get("path") or ""
    return public


def _cleanup_tasks():
    now = time.time()
    for task_id, task in list(TASKS.items()):
        if not isinstance(task, dict):
            TASKS.pop(task_id, None)
            continue
        if task.get("status") in {"queued", "running"}:
            continue
        if now - float(task.get("updated_at") or now) > 3600:
            TASKS.pop(task_id, None)


async def _download_worker(task_id: str, url: str, target: Path, headers: dict, proxy: str = ""):
    task = TASKS[task_id]
    tmp_path = target.with_suffix(target.suffix + ".part")
    try:
        async with _download_semaphore():
            task.update({"status": "running", "updated_at": time.time()})
            timeout = aiohttp.ClientTimeout(
                total=None,
                connect=max(int(CONFIG.get("connect_timeout") or 30), 3),
                sock_read=max(int(CONFIG.get("sock_read_timeout") or 120), 30),
            )
            chunk_size = max(int(CONFIG.get("chunk_size_mb") or 1), 1) * 1024 * 1024
            proxy = _safe_proxy(proxy) or _safe_proxy(CONFIG.get("proxy")) or None
            _check_proxy_circuit(proxy)
            async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
                async with session.get(url, allow_redirects=True, proxy=proxy) as resp:
                    _mark_proxy_success(proxy)
                    if resp.status >= 400:
                        text = await resp.text(errors="replace")
                        raise RuntimeError(f"download HTTP {resp.status}: {text[:200]}")
                    total = int(resp.headers.get("Content-Length") or 0)
                    task.update({"total": total, "updated_at": time.time()})
                    done = 0
                    with tmp_path.open("wb") as fp:
                        async for chunk in resp.content.iter_chunked(chunk_size):
                            if not chunk:
                                continue
                            fp.write(chunk)
                            done += len(chunk)
                            task.update({"done": done, "updated_at": time.time()})
            if not tmp_path.exists() or tmp_path.stat().st_size <= 0:
                raise RuntimeError("download produced an empty file")
            tmp_path.replace(target)
            try:
                os.utime(target, None)
                os.utime(target.parent, None)
            except Exception:
                pass
            task.update({
                "status": "done",
                "done": target.stat().st_size,
                "path": str(target),
                "updated_at": time.time(),
            })
    except Exception as exc:
        proxy = _safe_proxy(proxy) or _safe_proxy(CONFIG.get("proxy")) or None
        if proxy and _looks_like_proxy_failure(exc):
            _mark_proxy_failure(proxy, exc)
        try:
            if tmp_path.exists() and tmp_path.stat().st_size <= 0:
                tmp_path.unlink()
        except Exception:
            pass
        task.update({"status": "error", "error": str(exc), "updated_at": time.time()})


async def _fetch_url(payload: dict) -> dict:
    url = str(payload.get("url") or "").strip()
    if not _host_allowed(url):
        raise RuntimeError("fetch host is not allowed")
    method = str(payload.get("method") or "GET").upper()
    if method not in {"GET", "POST"}:
        raise RuntimeError("method must be GET or POST")
    proxy = _safe_proxy(payload.get("proxy")) or _safe_proxy(CONFIG.get("proxy")) or None
    config_fetch_timeout = max(_safe_int(CONFIG.get("fetch_timeout"), 45, minimum=10), 10)
    request_fetch_timeout = _safe_int(
        payload.get("timeout") or payload.get("fetch_timeout") or config_fetch_timeout,
        config_fetch_timeout,
        minimum=10,
        maximum=600,
    )
    timeout = aiohttp.ClientTimeout(
        total=request_fetch_timeout,
        connect=max(_safe_int(CONFIG.get("connect_timeout"), 30, minimum=3), 3),
        sock_read=request_fetch_timeout,
    )
    headers = _download_headers(payload)
    max_bytes = max(int(payload.get("max_bytes") or 4 * 1024 * 1024), 64 * 1024)
    chunks = []
    size = 0
    try:
        _check_proxy_circuit(proxy)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            request_kwargs = {"allow_redirects": True, "proxy": proxy}
            if method == "POST":
                request_kwargs["json"] = payload.get("json") if isinstance(payload.get("json"), dict) else {}
                request = session.post
            else:
                request = session.get
            async with request(url, **request_kwargs) as resp:
                _mark_proxy_success(proxy)
                async for chunk in resp.content.iter_chunked(256 * 1024):
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size <= max_bytes:
                        chunks.append(chunk)
                    else:
                        remain = max_bytes - (size - len(chunk))
                        if remain > 0:
                            chunks.append(chunk[:remain])
                        break
                raw = b"".join(chunks)
                text = raw.decode(resp.charset or "utf-8", errors="replace")
                return {
                    "ok": True,
                    "status": int(resp.status),
                    "headers": _safe_response_headers(resp.headers),
                    "text": text,
                    "truncated": size > max_bytes,
                    "bytes": min(size, max_bytes),
                }
    except Exception as exc:
        if proxy and _looks_like_proxy_failure(exc):
            _mark_proxy_failure(proxy, exc)
        target = urlparse(url).netloc or url
        proxy_text = proxy or "direct"
        raise RuntimeError(f"fetch {target} via {proxy_text} failed: {exc}") from exc


def _subject_model_path(target_type: str, model_name: str) -> Path:
    name = _safe_filename(model_name)
    if not name:
        raise RuntimeError(f"empty {target_type} model name")
    return (_model_target_dir(target_type) / name).resolve()


def _subject_audit_preflight(payload: dict) -> tuple:
    missing = []
    paths = {}
    try:
        import ultralytics  # noqa: F401
    except Exception:
        missing.append("Python package: ultralytics")

    skip_subject_models = str(payload.get("skip_subject_models") or "").lower() in {"1", "true", "yes", "on"}
    model_specs = []
    if not skip_subject_models:
        model_specs.extend([
            ("face_model", "ultralytics_bbox", "face_yolov8m.pt"),
            ("person_model", "ultralytics_segm", "person_yolov8s-seg.pt"),
        ])
    model_specs.append(("foot_model", "ultralytics_bbox", ""))
    for key, target_type, default_name in model_specs:
        model_name = str(payload.get(key) or default_name).strip()
        if not model_name:
            continue
        try:
            path = _subject_model_path(target_type, model_name)
            paths[key] = path
            if not path.exists() or path.stat().st_size <= 0:
                missing.append(f"models/{MODEL_TARGET_ALIASES[target_type][0]}/{path.name}")
        except Exception as exc:
            missing.append(f"{key}: {exc}")
    return missing, paths


def _load_yolo_model(path: Path):
    key = str(path.resolve())
    model = YOLO_MODEL_CACHE.get(key)
    if model is not None:
        return model
    from ultralytics import YOLO
    model = YOLO(key)
    YOLO_MODEL_CACHE[key] = model
    return model


def _run_yolo_count(model_path: Path, image_path: Path, threshold: float) -> tuple:
    threshold = min(max(float(threshold or 0.35), 0.01), 1.0)
    model = _load_yolo_model(model_path)
    results = model(str(image_path), verbose=False, conf=threshold)
    boxes = []
    for result in results or []:
        raw_boxes = getattr(result, "boxes", None)
        if raw_boxes is None:
            continue
        xyxy_values = getattr(raw_boxes, "xyxy", None)
        conf_values = getattr(raw_boxes, "conf", None)
        cls_values = getattr(raw_boxes, "cls", None)
        xyxy_list = xyxy_values.detach().cpu().tolist() if xyxy_values is not None else []
        conf_list = conf_values.detach().cpu().tolist() if conf_values is not None else []
        cls_list = cls_values.detach().cpu().tolist() if cls_values is not None else []
        for index, box in enumerate(xyxy_list):
            conf = float(conf_list[index]) if index < len(conf_list) else 0.0
            if conf < threshold:
                continue
            boxes.append({
                "bbox": [round(float(x), 2) for x in box[:4]],
                "confidence": round(conf, 4),
                "class": int(float(cls_list[index])) if index < len(cls_list) else 0,
            })
    return len(boxes), boxes[:12]


async def _subject_audit_payload(request) -> tuple:
    if str(request.content_type or "").lower().startswith("multipart/"):
        reader = await request.multipart()
        payload = {}
        image_bytes = b""
        image_name = "subject_audit.png"
        async for part in reader:
            if part.name == "image":
                image_name = _safe_image_filename(part.filename or image_name)
                image_bytes = await part.read(decode=False)
            else:
                payload[str(part.name or "")] = (await part.text()).strip()
        return payload, image_bytes, image_name
    payload = await request.json()
    return payload if isinstance(payload, dict) else {}, b"", "subject_audit.png"


async def _run_subject_audit_request(request):
    payload, image_bytes, image_name = await _subject_audit_payload(request)
    missing, paths = _subject_audit_preflight(payload)
    base = {
        "ok": not missing,
        "subject_audit_supported": True,
        "ready": not missing,
        "missing": missing,
        "face_model": Path(str(payload.get("face_model") or "face_yolov8m.pt")).name,
        "person_model": Path(str(payload.get("person_model") or "person_yolov8s-seg.pt")).name,
    }
    if payload.get("foot_model"):
        base["foot_model"] = Path(str(payload.get("foot_model") or "")).name
    if missing:
        base["error"] = "Illustrious single-subject audit dependency missing: " + ", ".join(missing)
        return base
    if str(payload.get("preflight_only") or "").lower() in {"1", "true", "yes", "on"}:
        return base
    if not image_bytes:
        return {**base, "ok": False, "error": "subject audit image is required"}

    temp_path = Path(tempfile.gettempdir()) / f"astrbot_subject_audit_{uuid.uuid4().hex}_{image_name}"
    try:
        temp_path.write_bytes(image_bytes)
        face_count, face_boxes = _run_yolo_count(
            paths["face_model"],
            temp_path,
            float(payload.get("face_threshold") or 0.35),
        ) if "face_model" in paths else (0, [])
        person_count, person_boxes = _run_yolo_count(
            paths["person_model"],
            temp_path,
            float(payload.get("person_threshold") or 0.35),
        ) if "person_model" in paths else (0, [])
        foot_count, foot_boxes = (0, [])
        if "foot_model" in paths:
            foot_count, foot_boxes = _run_yolo_count(
                paths["foot_model"],
                temp_path,
                float(payload.get("foot_threshold") or 0.35),
            )
        rejected = bool(face_count > 1 or person_count > 1)
        result = {
            **base,
            "face_count": face_count,
            "person_count": person_count,
            "foot_count": foot_count,
            "face_boxes": face_boxes,
            "person_boxes": person_boxes,
            "foot_boxes": foot_boxes,
            "rejected": rejected,
            "reject_reason": ", ".join(
                item for item in [
                    f"face_count={face_count}" if face_count > 1 else "",
                    f"person_count={person_count}" if person_count > 1 else "",
                ] if item
            ),
        }
        return result
    finally:
        try:
            temp_path.unlink(missing_ok=True)
        except Exception:
            pass


if PromptServer is not None:
    routes = PromptServer.instance.routes

    @routes.get("/astrbot/lora/status")
    async def astrbot_lora_bridge_status(request):
        auth_required = bool(str(CONFIG.get("token") or "").strip())
        local_auth_bypass = _request_is_local_or_private(request)
        if auth_required and not _check_auth(request):
            return web.json_response({
                "ok": True,
                "version": BRIDGE_VERSION,
                "auth_required": True,
                "authenticated": False,
                "local_auth_bypass": local_auth_bypass,
                "allow_local_requests_without_token": bool(CONFIG.get("allow_local_requests_without_token", True)),
            })
        lora_dir = _lora_dir()
        return web.json_response({
            "ok": True,
            "version": BRIDGE_VERSION,
            "auth_required": auth_required,
            "authenticated": True,
            "local_auth_bypass": local_auth_bypass,
            "allow_local_requests_without_token": bool(CONFIG.get("allow_local_requests_without_token", True)),
            "lora_dir": str(lora_dir),
            "allowed_hosts": CONFIG.get("allowed_hosts") or DEFAULT_ALLOWED_HOSTS,
            "proxy_configured": bool(str(CONFIG.get("proxy") or "").strip()),
            "request_proxy_supported": True,
            "request_fetch_supported": True,
            "request_resource_download_supported": True,
            "subject_audit_supported": True,
            "subject_audit_endpoint": "/astrbot/subject-audit",
            "download_targets": sorted(set(TARGET_TYPE_ALIASES.values())),
            "max_concurrent_downloads": max(int(CONFIG.get("max_concurrent_downloads") or 1), 1),
            "task_counts": {
                "queued": sum(1 for task in TASKS.values() if isinstance(task, dict) and task.get("status") == "queued"),
                "running": sum(1 for task in TASKS.values() if isinstance(task, dict) and task.get("status") == "running"),
                "done": sum(1 for task in TASKS.values() if isinstance(task, dict) and task.get("status") == "done"),
                "error": sum(1 for task in TASKS.values() if isinstance(task, dict) and task.get("status") == "error"),
            },
        })

    @routes.post("/astrbot/lora/fetch")
    async def astrbot_lora_bridge_fetch(request):
        try:
            payload = await request.json()
        except Exception:
            return _json_error("request body must be JSON", status=400)
        if not _check_auth(request, payload=payload):
            return _json_error("invalid AstrBot bridge token", status=401)
        try:
            return web.json_response(await _fetch_url(payload))
        except Exception as exc:
            return _json_error(str(exc), status=502)

    @routes.post("/astrbot/subject-audit")
    async def astrbot_subject_audit(request):
        try:
            if not _check_auth(request):
                return _json_error("invalid AstrBot bridge token", status=401)
            data = await _run_subject_audit_request(request)
            return web.json_response(data)
        except Exception as exc:
            return _json_error(str(exc), status=500)

    @routes.post("/astrbot/lora/download")
    async def astrbot_lora_bridge_download(request):
        _cleanup_tasks()
        try:
            payload = await request.json()
        except Exception:
            return _json_error("request body must be JSON", status=400)
        if not _check_auth(request, payload=payload):
            return _json_error("invalid AstrBot bridge token", status=401)
        url = str(payload.get("url") or "").strip()
        if not _host_allowed(url):
            return _json_error("download host is not allowed", status=400)
        proxy = str(payload.get("proxy") or "").strip()
        if proxy and not _safe_proxy(proxy):
            return _json_error("proxy must be an http(s) URL or host:port", status=400)
        try:
            filename = _safe_filename(payload.get("filename") or Path(urlparse(url).path).name)
            target, root, target_type, target_subdir = _resolve_download_target(payload, filename)
        except Exception as exc:
            return _json_error(str(exc), status=400)
        task_id = uuid.uuid4().hex
        TASKS[task_id] = {
            "task_id": task_id,
            "status": "queued",
            "filename": filename,
            "target_type": target_type,
            "target_subdir": target_subdir,
            "done": 0,
            "total": 0,
            "error": "",
            "path": str(target),
            "updated_at": time.time(),
        }
        if target.exists() and target.stat().st_size > 0:
            TASKS[task_id].update({
                "status": "done",
                "done": target.stat().st_size,
                "total": target.stat().st_size,
                "updated_at": time.time(),
            })
            return web.json_response(_task_snapshot(task_id))
        asyncio.create_task(_download_worker(task_id, url, target, _download_headers(payload), proxy=proxy))
        return web.json_response(_task_snapshot(task_id))

    @routes.get("/astrbot/lora/task/{task_id}")
    async def astrbot_lora_bridge_task(request):
        _cleanup_tasks()
        if not _check_auth(request):
            return _json_error("invalid AstrBot bridge token", status=401)
        task_id = request.match_info.get("task_id", "")
        task = _task_snapshot(task_id)
        if not task:
            return _json_error("task not found", status=404)
        return web.json_response(task)


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}

'@
    $readme = @'
# ComfyUI AstrBot Resource Bridge

This custom node package registers a small HTTP bridge inside the existing
ComfyUI server. It lets AstrBot ask the ComfyUI machine to fetch metadata and
download files directly into ComfyUI-owned model folders.

It does not add visible workflow nodes.

## Routes

- `GET /astrbot/lora/status`
- `POST /astrbot/lora/fetch`
- `POST /astrbot/lora/download`
- `GET /astrbot/lora/task/{task_id}`

`download` defaults to `models/loras` for backward compatibility. Newer AstrBot
versions can pass `target_type`, such as `loras`, `checkpoints`, `controlnet`,
or `controlnet_aux_ckpts`.

## Config

On first ComfyUI start, the bridge creates `config.json` next to this file.
Copy the generated `token` into AstrBot:

`civitai_settings.comfyui_bridge_token`

If the ComfyUI machine reaches Civitai/Hugging Face/GitHub through Clash, set the
Bridge proxy from the ComfyUI machine's network view:

- Clash on the same ComfyUI machine: `http://127.0.0.1:7890`
- Clash LAN on another machine: `http://<LAN-IP>:7890`

Plain `host:port` values are accepted and normalized to `http://host:port`.

Keep the token private if your ComfyUI address is exposed through FRP or a
reverse proxy.

'@
    Set-Content -LiteralPath (Join-Path $Target "__init__.py") -Value $init -Encoding UTF8
    Set-Content -LiteralPath (Join-Path $Target "README.md") -Value $readme -Encoding UTF8
}

function Install-LoraBridge {
    $target = Join-Path $script:CustomNodes "ComfyUI-AstrBot-LoraBridge"
    New-Dir $target

    $repoRoot = $null
    try {
        $repoRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot "..") -ErrorAction Stop).Path
    } catch {
        $repoRoot = ""
    }
    $source = if ($repoRoot) { Join-Path $repoRoot "comfyui_custom_nodes\ComfyUI-AstrBot-LoraBridge" } else { "" }

    if ($source -and (Test-Path -LiteralPath $source)) {
        Write-Host "[复制] 从本地仓库安装 AstrBot LoRA Bridge"
        Copy-Item -Path (Join-Path $source "*") -Destination $target -Recurse -Force
    } else {
        Write-Host "[内置] 未找到本地 Bridge 源码，写入内嵌版本"
        Write-EmbeddedLoraBridge $target
    }

    $configPath = Join-Path $target "config.json"
    $config = Get-BridgeConfigObject $configPath
    $tokenValue = str $script:BridgeToken
    if (-not $tokenValue) {
        try {
            $tokenValue = [string]$config.PSObject.Properties["token"].Value
        } catch {
            $tokenValue = ""
        }
    }
    if (-not $tokenValue) {
        $tokenValue = New-BridgeToken
    }
    $config.PSObject.Properties["token"].Value = $tokenValue
    $proxyValue = Resolve-BridgeProxy
    if ($proxyValue) {
        $config.PSObject.Properties["proxy"].Value = $proxyValue
    }
    $config | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath $configPath -Encoding UTF8

    Write-Host ""
    Write-Host "ComfyUI AstrBot Resource Bridge 已安装/更新："
    Write-Host "  $target"
    Write-Host ""
    Write-Host "请在 AstrBot 插件 WebUI 中设置："
    Write-Host "  civitai_settings.lora_download_mode = comfyui_bridge"
    Write-Host "  civitai_settings.comfyui_bridge_token = $tokenValue"
    Write-Host "  civitai_settings.comfyui_bridge_endpoint = /astrbot/lora"
    if ($proxyValue) {
        Write-Host "  civitai_settings.comfyui_bridge_proxy = $proxyValue"
    } else {
        Write-Host "  civitai_settings.comfyui_bridge_proxy = <可选，例如 ComfyUI 机器上的 http://127.0.0.1:7890>"
    }
    if ($config.PSObject.Properties["proxy"].Value) {
        Write-Host ""
        Write-Host "Bridge 已配置代理：$($config.PSObject.Properties["proxy"].Value)"
    }
    Write-Host ""
    Write-Host "修改 Bridge 代理后请重启 ComfyUI；代理地址是从 ComfyUI 机器视角访问的。"
    Write-Host "重启后在机器人里测试：/comfy_civitai bridge"
}

function Show-PathStatus {
    param(
        [string]$Path,
        [string]$Label = "",
        [double]$MinGB = 0
    )
    $name = if ($Label) { $Label } else { $Path }
    if (Test-Path -LiteralPath $Path) {
        $item = Get-Item -LiteralPath $Path
        if ($item.PSIsContainer) {
            Write-Host "[正常] $name"
        } else {
            $problem = Get-ModelFileProblem $Path $MinGB
            if ($problem) {
                Write-Host ("[损坏] {0} ({1:N2} GB)" -f $name, ($item.Length / 1GB))
                Write-Host "       $problem"
            } else {
                Write-Host ("[正常] {0} ({1:N2} GB)" -f $name, ($item.Length / 1GB))
            }
        }
    } else {
        Write-Host "[缺失] $name"
    }
}

function Get-FileMinGB {
    param($File)
    if ($File -is [hashtable] -and $File.ContainsKey("MinGB")) {
        return [double]$File.MinGB
    }
    return 0
}

function Show-ManualFileStatus {
    param($File)
    $isGrouped = ((str $File.Name) -like "* / *")
    if (-not $isGrouped -and (Test-Path -LiteralPath $File.Dest)) {
        Show-PathStatus $File.Dest $File.Name (Get-FileMinGB $File)
    } else {
        Write-Host "[手动] $($File.Name)"
        Write-Host "         $($File.Note)"
    }
}

function Initialize-Paths {
    param([string]$Root)
    $script:Root = $Root
    $script:CustomNodes = Join-Path $Root "custom_nodes"
    $script:Checkpoints = Join-Path $Root "models\checkpoints"
    $script:Controlnet = Join-Path $Root "models\controlnet"
    $script:Vae = Join-Path $Root "models\vae"
    $script:TextEncoders = Join-Path $Root "models\text_encoders"
    $script:Loras = Join-Path $Root "models\loras"
    $script:Unet = Join-Path $Root "models\unet"
    $script:DiffusionModels = Join-Path $Root "models\diffusion_models"
    $script:ClipVision = Join-Path $Root "models\clip_vision"
    $script:UltralyticsBbox = Join-Path $Root "models\ultralytics\bbox"
    $script:UltralyticsSegm = Join-Path $Root "models\ultralytics\segm"
    $script:ControlnetAuxCkpts = Join-Path $script:CustomNodes "comfyui_controlnet_aux\ckpts"

    foreach ($dir in @(
        $script:CustomNodes,
        $script:Checkpoints,
        $script:Controlnet,
        $script:Vae,
        $script:TextEncoders,
        $script:Loras,
        $script:Unet,
        $script:DiffusionModels,
        $script:ClipVision,
        $script:UltralyticsBbox,
        $script:UltralyticsSegm
    )) {
        New-Dir $dir
    }

    $script:CoreFiles = @(
        @{
            Name = "depth_anything_v2_vitl.pth"
            Url = "https://huggingface.co/depth-anything/Depth-Anything-V2-Large/resolve/main/depth_anything_v2_vitl.pth?download=true"
            Dest = Join-Path $script:ControlnetAuxCkpts "depth-anything\Depth-Anything-V2-Large\depth_anything_v2_vitl.pth"
            MinGB = 1.0
        }
    )

    $script:ModelFiles = @(
        @{
            Name = "Illustrious-XL-v2.0.safetensors"
            Url = "https://huggingface.co/OnomaAIResearch/Illustrious-XL-v2.0/resolve/main/Illustrious-XL-v2.0.safetensors?download=true"
            Dest = Join-Path $script:Checkpoints "Illustrious-XL-v2.0.safetensors"
            MinGB = 5.5
        },
        @{
            Name = "waiIllustriousSDXL_v170.safetensors"
            Url = "https://huggingface.co/nnnn1111/models-moved/resolve/main/waiIllustriousSDXL_v170.safetensors?download=true"
            Dest = Join-Path $script:Checkpoints "waiIllustriousSDXL_v170.safetensors"
            MinGB = 5.5
        },
        @{
            Name = "illustriousXLDepth_v20.safetensors"
            Url = "https://huggingface.co/azr43l/controlnets/resolve/main/illustriousXLDepth_v20.safetensors?download=true"
            Dest = Join-Path $script:Controlnet "illustriousXLDepth_v20.safetensors"
            MinGB = 4.0
        }
    )

    $script:DetailerFiles = @(
        @{
            Name = "foot-yolov8l.pt"
            Url = "https://huggingface.co/AunyMoons/loras-pack/resolve/main/foot-yolov8l.pt?download=true"
            Dest = Join-Path $script:UltralyticsBbox "foot-yolov8l.pt"
            MinGB = 0.05
        },
        @{
            Name = "face_yolov8m.pt"
            Url = "https://huggingface.co/Bingsu/adetailer/resolve/main/face_yolov8m.pt?download=true"
            Dest = Join-Path $script:UltralyticsBbox "face_yolov8m.pt"
            MinGB = 0.04
        },
        @{
            Name = "person_yolov8s-seg.pt"
            Url = "https://huggingface.co/Bingsu/adetailer/resolve/main/person_yolov8s-seg.pt?download=true"
            Dest = Join-Path $script:UltralyticsSegm "person_yolov8s-seg.pt"
            MinGB = 0.02
        }
    )

    $script:MinimalFiles = @($script:CoreFiles + $script:ModelFiles + $script:DetailerFiles)

    $script:WanFiles = @(
        @{
            Name = "wan2.2_i2v_A14b_high_noise_lightx2v_4step_1030-Q4_K_M.gguf"
            Url = "https://huggingface.co/jayn7/WAN2.2-I2V_A14B-DISTILL-LIGHTX2V-4STEP-GGUF/resolve/main/high_noise_1030/wan2.2_i2v_A14b_high_noise_lightx2v_4step_1030-Q4_K_M.gguf?download=true"
            Dest = Join-Path $script:Unet "wan2.2_i2v_A14b_high_noise_lightx2v_4step_1030-Q4_K_M.gguf"
            MinGB = 8.5
        },
        @{
            Name = "wan2.2_i2v_A14b_low_noise_lightx2v_4step-Q4_K_M.gguf"
            Url = "https://huggingface.co/jayn7/WAN2.2-I2V_A14B-DISTILL-LIGHTX2V-4STEP-GGUF/resolve/main/low_noise/wan2.2_i2v_A14b_low_noise_lightx2v_4step-Q4_K_M.gguf?download=true"
            Dest = Join-Path $script:Unet "wan2.2_i2v_A14b_low_noise_lightx2v_4step-Q4_K_M.gguf"
            MinGB = 8.5
        },
        @{
            Name = "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
            Url = "https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/text_encoders/umt5_xxl_fp8_e4m3fn_scaled.safetensors?download=true"
            Dest = Join-Path $script:TextEncoders "umt5_xxl_fp8_e4m3fn_scaled.safetensors"
            MinGB = 5.5
        },
        @{
            Name = "Wan2.1_VAE.safetensors"
            Url = "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors?download=true"
            Dest = Join-Path $script:Vae "Wan2.1_VAE.safetensors"
            MinGB = 0.2
        }
    )

    # 8.5.1：图生视频画质修复必备——CLIPVision 语义引导模型（两档工作流都必需）。
    # 历史 wan_i2v 工作流缺 CLIPVisionEncode 管线是画质塌房的头号根因。
    $script:WanClipVisionFiles = @(
        @{
            Name = "clip_vision_h.safetensors"
            Url = "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/clip_vision/clip_vision_h.safetensors?download=true"
            Dest = Join-Path $script:ClipVision "clip_vision_h.safetensors"
            MinGB = 1.0
        }
    )

    # 8.5.1：画质档(FP8 原生双模型)——对齐 Wan2.2 教程原生工作流，画质上限高于 Q4 快速档。
    # 单文件约 14GB，5080 16GB 可跑；OOM 时插件自动降级到快速档 Q4 4 步兜底。
    $script:WanQualityFiles = @(
        @{
            Name = "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
            Url = "https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors?download=true"
            Dest = Join-Path $script:Unet "wan2.2_i2v_high_noise_14B_fp8_scaled.safetensors"
            MinGB = 13.0
        },
        @{
            Name = "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"
            Url = "https://huggingface.co/Comfy-Org/Wan_2.2_ComfyUI_Repackaged/resolve/main/split_files/diffusion_models/wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors?download=true"
            Dest = Join-Path $script:Unet "wan2.2_i2v_low_noise_14B_fp8_scaled.safetensors"
            MinGB = 13.0
        }
    )

    $script:AnimaFiles = @(
        @{
            Name = "anima_baseV10.safetensors"
            Url = "https://huggingface.co/LyliaEngine/anima_baseV10/resolve/main/anima_baseV10.safetensors?download=true"
            Dest = Join-Path $script:DiffusionModels "anima_baseV10.safetensors"
            MinGB = 3.0
        },
        @{
            Name = "qwen_3_06b_base.safetensors"
            Url = "https://huggingface.co/Qwen/Qwen3-0.6B-Base/resolve/main/model.safetensors?download=true"
            Dest = Join-Path $script:TextEncoders "qwen_3_06b_base.safetensors"
            MinGB = 0.9
        },
        @{
            Name = "qwen_image_vae.safetensors"
            Url = "https://huggingface.co/Comfy-Org/Qwen-Image_ComfyUI/resolve/main/split_files/vae/qwen_image_vae.safetensors?download=true"
            Dest = Join-Path $script:Vae "qwen_image_vae.safetensors"
            MinGB = 0.2
        },
        @{
            Name = "Anima-LLLite/anima-lllite-any-test-like-1-step2000.safetensors"
            Url = "https://huggingface.co/kohya-ss/Anima-LLLite/resolve/main/anima-lllite-any-test-like-1-step2000.safetensors?download=true"
            Dest = Join-Path $script:Controlnet "Anima-LLLite\anima-lllite-any-test-like-1-step2000.safetensors"
            MinGB = 0.01
        },
        @{
            Name = "anima-highres-aesthetic-boost.safetensors"
            Url = "https://huggingface.co/LyliaEngine/anima-highres-aesthetic-boost/resolve/main/anima-highres-aesthetic-boost.safetensors?download=true"
            Dest = Join-Path $script:Loras "anima-highres-aesthetic-boost.safetensors"
            MinGB = 0.1
        }
    )

    $script:ManualFiles = @(
        @{
            Name = "anima-preview-3-masterpieces-v5.safetensors"
            Dest = Join-Path $script:Loras "anima-preview-3-masterpieces-v5.safetensors"
            Note = "历史工作流默认 LoRA，未找到稳定公开直链；可用插件 Civitai 搜索/导入或手动放入 models\loras。"
        },
        @{
            Name = "qwen_image_union_diffsynth_lora.safetensors"
            Dest = Join-Path $script:Loras "qwen_image_union_diffsynth_lora.safetensors"
            Note = "Union Control 旧链路 LoRA，未找到稳定公开直链；仅使用该旧工作流时需要。"
        },
        @{
            Name = "PVC.safetensors / 3d_style.safetensors / AnimeFigure_ANIMA.safetensors / XDetail_light.safetensors"
            Dest = $script:Loras
            Note = "PVC 画风包，按文件名从 Civitai/Hugging Face 手动确认来源后放入 models\loras。"
        },
        @{
            Name = "nova3DCGXL_ilV90.safetensors"
            Dest = Join-Path $script:Checkpoints "nova3DCGXL_ilV90.safetensors"
            Note = "newpvc checkpoint，按文件名手动确认来源后放入 models\checkpoints。"
        },
        @{
            Name = "Subsurface_Scattering.safetensors / addDetail_Aesthetic.safetensors"
            Dest = $script:Loras
            Note = "newpvc 辅助 LoRA，按文件名手动确认来源后放入 models\loras。"
        }
    )
}

function Show-Status {
    Write-Title "检测结果"
    Write-Host "ComfyUI 根目录：$script:Root"
    Write-Host ""
    Write-Host "核心预处理模型："
    foreach ($file in $script:CoreFiles) {
        Show-PathStatus $file.Dest $file.Name (Get-FileMinGB $file)
    }
    Write-Host ""
    Write-Host "Illustrious 常用模型："
    foreach ($file in $script:ModelFiles) {
        Show-PathStatus $file.Dest $file.Name (Get-FileMinGB $file)
    }
    Write-Host ""
    Write-Host "Illustrious foot Detailer mandatory model:"
    foreach ($file in $script:DetailerFiles) {
        Show-PathStatus $file.Dest $file.Name (Get-FileMinGB $file)
    }
    Write-Host ""
    Write-Host "Wan 视频当前工作流模型（快速档）："
    foreach ($file in $script:WanFiles) {
        Show-PathStatus $file.Dest $file.Name (Get-FileMinGB $file)
    }
    Write-Host ""
    Write-Host "Wan CLIP Vision 模型（图生视频语义引导，8.5.1 必备）："
    foreach ($file in $script:WanClipVisionFiles) {
        Show-PathStatus $file.Dest $file.Name (Get-FileMinGB $file)
    }
    Write-Host ""
    Write-Host "Wan 画质档模型（FP8 原生双模型）："
    foreach ($file in $script:WanQualityFiles) {
        Show-PathStatus $file.Dest $file.Name (Get-FileMinGB $file)
    }
    Write-Host ""
    Write-Host "Anima / Qwen 旧链路可自动补齐模型："
    foreach ($file in $script:AnimaFiles) {
        Show-PathStatus $file.Dest $file.Name (Get-FileMinGB $file)
    }
    Write-Host ""
    Write-Host "最小必需自定义节点："
    Show-PathStatus (Join-Path $script:CustomNodes "comfyui_controlnet_aux") "comfyui_controlnet_aux"
    Show-PathStatus (Join-Path $script:CustomNodes "ComfyUI-Impact-Pack") "ComfyUI-Impact-Pack"
    Show-PathStatus (Join-Path $script:CustomNodes "ComfyUI-Impact-Subpack") "ComfyUI-Impact-Subpack"
    Write-Host ""
    Write-Host "可选节点："
    Show-PathStatus (Join-Path $script:CustomNodes "ComfyUI_UltimateSDUpscale") "ComfyUI_UltimateSDUpscale"
    Show-PathStatus (Join-Path $script:CustomNodes "ComfyUI-Anima-LLLite") "ComfyUI-Anima-LLLite"
    Show-PathStatus (Join-Path $script:CustomNodes "ComfyUI_LayerStyle") "ComfyUI_LayerStyle (LayerUtility: ImageScaleByAspectRatio V2)"
    Show-PathStatus (Join-Path $script:CustomNodes "ComfyUI-GGUF") "ComfyUI-GGUF"
    Show-PathStatus (Join-Path $script:CustomNodes "ComfyUI-VideoHelperSuite") "ComfyUI-VideoHelperSuite"
    Show-PathStatus (Join-Path $script:CustomNodes "ComfyUI-WanMoeKSampler") "ComfyUI-WanMoeKSampler"
    Write-Host ""
    Write-Host "AstrBot 跨机器 LoRA 下载："
    Show-PathStatus (Join-Path $script:CustomNodes "ComfyUI-AstrBot-LoraBridge") "ComfyUI-AstrBot-LoraBridge"
    Write-Host ""
    Write-Host "需手动确认来源的历史/风格资源："
    foreach ($file in $script:ManualFiles) {
        Show-ManualFileStatus $file
    }
}

function Install-MinimalNodes {
    Install-NodeRepo $script:CustomNodes "https://github.com/Fannovel16/comfyui_controlnet_aux" "comfyui_controlnet_aux"
    Install-NodeRepo $script:CustomNodes "https://github.com/ltdrdata/ComfyUI-Impact-Pack" "ComfyUI-Impact-Pack"
    Install-NodeRepo $script:CustomNodes "https://github.com/ltdrdata/ComfyUI-Impact-Subpack" "ComfyUI-Impact-Subpack"
    Install-NodeRequirements "ComfyUI-Impact-Pack"
    Install-NodeRequirements "ComfyUI-Impact-Subpack"
    Install-SubjectAuditRequirements
}

function Download-MinimalModels {
    foreach ($file in $script:MinimalFiles) {
        Download-File $file.Url $file.Dest (Get-FileMinGB $file)
    }
}

function Download-CoreModels {
    foreach ($file in $script:CoreFiles) {
        Download-File $file.Url $file.Dest (Get-FileMinGB $file)
    }
}

function Download-WanModels {
    Write-Title "Wan 视频模型下载（快速档 Q4 + 文本编码器/VAE）"
    foreach ($file in $script:WanFiles) {
        Download-File $file.Url $file.Dest (Get-FileMinGB $file)
    }
}

function Download-WanClipVision {
    Write-Title "Wan CLIP Vision 模型下载（图生视频语义引导，8.5.1 画质修复必备）"
    foreach ($file in $script:WanClipVisionFiles) {
        Download-File $file.Url $file.Dest (Get-FileMinGB $file)
    }
    Write-Host ""
    Write-Host "CLIP Vision 模型已就位。两个工作流(快速档/画质档)都需要它，不下会画质塌房。"
}

function Download-WanQualityModels {
    Write-Title "Wan 视频画质档模型下载（FP8 原生双模型，对齐 Wan2.2 教程原生工作流）"
    Write-Host "每个文件约 14GB，共 ~28GB；下载前请确认磁盘空间充足。"
    Write-Host "5080 16GB 可跑 832x480/81 帧；OOM 时插件自动降级到快速档 Q4 4 步。"
    foreach ($file in $script:WanQualityFiles) {
        Download-File $file.Url $file.Dest (Get-FileMinGB $file)
    }
    Write-Host ""
    Write-Host "画质档模型已就位。在插件配置 video_settings.workflow 选择 wan_i2v_quality.json 即可启用。"
}

function Download-AnimaModels {
    Write-Title "Anima / Qwen 旧链路模型下载"
    foreach ($file in $script:AnimaFiles) {
        Download-File $file.Url $file.Dest (Get-FileMinGB $file)
    }
    Write-Host ""
    Write-Host "Anima 历史默认 LoRA anima-preview-3-masterpieces-v5.safetensors 未找到稳定公开直链；如果工作流报缺失，请用插件 Civitai 搜索/导入或手动放入 models\loras。"
}

function Install-HqNode {
    Install-NodeRepo $script:CustomNodes "https://github.com/ssitu/ComfyUI_UltimateSDUpscale" "ComfyUI_UltimateSDUpscale"
}

function Install-AnimaNodes {
    Install-MinimalNodes
    Install-NodeRepo $script:CustomNodes "https://github.com/kohya-ss/ComfyUI-Anima-LLLite" "ComfyUI-Anima-LLLite"
    Install-NodeRepo $script:CustomNodes "https://github.com/chflame163/ComfyUI_LayerStyle" "ComfyUI_LayerStyle"
    Write-Host ""
    Write-Host "Anima 自定义节点处理完成；如需已确认直链模型，运行 -DownloadAnimaModels 或 -Mode complete。"
}

function Install-WanNodes {
    Install-NodeRepo $script:CustomNodes "https://github.com/city96/ComfyUI-GGUF" "ComfyUI-GGUF"
    Install-NodeRepo $script:CustomNodes "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite" "ComfyUI-VideoHelperSuite"
    Install-NodeRepo $script:CustomNodes "https://github.com/stduhpf/ComfyUI-WanMoeKSampler" "ComfyUI-WanMoeKSampler"
    Write-Host ""
    Write-Host "Wan 自定义节点处理完成；如需模型，运行 -DownloadWanModels 或 -Mode complete。"
}

function Install-LitePackage {
    Write-Title "轻量化安装"
    Install-MinimalNodes
    Download-CoreModels
    Install-LoraBridge
    Write-Host ""
    Write-Host "轻量化安装完成：已安装 Bridge、comfyui_controlnet_aux，并补齐 Depth-Anything V2 Large 预处理模型。"
    Write-Host "请重启 ComfyUI。"
}

function Install-FullPackage {
    Write-Title "完整版安装"
    Install-MinimalNodes
    Download-MinimalModels
    Install-HqNode
    Install-AnimaNodes
    Install-WanNodes
    Install-LoraBridge
    Write-Host ""
    Write-Host "完整版安装完成：已安装推荐节点、Bridge、Illustrious checkpoint、Depth ControlNet 和 Depth-Anything 预处理模型。"
    Write-Host "请重启 ComfyUI。"
}

function Install-CompleteRuntimePackage {
    Write-Title "完整运行环境"
    Install-MinimalNodes
    Download-MinimalModels
    Install-HqNode
    Install-AnimaNodes
    Download-AnimaModels
    Install-WanNodes
    Download-WanModels
    Download-WanClipVision
    Install-LoraBridge
    Write-Host ""
    Write-Host "完整运行环境处理完成：已补齐普通绘图、Wan 视频快速档、Wan CLIP Vision、Anima/Qwen 旧链路的已确认节点和模型，并安装 Resource Bridge。"
    Write-Host "画质档(FP8 原生双模型, ~28GB)默认不自动下载——如需更高画质，在主菜单选 5b 单独下载，并切到 wan_i2v_quality.json。"
    Write-Host "仍标记为 [手动] 的历史/风格 LoRA 需要你按文件名确认来源后放入对应目录。"
    Write-Host "请重启 ComfyUI。"
    Write-Host ""
    Write-Host "下面是最终检测汇总："
    Show-Status
}

function Ask-HfMirror {
    if ($script:UseHfMirror) {
        return
    }
    $answer = (Read-Host "Hugging Face 是否使用 hf-mirror.com 镜像？[Y/n]").Trim().ToLower()
    if ($answer -eq "" -or $answer -eq "y" -or $answer -eq "yes") {
        $script:UseHfMirror = $true
    }
}

function Ask-ClashLanProxy {
    if ($script:BridgeProxy -or $script:UseClashLan) {
        return
    }
    Write-Host ""
    Write-Host "如果 ComfyUI 机器需要通过 Clash LAN 出网，请填写 HTTP 代理地址。"
    Write-Host "示例：http://192.168.1.23:7890；留空表示不用代理。"
    $proxy = Normalize-HttpProxy (Read-Host "Clash LAN HTTP 代理")
    if ($proxy) {
        $script:BridgeProxy = $proxy
        Write-Host "已设置代理：$proxy"
    }
}

function Resolve-SetupMode {
    param([string]$RequestedMode)
    $value = (str $RequestedMode).Trim().ToLower()
    if (-not $value) {
        return ""
    }
    if ($value -in @("lite", "light")) { return "lite" }
    if ($value -in @("full", "standard")) { return "full" }
    if ($value -in @("complete", "all", "runtime")) { return "complete" }
    if ($value -in @("check", "status", "detect")) { return "check" }
    if ($value -in @("menu", "interactive")) { return "menu" }
    if ($value -in @("exit", "quit")) { return "exit" }
    return $value
}

function Test-FileRecordValid {
    param($File)
    if (-not $File -or -not (Test-Path -LiteralPath $File.Dest)) {
        return $false
    }
    return (-not (Get-ModelFileProblem $File.Dest (Get-FileMinGB $File)))
}

function Get-ValidFileCount {
    param($Files)
    $count = 0
    foreach ($file in @($Files)) {
        if (Test-FileRecordValid $file) {
            $count += 1
        }
    }
    return $count
}

function Test-DirExists {
    param([string]$Path)
    return (Test-Path -LiteralPath $Path)
}

function Show-Dashboard {
    Write-Header "ComfyUI：$script:Root"
    $minimalOk = Get-ValidFileCount $script:MinimalFiles
    $wanOk = Get-ValidFileCount $script:WanFiles
    $wanClipOk = Get-ValidFileCount $script:WanClipVisionFiles
    $wanQualityOk = Get-ValidFileCount $script:WanQualityFiles
    $animaOk = Get-ValidFileCount $script:AnimaFiles
    $nodeOk = 0
    foreach ($path in @(
        (Join-Path $script:CustomNodes "comfyui_controlnet_aux"),
        (Join-Path $script:CustomNodes "ComfyUI-Impact-Pack"),
        (Join-Path $script:CustomNodes "ComfyUI-Impact-Subpack"),
        (Join-Path $script:CustomNodes "ComfyUI_UltimateSDUpscale"),
        (Join-Path $script:CustomNodes "ComfyUI-Anima-LLLite"),
        (Join-Path $script:CustomNodes "ComfyUI_LayerStyle"),
        (Join-Path $script:CustomNodes "ComfyUI-GGUF"),
        (Join-Path $script:CustomNodes "ComfyUI-VideoHelperSuite"),
        (Join-Path $script:CustomNodes "ComfyUI-WanMoeKSampler"),
        (Join-Path $script:CustomNodes "ComfyUI-AstrBot-LoraBridge")
    )) {
        if (Test-DirExists $path) {
            $nodeOk += 1
        }
    }
    $mirror = if ($script:UseHfMirror) { "hf-mirror.com" } else { "huggingface.co" }
    $proxy = Resolve-DownloadProxy
    if (-not $proxy) { $proxy = "直连" }
    Write-Host (" 模型状态   基础 {0}/{1}   Wan {2}/{3}   Wan CLIP {6}/{7}   Wan画质 {8}/{9}   Anima/Qwen {4}/{5}" -f $minimalOk, @($script:MinimalFiles).Count, $wanOk, @($script:WanFiles).Count, $animaOk, @($script:AnimaFiles).Count, $wanClipOk, @($script:WanClipVisionFiles).Count, $wanQualityOk, @($script:WanQualityFiles).Count)
    Write-Host (" 节点状态   已安装 {0}/10" -f $nodeOk)
    Write-Host (" 网络状态   HuggingFace={0}   代理={1}" -f $mirror, $proxy)
    Write-Rule
}

function Show-Menu {
    Write-Host ""
    Write-Host " 快速方案"
    Write-MenuItem "1" "完整运行环境" "普通绘图 + Anima/Qwen + Wan 视频 + Bridge，一键补齐。"
    Write-MenuItem "2" "完整绘图环境" "普通绘图模型、推荐节点、Bridge。"
    Write-MenuItem "3" "轻量修复" "只装 Bridge + controlnet_aux + Depth-Anything。"
    Write-MenuItem "4" "深度检测" "显示每个文件/节点的 [正常]、[缺失]、[损坏] 状态。"
    Write-Host ""
    Write-Host " 定向修复"
    Write-MenuItem "5" "修复/下载 Wan 视频模型（快速档）" "GGUF 高低噪、UMT5 文本编码器、Wan VAE。"
    Write-MenuItem "5a" "下载 Wan CLIP Vision 模型" "图生视频语义引导，8.5.1 画质修复必备，两档工作流都要。"
    Write-MenuItem "5b" "下载 Wan 画质档模型（FP8）" "原生高/低噪双模型，对齐 Wan2.2 教程，画质上限更高（~28GB）。"
    Write-MenuItem "6" "安装 Wan 视频节点" "ComfyUI-GGUF、VideoHelperSuite、WanMoeKSampler。"
    Write-MenuItem "7" "修复/下载 Anima/Qwen 模型" "Anima base、Qwen 文本编码器、Qwen VAE、Anima LLLite。"
    Write-MenuItem "8" "安装 Anima 旧链路节点" "controlnet_aux、Anima-LLLite、LayerStyle。"
    Write-MenuItem "9" "修复/下载普通绘图模型" "Illustrious/WAI checkpoint、Depth ControlNet、Depth-Anything。"
    Write-MenuItem "10" "只安装核心节点" "comfyui_controlnet_aux。"
    Write-MenuItem "11" "安装/刷新 Resource Bridge" "让 ComfyUI 机器远程下载 LoRA/模型资源。"
    Write-MenuItem "12" "网络设置" "Hugging Face 镜像、Clash/LAN 代理。"
    Write-Host ""
    Write-MenuItem "0" "退出"
}

function Configure-NetworkOptions {
    Write-Title "网络设置"
    $currentMirror = if ($script:UseHfMirror) { "Y" } else { "N" }
    $mirrorAnswer = (Read-Host "Hugging Face 使用 hf-mirror.com 镜像？当前=$currentMirror [Y/N/回车保持]").Trim().ToLower()
    if ($mirrorAnswer -in @("y", "yes")) {
        $script:UseHfMirror = $true
    } elseif ($mirrorAnswer -in @("n", "no")) {
        $script:UseHfMirror = $false
    }
    $currentProxy = Resolve-DownloadProxy
    if (-not $currentProxy) { $currentProxy = "直连" }
    Write-Host "当前代理：$currentProxy"
    Write-Host "可输入 http://192.168.1.23:7890；输入 none 清空；直接回车保持。"
    $proxyAnswer = (Read-Host "代理").Trim()
    if ($proxyAnswer.ToLower() -eq "none") {
        $script:BridgeProxy = ""
        $script:UseClashLan = $false
    } elseif ($proxyAnswer) {
        $script:BridgeProxy = Normalize-HttpProxy $proxyAnswer
    }
}

function Invoke-MenuAction {
    param([string]$Choice)
    if ($Choice -eq "1") {
        Install-CompleteRuntimePackage
    } elseif ($Choice -eq "2") {
        Install-FullPackage
    } elseif ($Choice -eq "3") {
        Install-LitePackage
    } elseif ($Choice -eq "4") {
        Show-Status
    } elseif ($Choice -eq "5") {
        Download-WanModels
        Write-Host ""
        Write-Host "Wan 视频模型（快速档）处理完成。如果 ComfyUI 已经在运行，请重启 ComfyUI。"
    } elseif ($Choice -eq "5a") {
        Download-WanClipVision
        Write-Host ""
        Write-Host "CLIP Vision 模型处理完成。请重启 ComfyUI。"
    } elseif ($Choice -eq "5b") {
        Download-WanQualityModels
        Write-Host ""
        Write-Host "画质档模型处理完成。在插件配置 video_settings.workflow 选 wan_i2v_quality.json。请重启 ComfyUI。"
    } elseif ($Choice -eq "6") {
        Install-WanNodes
        Write-Host ""
        Write-Host "Wan 视频节点处理完成。请重启 ComfyUI。"
    } elseif ($Choice -eq "7") {
        Download-AnimaModels
        Write-Host ""
        Write-Host "Anima/Qwen 模型处理完成。"
    } elseif ($Choice -eq "8") {
        Install-AnimaNodes
        Write-Host ""
        Write-Host "Anima 旧链路节点处理完成。请重启 ComfyUI。"
    } elseif ($Choice -eq "9") {
        Download-MinimalModels
        Write-Host ""
        Write-Host "普通绘图模型处理完成。"
    } elseif ($Choice -eq "10") {
        Install-MinimalNodes
        Write-Host ""
        Write-Host "核心节点处理完成。请重启 ComfyUI。"
    } elseif ($Choice -eq "11") {
        Install-LoraBridge
        Write-Host ""
        Write-Host "Resource Bridge 处理完成。请重启 ComfyUI。"
    } elseif ($Choice -eq "12") {
        Configure-NetworkOptions
    } else {
        Write-Warning "无效选择：$Choice"
    }
}

function Invoke-MainMenu {
    while ($true) {
        Show-Dashboard
        Show-Menu
        $choice = (Read-Host "输入序号").Trim()
        if ($choice -eq "0") {
            Write-Host "退出。"
            break
        }
        try {
            Invoke-MenuAction $choice
        } catch {
            Write-Host ""
            Write-Host "[错误] $($_.Exception.Message)"
        }
        Wait-ReturnToMenu
    }
}

$hasAction = $InstallCustomNodes -or $DownloadMinimal -or $InstallHqNode -or $InstallAnimaNodes -or $InstallWanNodes -or $DownloadWanModels -or $DownloadWanQuality -or $DownloadClipVision -or $DownloadAnimaModels -or $InstallCompleteRuntime -or $InstallLoraBridge -or $CheckOnly -or [bool](str $Mode)
$interactive = -not $NonInteractive -and (-not $hasAction -or -not $ComfyRoot)

if ($interactive) {
    $root = Resolve-ComfyRootInteractive $ComfyRoot
} else {
    $root = Resolve-ComfyRootStrict $ComfyRoot
}

Initialize-Paths $root

$runInteractiveMenu = ($interactive -and ($hasAction -eq $false))
if ($runInteractiveMenu) {
    Invoke-MainMenu
    exit 0
}

$selectedMode = if ((str $Mode).Trim()) { Resolve-SetupMode $Mode } else { "" }
if ($selectedMode -eq "menu") {
    Invoke-MainMenu
    exit 0
}

Show-Status

if ($CheckOnly -or $selectedMode -eq "check") {
    Write-Host ""
    Write-Host "检测完成。"
    exit 0
}

if ($selectedMode -eq "lite") {
    Install-LitePackage
    exit 0
}

if ($selectedMode -eq "full") {
    Install-FullPackage
    exit 0
}

if ($selectedMode -eq "complete") {
    Install-CompleteRuntimePackage
    exit 0
}

if ($selectedMode -eq "exit") {
    Write-Host "退出。"
    exit 0
}

if ($InstallCustomNodes) { Install-MinimalNodes }
if ($DownloadMinimal) { Download-MinimalModels }
if ($InstallHqNode) { Install-HqNode }
if ($InstallAnimaNodes) { Install-AnimaNodes }
if ($InstallWanNodes) { Install-WanNodes }
if ($DownloadWanModels) { Download-WanModels }
if ($DownloadClipVision) { Download-WanClipVision }
if ($DownloadWanQuality) { Download-WanQualityModels }
if ($DownloadAnimaModels) { Download-AnimaModels }
if ($InstallCompleteRuntime) { Install-CompleteRuntimePackage }
if ($InstallLoraBridge) { Install-LoraBridge }

Write-Host ""
Write-Host "处理完成。安装或更新自定义节点后，请重启 ComfyUI。"
