# CafePOS Juma sync: task in Task Scheduler. Called from install_juma_sync.bat as administrator.
# Runs as SYSTEM at startup (before anyone logs in), never stops after 3 days, works on battery.
param(
    [Parameter(Mandatory = $true)][string]$Root
)
$ErrorActionPreference = "Stop"

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$action = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Root\start_juma_sync.bat`"" -WorkingDirectory $Root
Register-ScheduledTask -TaskName "CafePOS Juma sync" -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Principal $principal -Settings $settings -Force | Out-Null

Stop-ScheduledTask -TaskName "CafePOS Juma sync" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "CafePOS Juma sync"
Write-Host "Task CafePOS Juma sync: started, and will start at every boot"
