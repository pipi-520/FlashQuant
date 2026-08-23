# 注册 Windows 任务计划：开机自启 monitor 守护循环（无需用户登录）。
# 用法（管理员 PowerShell，在项目根目录执行）：
#   powershell -ExecutionPolicy Bypass -File scripts\install_win_service.ps1
# 卸载：
#   Unregister-ScheduledTask -TaskName 'chaogu-monitor' -Confirm:$false

$Root = Split-Path -Parent $PSScriptRoot
$Loop = Join-Path $Root 'scripts\monitor_win_loop.ps1'

if (-not (Test-Path $Loop)) {
    Write-Error "未找到守护脚本：$Loop"
    exit 1
}

$taskName = 'chaogu-monitor'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Loop`""
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -StartWhenAvailable

Register-ScheduledTask -TaskName $taskName `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -RunLevel Highest `
    -User 'SYSTEM' `
    -Description 'FlashQuant 实时新闻事件监测（24/7，开机自启+崩溃重启）' `
    -Force | Out-Null

Write-Host "[OK] 已注册任务计划 $taskName（开机自启）"
Write-Host "      手动立即启动：Start-ScheduledTask -TaskName $taskName"
Write-Host "      查看状态：Get-ScheduledTask -TaskName $taskName | Get-ScheduledTaskInfo"
