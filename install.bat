@echo off
rem CafePOS: first install on the cafe laptop. Run as administrator.
rem Safe to run again: keeps .env, the database and the owner account.
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8

net session >nul 2>&1
if errorlevel 1 (
  echo Run install.bat as administrator: right click - Run as administrator.
  pause
  exit /b 1
)

where py >nul 2>&1
if errorlevel 1 (
  echo Python not found. Install Python 3.12 or newer from python.org and run install.bat again.
  pause
  exit /b 1
)

echo [1/6] Python packages
if not exist .venv\Scripts\python.exe py -3 -m venv .venv || goto :fail
.venv\Scripts\python.exe -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail

echo [2/6] Settings file .env
.venv\Scripts\python.exe scripts\make_env.py || goto :fail

echo [3/6] Database
.venv\Scripts\python.exe manage.py migrate --noinput || goto :fail
.venv\Scripts\python.exe manage.py collectstatic --noinput -v 0 || goto :fail

echo [4/6] Owner account
.venv\Scripts\python.exe manage.py shell --no-imports -c "import sys; from django.contrib.auth import get_user_model as m; sys.exit(0 if m().objects.filter(is_superuser=True).exists() else 3)"
if %errorlevel%==3 (
  echo Create the owner: login and a strong password.
  .venv\Scripts\python.exe manage.py createsuperuser || goto :fail
) else if errorlevel 1 (
  goto :fail
) else (
  echo Owner already exists
)

echo [5/6] Windows: autostart, backup, firewall, power
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\setup_windows.ps1 -Root "%CD%" || goto :fail

echo [6/6] Starting the server
schtasks /end /tn "CafePOS" >nul 2>&1
schtasks /run /tn "CafePOS" >nul || goto :fail

echo.
echo Done. CafePOS is running and will start by itself every time the laptop is turned on.
echo On this laptop: http://127.0.0.1:8000
.venv\Scripts\python.exe scripts\make_env.py | findstr /c:"http://"
echo Reserve this IP for the laptop in the router settings, so the address does not change.
echo.
pause
exit /b 0

:fail
echo.
echo Install stopped because of the error above.
pause
exit /b 1
