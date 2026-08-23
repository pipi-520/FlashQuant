# FlashQuant 实时监测守护循环（Windows）
# 用途：monitor.py 退出（崩溃/异常）后 5 秒自动重启，实现类似 systemd Restart=always 的效果。
# 由 install_win_service.ps1 注册为「开机自启」任务后，配合使用。

$ErrorActionPreference = 'Continue'

$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Monitor = Join-Path $Root 'news_aggregator\monitor.py'
$LogDir = Join-Path $Root 'logs'
$Log = Join-Path $LogDir 'monitor.log'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

while ($true) {
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $Log -Encoding UTF8 -Value "[$ts] monitor.py 启动"
    & $Python $Monitor *>> $Log
    $code = $LASTEXITCODE
    $ts = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
    Add-Content -Path $Log -Encoding UTF8 -Value "[$ts] monitor.py 退出 (exit=$code)，5 秒后重启"
    Start-Sleep -Seconds 5
}
