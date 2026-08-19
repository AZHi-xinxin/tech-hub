# tech-hub Windows 启动脚本
# 用法：
#   1. 安装 Python 3.10+（安装时勾选 "Add python.exe to PATH"）
#   2. 复制 credentials.env.example 为 credentials.env，把五个 TOKEN 换成自己的随机串
#   3. 双击本文件即可启动；浏览器打开 http://127.0.0.1:8791/ui
# 说明：hub 是单文件 + SQLite，本脚本只负责读配置、查依赖、拉起进程，无其他改动。
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

if (-not (Test-Path 'credentials.env')) {
    Write-Host '[start-hub] 未找到 credentials.env：请先复制 credentials.env.example 改名，并填入自己的 TOKEN。'
    Read-Host '按回车退出'
    exit 1
}

# 读取 credentials.env（KEY=VALUE，支持 # 注释与空行）
Get-Content 'credentials.env' -Encoding UTF8 | ForEach-Object {
    $line = $_.Trim()
    if ($line -and -not $line.StartsWith('#')) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim())
        }
    }
}

# 找 Python
$py = $null
foreach ($c in @('py', 'python', 'python3')) {
    if (Get-Command $c -ErrorAction SilentlyContinue) { $py = $c; break }
}
if (-not $py) {
    Write-Host '[start-hub] 未找到 Python：请安装 Python 3.10+ 并勾选 Add to PATH。'
    Read-Host '按回车退出'
    exit 1
}

# 检查并安装依赖
& $py -c 'import fastapi, uvicorn' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host '[start-hub] 缺少依赖，正在安装 fastapi uvicorn（首次较慢）...'
    & $py -m pip install fastapi uvicorn
    if ($LASTEXITCODE -ne 0) {
        Write-Host '[start-hub] 依赖安装失败，请手动执行：' + $py + ' -m pip install fastapi uvicorn'
        Read-Host '按回车退出'
        exit 1
    }
}

$port = [Environment]::GetEnvironmentVariable('TECH_HUB_PORT')
if (-not $port) { $port = '8791' }
Write-Host '[start-hub] tech-hub 启动中，聊天界面: http://127.0.0.1:' + $port + '/ui （Ctrl+C 停止）'
& $py hub.py
