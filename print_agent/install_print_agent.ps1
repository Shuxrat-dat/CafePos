# CafePOS print agent: task in Task Scheduler. Called from install_print_agent.bat as administrator.
# Runs as SYSTEM at startup (before anyone logs in), never stops after 3 days, works on battery,
# restarts every minute if the agent exits.
param(
    [Parameter(Mandatory = $true)][string]$Root,
    [Parameter(Mandatory = $true)][string]$Python
)
$ErrorActionPreference = "Stop"

$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Root\cafepos_print_agent.py`"" -WorkingDirectory $Root
Register-ScheduledTask -TaskName "CafePOS print agent" -Action $action -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Principal $principal -Settings $settings -Force | Out-Null

Stop-ScheduledTask -TaskName "CafePOS print agent" -ErrorAction SilentlyContinue
Start-ScheduledTask -TaskName "CafePOS print agent"
Write-Host "Task CafePOS print agent: started, and will start at every boot"
