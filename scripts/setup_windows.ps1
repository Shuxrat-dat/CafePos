# CafePOS: настройка Windows на ноутбуке заведения. Запускается из install.bat от администратора.
# Автозапуск сервера при включении, ежедневный бэкап, порт 8000 в брандмауэре, без сна от сети.
param([Parameter(Mandatory = $true)][string]$Root)
$ErrorActionPreference = "Stop"

# Wi-Fi кафе как частная сеть: иначе брандмауэр не пустит телефоны и планшеты.
try {
    Get-NetConnectionProfile | Where-Object NetworkCategory -ne "DomainAuthenticated" |
        Set-NetConnectionProfile -NetworkCategory Private
    Write-Host "Network: private"
} catch {
    Write-Host "Network: could not switch to private, set it in Settings - Network"
}

Get-NetFirewallRule -DisplayName "CafePOS" -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName "CafePOS" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8000 -Profile Private | Out-Null
Write-Host "Firewall: port 8000 open for private networks"

# Задачи от SYSTEM работают до входа в Windows. Без этих настроек Windows не запустит
# задачу на батарее и остановит её через 3 дня.
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest

$serverSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
$serverAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Root\start_server.bat`"" -WorkingDirectory $Root
Register-ScheduledTask -TaskName "CafePOS" -Action $serverAction -Trigger (New-ScheduledTaskTrigger -AtStartup) `
    -Principal $principal -Settings $serverSettings -Force | Out-Null
Write-Host "Task CafePOS: server starts when the laptop is turned on"

$backupSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit (New-TimeSpan -Hours 1) -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries -StartWhenAvailable
$backupAction = New-ScheduledTaskAction -Execute "cmd.exe" -Argument "/c `"$Root\backup.bat`"" -WorkingDirectory $Root
Register-ScheduledTask -TaskName "CafePOS backup" -Action $backupAction -Trigger (New-ScheduledTaskTrigger -Daily -At "23:30") `
    -Principal $principal -Settings $backupSettings -Force | Out-Null
Write-Host "Task CafePOS backup: every day at 23:30, copies in backups\"

# От сети: не засыпать и ничего не делать при закрытии крышки. На батарее настройки не меняются.
powercfg /change standby-timeout-ac 0
powercfg /change hibernate-timeout-ac 0
powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0
powercfg /setactive SCHEME_CURRENT
Write-Host "Power: no sleep while plugged in, closing the lid does nothing"
