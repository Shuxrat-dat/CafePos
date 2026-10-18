@echo off
rem CafePOS: working server on port 8000.
rem install.bat registers the CafePOS task that runs this file when the laptop is turned on.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
if not exist logs mkdir logs
echo CafePOS: http://127.0.0.1:8000  log: logs\server.log
echo ===== %date% %time% start >> logs\server.log
rem New code after an update needs its migrations before the server starts.
.venv\Scripts\python.exe manage.py migrate --noinput >> logs\server.log 2>&1
.venv\Scripts\python.exe manage.py collectstatic --noinput -v 0 >> logs\server.log 2>&1
.venv\Scripts\python.exe -m waitress --listen=0.0.0.0:8000 config.wsgi:application >> logs\server.log 2>&1
