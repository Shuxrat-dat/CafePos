@echo off
cd /d "%~dp0"
.venv\Scripts\python.exe manage.py collectstatic --noinput
.venv\Scripts\python.exe -m waitress --listen=0.0.0.0:8000 config.wsgi:application