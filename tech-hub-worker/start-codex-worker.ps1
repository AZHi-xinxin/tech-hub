param(
    [string]$WorkerRoot = $PSScriptRoot,
    [string]$TokenPath = ''
)

$ErrorActionPreference = 'Stop'
$userProfile = [Environment]::GetFolderPath('UserProfile')
if ([string]::IsNullOrWhiteSpace($TokenPath)) {
    $TokenPath = Join-Path $userProfile '.tech-hub\secrets\codex.token'
}
$configPath = Join-Path $WorkerRoot 'codex-worker.config.json'
$extensionRoot = Join-Path $userProfile '.vscode\extensions'

if (-not (Test-Path -LiteralPath $configPath -PathType Leaf)) {
    throw "Worker config not found: $configPath"
}
if (-not (Test-Path -LiteralPath $TokenPath -PathType Leaf)) {
    throw "Token file not found: $TokenPath"
}

$codexPattern = Join-Path $extensionRoot 'openai.chatgpt-*-win32-x64\bin\windows-x86_64\codex.exe'
$codex = Get-ChildItem -Path $codexPattern -File -ErrorAction SilentlyContinue |
    Where-Object { Test-Path -LiteralPath (Join-Path $_.DirectoryName 'codex-code-mode-host.exe') } |
    Sort-Object LastWriteTimeUtc -Descending |
    Select-Object -First 1
if (-not $codex) { throw 'No complete VS Code Codex binary directory found' }
$python = Get-Command python -ErrorAction Stop

$config = Get-Content -LiteralPath $configPath -Raw -Encoding UTF8 | ConvertFrom-Json
$config.codex_command = $codex.FullName
[IO.File]::WriteAllText($configPath, ($config | ConvertTo-Json -Depth 8), [Text.UTF8Encoding]::new($false))

$env:TECH_HUB_CODEX_TOKEN = (Get-Content -LiteralPath $TokenPath -Raw).Trim()
Set-Location -LiteralPath $WorkerRoot
while ($true) {
    & $python.Source '.\codex_worker.py' --config '.\codex-worker.config.json'
    Start-Sleep -Seconds 5
}
