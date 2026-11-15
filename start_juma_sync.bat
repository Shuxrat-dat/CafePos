@echo off
rem CafePOS: background link with Juma delivery (catalog, orders, statuses).
rem install_juma_sync.bat registers the task that runs this file when the laptop is turned on.
rem JUMA_URL and JUMA_MERCHANT_KEY are read from .env. If the process stops, it starts again in 10 seconds.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
:loop
echo ===== %date% %time% start >> logs\juma_sync.log
.venv\Scripts\python.exe manage.py juma_sync >> logs\juma_sync.log 2>&1
timeout /t 10 /nobreak >nul
goto loop
