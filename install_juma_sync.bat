@echo off
rem CafePOS: autostart of the Juma delivery sync. Run as administrator, after install.bat.
rem Safe to run again: replaces the task with the same settings.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

net session >nul 2>&1
if errorlevel 1 (
  echo Run install_juma_sync.bat as administrator: right click - Run as administrator.
  pause
  exit /b 1
)

if not exist .venv\Scripts\python.exe (
  echo Run install.bat first.
  pause
  exit /b 1
)

findstr /b /c:"JUMA_URL=" .env >nul 2>&1
if errorlevel 1 (
  echo JUMA_URL and JUMA_MERCHANT_KEY are not set in .env. The Juma administrator gives the key in the cafe card.
  pause
  exit /b 1
)

echo [1/2] Database
.venv\Scripts\python.exe manage.py migrate --noinput
if errorlevel 1 (
  echo Migrations failed.
  pause
  exit /b 1
)

echo [2/2] Autostart task "CafePOS Juma sync"
powershell -NoProfile -ExecutionPolicy Bypass -File install_juma_sync.ps1 -Root "%CD%"
if errorlevel 1 (
  echo Could not register the task.
  pause
  exit /b 1
)

echo.
echo Done. The Juma sync is running and will start by itself every time the computer is turned on.
echo Log: %CD%\logs\juma_sync.log
echo After changing .env restart it: schtasks /end /tn "CafePOS Juma sync" and schtasks /run /tn "CafePOS Juma sync"
echo.
pause
