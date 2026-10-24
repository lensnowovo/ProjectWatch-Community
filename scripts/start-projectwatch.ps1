# ProjectWatch 一键启动（Windows）
# 首次运行：检查 Python → 在 %LOCALAPPDATA%\ProjectWatch 建立独立环境 → 安装 → 启动并打开浏览器。
$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
$home_dir = Join-Path $env:LOCALAPPDATA "ProjectWatch"
$venv = Join-Path $home_dir "venv-projectwatch-local"
$python = Join-Path $venv "Scripts\python.exe"
$marker = Join-Path $home_dir "installed-projectwatch-local.txt"
New-Item -ItemType Directory -Path $home_dir -Force | Out-Null

function Find-Python {
    foreach ($v in @("3.13", "3.12", "3.11")) {
        try { $p = & py "-$v" -c "import sys; print(sys.executable)" 2>$null; if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() } } catch {}
    }
    try {
        $p = & python -c "import sys; assert sys.version_info >= (3, 11); print(sys.executable)" 2>$null
        if ($LASTEXITCODE -eq 0 -and $p) { return $p.Trim() }
    } catch {}
    return $null
}

function Test-Dependencies([string]$PythonExecutable, [string]$Repository) {
    try {
        & $PythonExecutable -c "import project_watch.studio.app, fastapi, uvicorn, pydantic; from importlib.metadata import version; version('projectwatch-local')" 2>$null | Out-Null
        $dependenciesReady = $LASTEXITCODE -eq 0
    } catch { $dependenciesReady = $false }
    if (-not $dependenciesReady) {
        Write-Host "运行环境依赖不完整。请在 PowerShell 中执行下列命令，再重新启动：" -ForegroundColor Yellow
        Write-Host "& '$PythonExecutable' -m pip install -e '$Repository[ocr]'"
        return $false
    }
    return $true
}

function Test-PortAvailable([int]$Port) {
    $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, $Port)
    try { $listener.Start(); return $true } catch { return $false } finally { $listener.Stop() }
}

function Test-StartupPort([int]$Preferred = 8796) {
    if ($Preferred -lt 1 -or $Preferred -gt 65516) {
        Write-Host "端口应在 1 到 65516 之间，例如 --port 9000。" -ForegroundColor Yellow
        return $false
    }
    foreach ($candidate in $Preferred..($Preferred + 19)) {
        if (Test-PortAvailable $candidate) {
            if ($candidate -ne $Preferred) {
                Write-Host "端口 $Preferred 已被占用，将自动使用 $candidate，不影响已有服务。"
            }
            return $true
        }
    }
    Write-Host "端口 $Preferred 附近均被占用，请用 Start-ProjectWatch.cmd --port 9000 指定其他端口；不要停止不相关的进程。" -ForegroundColor Yellow
    return $false
}

if (-not (Test-Path $python)) {
    $base = Find-Python
    if (-not $base) {
        Write-Host "没有找到 Python 3.11 或更新版本。" -ForegroundColor Yellow
        if (Get-Command winget -ErrorAction SilentlyContinue) {
            Write-Host "正在通过 winget 安装 Python 3.12……"
            winget install -e --id Python.Python.3.12 --accept-package-agreements --accept-source-agreements
            $base = Find-Python
        }
        if (-not $base) {
            Write-Host "请先从 https://www.python.org/downloads/ 安装 Python 3.12（勾选 Add to PATH），然后重新双击启动。" -ForegroundColor Red
            exit 1
        }
    }
    Write-Host "正在创建运行环境……"
    & $base -m venv $venv
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $python)) {
        Write-Host "运行环境创建失败。请检查 Python 安装和目录写入权限，然后重新双击启动。" -ForegroundColor Red
        exit 1
    }
}

$stamp = (Get-Item (Join-Path $repo "pyproject.toml")).LastWriteTimeUtc.Ticks.ToString() + "|" + $repo
$installed = if (Test-Path $marker) { Get-Content $marker -Raw } else { "" }
if ($installed.Trim() -ne $stamp) {
    Write-Host "正在安装 ProjectWatch（首次约 1-3 分钟）……"
    & $python -m pip install --disable-pip-version-check -q --upgrade "pip>=26.2.2"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "安装工具更新失败。请检查网络后重试；尚未安装 ProjectWatch。" -ForegroundColor Red
        exit 1
    }
    & $python -m pip install --disable-pip-version-check -q -e "$repo[ocr]"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "默认源安装失败，改用清华镜像重试……" -ForegroundColor Yellow
        & $python -m pip install --disable-pip-version-check -q -e "$repo[ocr]" -i https://pypi.tuna.tsinghua.edu.cn/simple
        if ($LASTEXITCODE -ne 0) { Write-Host "安装失败，请检查网络后重试。" -ForegroundColor Red; exit 1 }
    }
    Set-Content -Path $marker -Value $stamp -Encoding UTF8
}

if (-not (Test-Dependencies $python $repo)) { exit 1 }
$preferredPort = 8796
for ($argumentIndex = 0; $argumentIndex -lt $args.Count; $argumentIndex++) {
    $argument = [string]$args[$argumentIndex]
    $portText = $null
    if ($argument -eq '--port') {
        if ($argumentIndex + 1 -lt $args.Count) { $portText = [string]$args[++$argumentIndex] }
        else { Write-Host '请在 --port 后填写端口，例如 --port 9000。'; exit 1 }
    } elseif ($argument.StartsWith('--port=')) { $portText = $argument.Substring(7) }
    if ($null -ne $portText -and -not [int]::TryParse($portText, [ref]$preferredPort)) {
        Write-Host '端口应为数字，例如 --port 9000。'
        exit 1
    }
}
if (-not (Test-StartupPort $preferredPort)) { exit 1 }
& $python -m project_watch.studio @args
exit $LASTEXITCODE
